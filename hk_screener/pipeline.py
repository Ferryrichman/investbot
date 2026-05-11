"""
Daily orchestrator
1. HKEX list (full universe)
2. Price metrics (full)
3. IPO listing date (full)
4. Filter universe (近 4 年 IPO + 細市值 + 不要內資/CH21)
5. CCASS only on filtered universe (~300 stocks)
6. Sponsor records compute
7. Generate data.json for frontend
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import (MAIN_BOARD, GEM, IPO_WITHIN_YEARS, RANGE_DAYS, RANGE_MAX_PCT,
                     TOP_HOLDER_MIN_PCT, BROKER_COUNT_MAX, DAILY_TURNOVER_MAX_HKD,
                     SPONSOR_HIT_RATE_MIN, JSON_OUT, EXCLUDE_STOCK_TYPES)
from .db import init_db, get_conn
from .scrapers import hkex_list, price, ipo_data, ccass, hkexnews_ipo
from .analysis import sponsor, ccass_trend


def get_filter_universe():
    """
    決定邊啲股要去 CCASS scrape — 只係近 4 年 + 小市值嘅符合條件
    其餘唔需要 (因為市值 / 上市年齡 / 內資 / CH21 已經剔除)
    """
    with get_conn() as conn:
        cutoff_year = (datetime.now(timezone.utc).year - IPO_WITHIN_YEARS)
        rows = conn.execute("""
            SELECT s.code, s.code_padded, s.board, s.market_cap_hkd,
                   i.listing_date
            FROM stocks s
            LEFT JOIN ipo_info i ON s.code = i.code
            WHERE s.is_chapter21 = 0
              AND s.stock_type NOT IN ('H Share', 'Red Chip')
              AND s.market_cap_hkd IS NOT NULL
              AND (
                   (s.board = 'Main' AND s.market_cap_hkd <= ?) OR
                   (s.board = 'GEM' AND s.market_cap_hkd <= ?)
              )
              AND i.listing_date IS NOT NULL
              AND i.listing_date >= date('now', ?)
        """, (
            MAIN_BOARD["market_cap_max_hkd"],
            GEM["market_cap_max_hkd"],
            f"-{IPO_WITHIN_YEARS} years",
        )).fetchall()
    return [r["code"] for r in rows]


def screen_candidates():
    """
    最終 screening — apply 全部硬條件 + 軟分數
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
              s.code, s.code_padded, s.name, s.name_zh, s.board, s.stock_type,
              s.industry, s.industry_tags, s.market_cap_hkd, s.is_chapter21,
              i.listing_date, i.ipo_price_hkd, i.raise_amount_hkd, i.sponsor,
              p.last_close, p.avg_turnover, p.range_pct, p.range_high, p.range_low,
              c.snapshot_date as ccass_date, c.top1_pct, c.top10_pct, c.broker_count,
              sr.hit_rate as sponsor_hit_rate, sr.total_ipos as sponsor_total,
              sr.pumped_count as sponsor_pumped
            FROM stocks s
            LEFT JOIN ipo_info i ON s.code = i.code
            LEFT JOIN price_metrics p ON s.code = p.code
            LEFT JOIN ccass_snapshot c ON s.code = c.code
            LEFT JOIN sponsor_record sr ON i.sponsor = sr.sponsor
            WHERE s.is_chapter21 = 0
              AND s.stock_type NOT IN ('H Share', 'Red Chip')
        """).fetchall()

    candidates = []
    for r in rows:
        d = dict(r)
        # Industry tags - parse JSON
        try:
            d["industry_tags"] = json.loads(d.get("industry_tags") or "[]")
        except Exception:
            d["industry_tags"] = []

        # Hard filters - 標記每個條件 pass / fail
        board = d.get("board")
        mc = d.get("market_cap_hkd")
        raise_amt = d.get("raise_amount_hkd")
        listing = d.get("listing_date")
        top10 = d.get("top10_pct")
        brokers = d.get("broker_count")
        turnover = d.get("avg_turnover")
        range_pct = d.get("range_pct")
        sponsor_hr = d.get("sponsor_hit_rate")
        sponsor_name = d.get("sponsor")

        checks = {}
        # Market cap
        if board == "Main":
            checks["mcap"] = bool(mc and mc <= MAIN_BOARD["market_cap_max_hkd"])
        elif board == "GEM":
            checks["mcap"] = bool(mc and mc <= GEM["market_cap_max_hkd"])
        else:
            checks["mcap"] = False

        # IPO raise (overriding) — 只係有數據先 evaluate;冇數據視為 unknown
        if raise_amt is not None:
            limit = MAIN_BOARD["ipo_raise_max_hkd"] if board == "Main" else GEM["ipo_raise_max_hkd"]
            checks["raise"] = raise_amt <= limit
        else:
            checks["raise"] = None  # unknown — 待 manual fill

        # Listing date 4 年內
        if listing:
            try:
                ld = datetime.fromisoformat(listing).date()
                cutoff = datetime.now(timezone.utc).date().replace(
                    year=datetime.now(timezone.utc).year - IPO_WITHIN_YEARS)
                checks["recent_ipo"] = ld >= cutoff
            except Exception:
                checks["recent_ipo"] = False
        else:
            checks["recent_ipo"] = False

        # Top holder concentration - None if no CCASS data
        checks["concentration"] = (top10 >= TOP_HOLDER_MIN_PCT) if top10 is not None else None
        # Broker count - None if no CCASS data
        checks["brokers"] = (brokers < BROKER_COUNT_MAX) if brokers is not None else None
        # Turnover - None if no price data
        checks["turnover"] = (turnover < DAILY_TURNOVER_MAX_HKD) if turnover is not None else None
        # Sideways - None if no price data
        checks["sideways"] = (range_pct < RANGE_MAX_PCT) if range_pct is not None else None
        # Sponsor pumped record - 冇 sponsor name 或者 sponsor 未 score 過 → unknown (None)
        if not sponsor_name:
            checks["sponsor"] = None
        elif sponsor_hr is None:
            checks["sponsor"] = None  # sponsor 有名但未計分(可能 lookback 期外)
        else:
            checks["sponsor"] = sponsor_hr >= SPONSOR_HIT_RATE_MIN

        # 計分 - 每 pass 一個 hard filter +1
        # 凌駕條件 (raise) miss 直接 0 分,因為條件未確認
        n_pass = sum(1 for v in checks.values() if v is True)
        n_known = sum(1 for v in checks.values() if v is not None)

        d["checks"] = checks
        d["score"] = n_pass
        d["score_max"] = n_known
        candidates.append(d)

    # Sort by score desc, then range_pct asc
    candidates.sort(key=lambda x: (-x["score"], x.get("range_pct") or 999))
    return candidates


def export_json(out_path=JSON_OUT):
    """
    Export 3 files:
      data.json       — full unencrypted (local dev only, gitignored)
      data_preview.json — top 5 candidates (public)
      data.enc.json   — full encrypted with INVESTBOT_PE_PASSPHRASE (if set)
    """
    candidates = screen_candidates()

    # Trim - frontend 唔需要全部 raw 資料
    summary = []
    for c in candidates:
        # CCASS time-series + anomalies (only for stocks with history)
        trend = ccass_trend.get_trend_for_export(c["code"])
        summary.append({
            "code": c["code"],
            "name": c.get("name"),
            "name_zh": c.get("name_zh"),
            "board": c["board"],
            "stock_type": c["stock_type"],
            "is_chapter21": bool(c.get("is_chapter21")),
            "industry": c.get("industry"),
            "industry_tags": c.get("industry_tags"),
            "market_cap_hkd": c.get("market_cap_hkd"),
            "listing_date": c.get("listing_date"),
            "ipo_price_hkd": c.get("ipo_price_hkd"),
            "raise_amount_hkd": c.get("raise_amount_hkd"),
            "sponsor": c.get("sponsor"),
            "sponsor_hit_rate": c.get("sponsor_hit_rate"),
            "sponsor_total": c.get("sponsor_total"),
            "sponsor_pumped": c.get("sponsor_pumped"),
            "last_close": c.get("last_close"),
            "avg_turnover": c.get("avg_turnover"),
            "range_pct": c.get("range_pct"),
            "range_high": c.get("range_high"),
            "range_low": c.get("range_low"),
            "top1_pct": c.get("top1_pct"),
            "top10_pct": c.get("top10_pct"),
            "broker_count": c.get("broker_count"),
            "ccass_date": c.get("ccass_date"),
            "checks": c["checks"],
            "score": c["score"],
            "score_max": c["score_max"],
            # CCASS time-series + anomalies (None if no history)
            "trend": trend,
        })

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "main_mcap_max_hkd": MAIN_BOARD["market_cap_max_hkd"],
            "main_raise_max_hkd": MAIN_BOARD["ipo_raise_max_hkd"],
            "gem_mcap_max_hkd": GEM["market_cap_max_hkd"],
            "gem_raise_max_hkd": GEM["ipo_raise_max_hkd"],
            "top_holder_min_pct": TOP_HOLDER_MIN_PCT,
            "broker_count_max": BROKER_COUNT_MAX,
            "turnover_max_hkd": DAILY_TURNOVER_MAX_HKD,
            "range_max_pct": RANGE_MAX_PCT,
            "range_days": RANGE_DAYS,
            "ipo_within_years": IPO_WITHIN_YEARS,
            "sponsor_hit_rate_min": SPONSOR_HIT_RATE_MIN,
        },
        "stocks": summary,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[pipeline] Wrote {len(summary)} stocks → {out_path}")

    # ── Preview (top 5,公開可見)──
    preview_summary = sorted(summary, key=lambda x: -x["score"])[:5]
    # 隱藏敏感欄位
    for s in preview_summary:
        s.pop("trend", None)  # 唔派 sparkline
    preview_out = {
        "generated_at": out["generated_at"],
        "thresholds": out["thresholds"],
        "stocks": preview_summary,
        "is_preview": True,
    }
    preview_path = out_path.parent / "data_preview.json"
    with open(preview_path, "w", encoding="utf-8") as f:
        json.dump(preview_out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[pipeline] Wrote preview ({len(preview_summary)} stocks) → {preview_path}")

    # ── Encrypted full data ──
    passphrase = os.environ.get("INVESTBOT_PE_PASSPHRASE")
    if passphrase:
        try:
            from shared.encrypt import encrypt_bytes
            plaintext = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            blob = encrypt_bytes(plaintext, passphrase)
            enc_path = out_path.parent / "data.enc.json"
            with open(enc_path, "w", encoding="utf-8") as f:
                json.dump(blob, f, separators=(",", ":"))
            print(f"[pipeline] Wrote encrypted blob → {enc_path}")
        except Exception as e:
            print(f"[pipeline] ❌ Encryption failed: {e}")
    else:
        print("[pipeline] INVESTBOT_PE_PASSPHRASE not set — skipping encrypted export")


def run_full(skip_hkex=False, skip_price=False, skip_ipo=False,
             skip_ccass=False, skip_sponsor=False, ccass_only_filtered=True):
    """
    Daily 全 pipeline
    """
    init_db()
    started = time.time()

    if not skip_hkex:
        print("=" * 60)
        print("STEP 1: HKEX listing + market cap")
        print("=" * 60)
        hkex_list.run()

    if not skip_price:
        print("=" * 60)
        print("STEP 2: Price metrics + 300-day range")
        print("=" * 60)
        price.run()

    if not skip_ipo:
        print("=" * 60)
        print("STEP 3a: IPO data (Yahoo firstTradeDate + manual CSV)")
        print("=" * 60)
        ipo_data.run()
        print("=" * 60)
        print("STEP 3b: IPO data (HKEX official NLR XLSX — raise + sponsor)")
        print("=" * 60)
        hkexnews_ipo.run()

    if not skip_ccass:
        print("=" * 60)
        if ccass_only_filtered:
            universe = get_filter_universe()
            print(f"STEP 4: CCASS scrape ({len(universe)} filtered stocks)")
            print("=" * 60)
            ccass.run(code_filter=universe)
        else:
            print("STEP 4: CCASS scrape (FULL universe — slow!)")
            print("=" * 60)
            ccass.run()

    if not skip_sponsor:
        print("=" * 60)
        print("STEP 5: Sponsor pump record")
        print("=" * 60)
        sponsor.compute_sponsor_records()

    print("=" * 60)
    print("STEP 5b: CCASS time-series + anomaly detection")
    print("=" * 60)
    ccass_trend.run()

    print("=" * 60)
    print("STEP 6: Export data.json")
    print("=" * 60)
    export_json()

    print(f"\n[pipeline] All done in {time.time()-started:.0f}s")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "export-only" in args:
        export_json()
    elif "filter-universe" in args:
        u = get_filter_universe()
        print(f"Filter universe ({len(u)} stocks):")
        print(", ".join(u[:50]) + (" ..." if len(u) > 50 else ""))
    else:
        kwargs = {}
        for a in args:
            if a.startswith("--skip-"):
                kwargs[a.replace("--", "").replace("-", "_")] = True
        run_full(**kwargs)
