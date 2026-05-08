"""
CCASS 歷史 backfill — 為 filter universe scrape 過去 N 個 trading day
寫入 ccass_history 表(per stock per date)
"""
import sys
import time
import json
from datetime import datetime, timedelta, timezone
from .ccass import fetch_ccass, _new_session
from ..db import init_db, upsert_ccass_history, get_conn
from ..config import IPO_WITHIN_YEARS


def trading_days_back(n):
    """Return list of N most recent trading days (skip Sat/Sun) descending"""
    days = []
    d = datetime.now() - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d -= timedelta(days=1)
    return days


def get_universe():
    """Filter universe — 同 pipeline.get_filter_universe 一樣"""
    from ..config import MAIN_BOARD, GEM
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.code, s.code_padded
            FROM stocks s
            LEFT JOIN ipo_info i ON s.code = i.code
            WHERE s.is_chapter21 = 0
              AND s.stock_type NOT IN ('H Share', 'Red Chip')
              AND s.market_cap_hkd IS NOT NULL
              AND ((s.board='Main' AND s.market_cap_hkd <= ?) OR
                   (s.board='GEM' AND s.market_cap_hkd <= ?))
              AND i.listing_date IS NOT NULL
              AND i.listing_date >= date('now', ?)
        """, (MAIN_BOARD["market_cap_max_hkd"],
              GEM["market_cap_max_hkd"],
              f"-{IPO_WITHIN_YEARS} years")).fetchall()
    return rows


def run(n_days=30, delay=0.3, code_filter=None):
    """
    Backfill 過去 n_days 個 trading day 嘅 CCASS 數據,只跑 filter universe
    """
    init_db()

    if code_filter:
        with get_conn() as conn:
            placeholders = ",".join("?" * len(code_filter))
            stocks = conn.execute(
                f"SELECT code, code_padded FROM stocks WHERE code IN ({placeholders})",
                code_filter
            ).fetchall()
    else:
        stocks = get_universe()

    days = trading_days_back(n_days)
    total_calls = len(stocks) * len(days)
    print(f"[ccass_backfill] {len(stocks)} stocks × {len(days)} days = {total_calls} calls")
    print(f"  Date range: {days[-1].strftime('%Y-%m-%d')} → {days[0].strftime('%Y-%m-%d')}")
    print(f"  Estimated time: {total_calls * (delay + 1):.0f}s")

    started = time.time()
    success = 0
    skipped = 0
    sess = _new_session()

    # Check existing data — skip dates we already have
    with get_conn() as conn:
        existing = {(r[0], r[1]) for r in conn.execute(
            "SELECT code, snapshot_date FROM ccass_history").fetchall()}

    call_idx = 0
    for stock in stocks:
        for day in days:
            call_idx += 1
            date_str = day.strftime("%Y/%m/%d")
            key = (stock["code"], date_str)
            if key in existing:
                skipped += 1
                continue

            data = fetch_ccass(stock["code_padded"], query_date=day, session=sess)
            if data:
                upsert_ccass_history({
                    "code": stock["code"],
                    "snapshot_date": data["snapshot_date"],
                    "top1_pct": data["top1_pct"],
                    "top10_pct": data["top10_pct"],
                    "broker_count": data["broker_count"],
                    "top20_json": data["top20_json"],
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                })
                success += 1

            if call_idx % 30 == 0:
                elapsed = time.time() - started
                pct = call_idx / total_calls * 100
                eta = (total_calls - call_idx) * (elapsed / call_idx) if call_idx else 0
                print(f"  {call_idx}/{total_calls} ({pct:.0f}%)  success={success} skipped={skipped}  ETA={eta:.0f}s")
                sess = _new_session()  # refresh session

            time.sleep(delay)

    print(f"[ccass_backfill] Done in {time.time()-started:.0f}s — success={success}, skipped={skipped}")
    return success


if __name__ == "__main__":
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    code_filter = sys.argv[2:] if len(sys.argv) > 2 else None
    run(n_days=n_days, code_filter=code_filter)
