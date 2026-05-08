"""
Recovery script — backfill mcap/industry/country for stocks lacking that data.
Sequential with delay (rate-limit safe). Only targets recent IPOs by default.
"""
import sys
import time
from datetime import datetime
from .hkex_list import (fetch_yahoo_info, determine_stock_type,
                        determine_industry_tags)
from ..db import init_db, get_conn
from ..config import IPO_WITHIN_YEARS
import json

def run(limit=None, delay=0.8, only_recent_ipos=True):
    init_db()
    with get_conn() as conn:
        if only_recent_ipos:
            stocks = conn.execute("""
                SELECT s.code, s.code_padded, s.name, s.board, s.is_chapter21
                FROM stocks s JOIN ipo_info i ON s.code = i.code
                WHERE i.listing_date >= date('now', ?)
                  AND s.market_cap_hkd IS NULL
                ORDER BY s.code
            """, (f"-{IPO_WITHIN_YEARS} years",)).fetchall()
        else:
            stocks = conn.execute("""
                SELECT code, code_padded, name, board, is_chapter21
                FROM stocks WHERE market_cap_hkd IS NULL ORDER BY code
            """).fetchall()

    if limit:
        stocks = stocks[:limit]

    print(f"[backfill] Processing {len(stocks)} stocks (delay={delay}s)")
    started = time.time()
    success = 0
    failed_codes = []

    with get_conn() as conn:
        for i, s in enumerate(stocks, 1):
            info = fetch_yahoo_info(s["code_padded"])
            if not info or info.get("market_cap_hkd") is None:
                failed_codes.append(s["code"])
            else:
                stock_type = determine_stock_type(info, s["code"])
                tags = determine_industry_tags(info.get("industry"), None)
                conn.execute("""
                    UPDATE stocks SET
                        industry = ?, stock_type = ?, market_cap_hkd = ?,
                        industry_tags = ?, name_zh = ?,
                        last_updated = ?
                    WHERE code = ?
                """, (
                    info.get("industry"),
                    stock_type,
                    info.get("market_cap_hkd"),
                    json.dumps(tags),
                    info.get("long_name"),
                    datetime.utcnow().isoformat(),
                    s["code"],
                ))
                success += 1
                conn.commit()

            if i % 20 == 0:
                elapsed = time.time() - started
                rate = i / elapsed
                eta = (len(stocks) - i) / rate if rate else 0
                print(f"  {i}/{len(stocks)}  ({elapsed:.0f}s)  success={success}  ETA={eta:.0f}s")

            time.sleep(delay)

    print(f"[backfill] Done in {time.time()-started:.0f}s — success={success}/{len(stocks)}")
    if failed_codes:
        print(f"[backfill] Failed: {len(failed_codes)} codes (first 10): {failed_codes[:10]}")
    return success


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(limit=limit)
