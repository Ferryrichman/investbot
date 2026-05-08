"""
IPO 數據 scraper
策略:
  1) Yahoo firstTradeDate → 上市日期 (auto, 全部股都有)
  2) Yahoo first close → IPO price 估算 (proxy,首日 close)
  3) Manual CSV override 提供準確 raise_amount + sponsor (data/ipo_manual.csv)
  4) Best-effort HKEXnews 搜尋 Allotment Results announcements (TODO Phase 3b)

CSV format (data/ipo_manual.csv):
  code,raise_amount_hkd,sponsor,ipo_price
  9890,80000000,某某金融,1.20
  ...
"""
import csv
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import DATA_DIR, IPO_WITHIN_YEARS
from ..db import init_db, upsert_ipo, get_conn

HEADERS = {"User-Agent": "Mozilla/5.0"}
MANUAL_CSV = DATA_DIR / "ipo_manual.csv"


def fetch_yahoo_listing(code_padded):
    """從 Yahoo chart 拎 firstTradeDate + 首個有效 close (= IPO price proxy)"""
    code4 = code_padded.lstrip("0").zfill(4)
    # Range max 取盡可能舊嘅資料
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code4}.HK?interval=1d&range=10y"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        d = r.json()
        result = d.get("chart", {}).get("result")
        if not result:
            return None
        res = result[0]
        first_ts = res["meta"].get("firstTradeDate")
        if not first_ts:
            return None
        listing_date = datetime.fromtimestamp(first_ts, tz=timezone.utc).date().isoformat()

        # First non-null close (IPO price proxy)
        timestamps = res.get("timestamp") or []
        closes = res["indicators"]["quote"][0].get("close") or []
        first_close = None
        for c in closes:
            if c is not None:
                first_close = float(c)
                break

        return {
            "listing_date": listing_date,
            "ipo_price_proxy": first_close,
        }
    except Exception:
        return None


def load_manual_csv():
    """Load manual IPO data overrides"""
    if not MANUAL_CSV.exists():
        return {}
    out = {}
    with open(MANUAL_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get("code", "").strip()
            if not code:
                continue
            out[code] = {
                "raise_amount_hkd": float(row["raise_amount_hkd"]) if row.get("raise_amount_hkd") else None,
                "sponsor": row.get("sponsor", "").strip() or None,
                "ipo_price": float(row["ipo_price"]) if row.get("ipo_price") else None,
            }
    return out


def ensure_manual_template():
    """Create empty CSV template if not exists"""
    if not MANUAL_CSV.exists():
        MANUAL_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(MANUAL_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "raise_amount_hkd", "sponsor", "ipo_price"])
            w.writerow(["# Sample row, delete and fill in real data:", "", "", ""])
        print(f"[ipo] Created template at {MANUAL_CSV}")


def is_within_years(listing_date_iso, years):
    if not listing_date_iso:
        return False
    try:
        d = datetime.fromisoformat(listing_date_iso).date()
        cutoff = datetime.now(timezone.utc).date()
        cutoff = cutoff.replace(year=cutoff.year - years)
        return d >= cutoff
    except Exception:
        return False


def run(parallel=20, code_filter=None):
    init_db()
    ensure_manual_template()
    manual = load_manual_csv()
    print(f"[ipo] Loaded {len(manual)} manual entries from {MANUAL_CSV.name}")

    with get_conn() as conn:
        if code_filter:
            placeholders = ",".join("?" * len(code_filter))
            stocks = conn.execute(
                f"SELECT code, code_padded FROM stocks WHERE code IN ({placeholders})",
                code_filter
            ).fetchall()
        else:
            stocks = conn.execute("SELECT code, code_padded FROM stocks").fetchall()

    print(f"[ipo] Fetching listing data for {len(stocks)} stocks (parallel={parallel})")
    started = time.time()
    completed = 0
    success = 0

    def process(stock):
        return stock["code"], fetch_yahoo_listing(stock["code_padded"])

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {ex.submit(process, s): s for s in stocks}
        for fut in as_completed(futures):
            code, info = fut.result()
            completed += 1
            if completed % 200 == 0:
                elapsed = time.time() - started
                print(f"  {completed}/{len(stocks)}  ({elapsed:.0f}s)  success={success}")
            if not info:
                continue

            override = manual.get(code, {})
            ipo_price = override.get("ipo_price") or info["ipo_price_proxy"]
            raise_amount = override.get("raise_amount_hkd")
            sponsor = override.get("sponsor")

            row = {
                "code": code,
                "listing_date": info["listing_date"],
                "ipo_price_hkd": ipo_price,
                "raise_amount_hkd": raise_amount,
                "sponsor": sponsor,
                "sponsors_all": None,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            upsert_ipo(row)
            success += 1

    print(f"[ipo] Done in {time.time()-started:.0f}s — success={success}/{len(stocks)}")

    # Stats: 近 N 年上市
    with get_conn() as conn:
        recent = conn.execute("""
            SELECT COUNT(*) FROM ipo_info
            WHERE listing_date >= date('now', ?)
        """, (f"-{IPO_WITHIN_YEARS} years",)).fetchone()[0]
        with_raise = conn.execute("""
            SELECT COUNT(*) FROM ipo_info
            WHERE listing_date >= date('now', ?) AND raise_amount_hkd IS NOT NULL
        """, (f"-{IPO_WITHIN_YEARS} years",)).fetchone()[0]
    print(f"[ipo] Recent {IPO_WITHIN_YEARS}-year IPOs: {recent} (with manual raise data: {with_raise})")
    return success


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        n = int(sys.argv[1])
        with get_conn() as conn:
            codes = [r["code"] for r in conn.execute(
                f"SELECT code FROM stocks LIMIT {n}").fetchall()]
        run(code_filter=codes)
    else:
        run()
