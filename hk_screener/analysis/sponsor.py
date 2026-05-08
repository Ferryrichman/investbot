"""
保薦人炒作記錄分析
邏輯:
  - 對每個保薦人,搵晒佢過去 SPONSOR_LOOKBACK_YEARS 年保薦過嘅 IPO
  - 每隻 IPO 由上市日開始,睇 5 年內最高價有冇 >= 3x IPO 價
  - 命中率 = 炒過嘅隻數 / 總隻數
  - 若 hit_rate >= SPONSOR_HIT_RATE_MIN → 該保薦人 flag「有炒作記錄」
"""
import time
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import (SPONSOR_LOOKBACK_YEARS, SPONSOR_PUMP_MULTIPLE,
                      SPONSOR_HIT_RATE_MIN)
from ..db import init_db, upsert_sponsor_record, get_conn

HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_post_ipo_max(code_padded, listing_date_iso, years=5):
    """
    抓取上市日起 N 年內嘅最高價
    返回 max_price 或 None
    """
    code4 = code_padded.lstrip("0").zfill(4)
    try:
        listing_dt = datetime.fromisoformat(listing_date_iso).replace(tzinfo=timezone.utc)
    except Exception:
        return None
    end_dt = min(listing_dt + timedelta(days=years * 366),
                 datetime.now(timezone.utc))
    period1 = int(listing_dt.timestamp())
    period2 = int(end_dt.timestamp())

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code4}.HK"
           f"?interval=1d&period1={period1}&period2={period2}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        d = r.json()
        result = d.get("chart", {}).get("result")
        if not result:
            return None
        highs = result[0]["indicators"]["quote"][0].get("high") or []
        valid = [h for h in highs if h is not None]
        if not valid:
            return None
        return max(valid)
    except Exception:
        return None


def compute_sponsor_records():
    """
    主邏輯 - 統計每個保薦人嘅 IPO 命中率
    """
    init_db()
    cutoff = (datetime.now(timezone.utc) -
              timedelta(days=SPONSOR_LOOKBACK_YEARS * 366)).date().isoformat()

    with get_conn() as conn:
        # 拎所有有 sponsor + 上市日期嘅 IPO,且係 lookback period 內
        ipos = conn.execute("""
            SELECT i.code, s.code_padded, i.listing_date, i.ipo_price_hkd, i.sponsor
            FROM ipo_info i
            JOIN stocks s ON i.code = s.code
            WHERE i.sponsor IS NOT NULL
              AND i.sponsor != ''
              AND i.listing_date IS NOT NULL
              AND i.listing_date >= ?
              AND i.ipo_price_hkd IS NOT NULL
        """, (cutoff,)).fetchall()

    print(f"[sponsor] Analyzing {len(ipos)} IPOs (lookback {SPONSOR_LOOKBACK_YEARS}y)")

    if not ipos:
        print("[sponsor] No IPOs with sponsor data — fill in data/ipo_manual.csv")
        return 0

    # 抓取每隻 IPO 嘅 5 年內最高價 (parallel)
    started = time.time()
    results = {}  # code → max_price

    def process(ipo):
        mp = fetch_post_ipo_max(ipo["code_padded"], ipo["listing_date"], years=5)
        return ipo["code"], mp

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(process, ipo): ipo for ipo in ipos}
        for fut in as_completed(futures):
            code, mp = fut.result()
            results[code] = mp

    print(f"[sponsor] Got max prices for {sum(1 for v in results.values() if v)} stocks "
          f"in {time.time()-started:.0f}s")

    # 按 sponsor 統計
    sponsor_stats = {}  # sponsor → {total, pumped}
    for ipo in ipos:
        sponsor = ipo["sponsor"]
        # 多保薦人用逗號分隔 → 每個都計一次
        for s in (sp.strip() for sp in sponsor.split(",")):
            if not s:
                continue
            stats = sponsor_stats.setdefault(s, {"total": 0, "pumped": 0})
            stats["total"] += 1
            mp = results.get(ipo["code"])
            ipo_price = ipo["ipo_price_hkd"]
            if mp and ipo_price and mp >= ipo_price * SPONSOR_PUMP_MULTIPLE:
                stats["pumped"] += 1

    # 寫入 DB
    now = datetime.now(timezone.utc).isoformat()
    flagged = []
    for sponsor, stats in sponsor_stats.items():
        total = stats["total"]
        pumped = stats["pumped"]
        hit_rate = (pumped / total) if total > 0 else 0
        upsert_sponsor_record({
            "sponsor": sponsor,
            "total_ipos": total,
            "pumped_count": pumped,
            "hit_rate": hit_rate,
            "last_computed": now,
        })
        if hit_rate >= SPONSOR_HIT_RATE_MIN:
            flagged.append((sponsor, pumped, total, hit_rate))

    flagged.sort(key=lambda x: (-x[3], -x[2]))
    print(f"\n[sponsor] {len(flagged)} sponsors with hit_rate >= {SPONSOR_HIT_RATE_MIN:.0%}:")
    for s, p, t, hr in flagged[:20]:
        print(f"  {s[:40]:<40}  {p}/{t}  =  {hr:.0%}")
    return len(sponsor_stats)


if __name__ == "__main__":
    compute_sponsor_records()
