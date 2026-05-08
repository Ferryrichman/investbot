"""
HKEX 上市清單 scraper
- 下載 HKEX 官方 List of Securities XLSX
- 篩選出 Equity + Investment Companies
- 識別 Main Board vs GEM (依 stock code)
- Flag Chapter 21 (Sub-Category = "Investment Companies")
- 用 yfinance 抓取 industry, sector, marketCap, country
"""
import io
import json
import time
import requests
import openpyxl
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import INDUSTRY_TAGS, EXCLUDE_STOCK_TYPES, CHAPTER_21_CODES
from ..db import init_db, upsert_stocks

HKEX_XLSX_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# GICS / Yahoo industry → 自定 tag mapping
# (Yahoo 用 GICS,我哋 map 返做 shell_friendly / acct_friendly)
_KEYWORD_TAGS = [
    # (substring → tags) — case-insensitive substring match against Yahoo industry
    ("construction", ["shell_friendly", "acct_friendly"]),
    ("engineering", ["shell_friendly", "acct_friendly"]),
    ("building products", ["acct_friendly"]),
    ("real estate", ["shell_friendly"]),
    ("reit", ["shell_friendly"]),
    ("capital markets", ["shell_friendly"]),
    ("asset management", ["shell_friendly"]),
    ("credit services", ["shell_friendly"]),
    ("banks", ["shell_friendly"]),
    ("insurance", ["shell_friendly"]),
    ("financial conglomerates", ["shell_friendly"]),
    ("software", ["acct_friendly"]),
    ("information technology services", ["acct_friendly"]),
    ("internet content", ["acct_friendly"]),
    ("computer hardware", ["acct_friendly"]),
    ("electronic gaming", ["acct_friendly"]),
    ("it services", ["acct_friendly"]),
]

# 夕陽 / 奇怪行業 (substring match, case-insensitive)
_SUNSET_KEYWORDS = [
    "textile", "apparel", "coking coal", "paper", "lumber", "aluminum", "tobacco",
    "footwear", "leisure facilities", "publishing", "broadcasting",
]


def download_hkex_list():
    """下載 HKEX XLSX 並解析"""
    print(f"[hkex_list] Downloading {HKEX_XLSX_URL}")
    r = requests.get(HKEX_XLSX_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    print(f"[hkex_list] Got {len(r.content)} bytes")
    wb = openpyxl.load_workbook(io.BytesIO(r.content), data_only=True)
    ws = wb.active

    rows = []
    # Headers in row 3, data from row 4
    for row in ws.iter_rows(min_row=4, values_only=True):
        code_padded = row[0]
        name = row[1]
        category = row[2]
        sub_category = row[3]
        if not code_padded or not category:
            continue
        # 只要 Equity 同 Investment Companies (Chapter 21 候選)
        if category != "Equity":
            continue
        # Sub-category: "Equity Securities (Main Board)" / "Equity Securities (GEM)" / "Investment Companies"
        if "Equity Securities" not in str(sub_category) and "Investment Companies" not in str(sub_category):
            continue
        rows.append({
            "code_padded": str(code_padded).zfill(5),
            "code": str(code_padded).lstrip("0") or "0",
            "name": name,
            "sub_category": sub_category,
        })
    print(f"[hkex_list] Parsed {len(rows)} equity rows")
    return rows


def determine_board(code):
    """code 4-digit → Main / GEM"""
    code_int = int(code)
    if 8000 <= code_int < 9000:
        return "GEM"
    return "Main"


_SESSION = None
_CRUMB = None

def _yahoo_session():
    """Get cached Yahoo session with crumb. Direct HTTP (avoid yfinance/curl_cffi SSL issues at scale)."""
    global _SESSION, _CRUMB
    if _SESSION and _CRUMB:
        return _SESSION, _CRUMB
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    s.get("https://fc.yahoo.com", timeout=10)  # set cookies
    r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    crumb = r.text.strip()
    # Crumb is short alphanumeric (~11 chars). Reject error JSON responses (longer).
    if not crumb or len(crumb) > 30 or "{" in crumb:
        return None, None
    _SESSION = s
    _CRUMB = crumb
    return s, crumb


def fetch_yahoo_info(code_padded, retry=True):
    """Direct HTTP query of Yahoo quoteSummary — return dict or None"""
    code4 = code_padded.lstrip("0").zfill(4)
    sess, crumb = _yahoo_session()
    if not sess:
        return None
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{code4}.HK"
           f"?modules=price,summaryDetail,assetProfile&crumb={crumb}")
    try:
        r = sess.get(url, timeout=15)
        if r.status_code == 401 and retry:
            # Crumb expired - reset and retry once
            global _SESSION, _CRUMB
            _SESSION = None
            _CRUMB = None
            return fetch_yahoo_info(code_padded, retry=False)
        if r.status_code != 200:
            return None
        d = r.json()
        res = d.get("quoteSummary", {}).get("result")
        if not res:
            return None
        r0 = res[0]
        price = r0.get("price", {}) or {}
        sd = r0.get("summaryDetail", {}) or {}
        ap = r0.get("assetProfile", {}) or {}
        # Helper to extract .raw from {raw, fmt} struct
        def raw(d, k):
            v = d.get(k)
            if isinstance(v, dict):
                return v.get("raw")
            return v
        return {
            "industry": ap.get("industry"),
            "sector": ap.get("sector"),
            "market_cap_hkd": raw(price, "marketCap"),
            "country": ap.get("country"),
            "long_name": price.get("longName"),
            "shares_outstanding": raw(sd, "sharesOutstanding"),
            "currency": price.get("currency"),
        }
    except Exception:
        return None


def determine_industry_tags(yahoo_industry, sub_category):
    """組合 industry tags"""
    tags = set()
    if yahoo_industry:
        ind_lower = yahoo_industry.lower()
        for keyword, kw_tags in _KEYWORD_TAGS:
            if keyword in ind_lower:
                tags.update(kw_tags)
        for sk in _SUNSET_KEYWORDS:
            if sk in ind_lower:
                tags.add("sunset")
                break
    return sorted(tags)


def determine_stock_type(yahoo_info, code):
    """
    識別 H Share / Red Chip
    - country == "China" → 大概率係 H Share (中國註冊)
    - Red Chip 較難自動識別(註冊地多為 HK / Cayman)
    """
    if not yahoo_info:
        return "Unknown"
    country = yahoo_info.get("country") or ""
    if country.lower() in ("china", "people's republic of china"):
        return "H Share"
    return "Equity"


def run(parallel=20, limit=None):
    """主入口 - scrape 全部 HK 股票 list + Yahoo 資料,寫入 DB"""
    init_db()
    rows = download_hkex_list()
    if limit:
        rows = rows[:limit]
    print(f"[hkex_list] Fetching Yahoo info for {len(rows)} stocks (parallel={parallel})")

    db_rows = []
    started = time.time()
    completed = 0

    def process(row):
        info = fetch_yahoo_info(row["code_padded"])
        return row, info

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {ex.submit(process, r): r for r in rows}
        for fut in as_completed(futures):
            row, info = fut.result()
            completed += 1
            if completed % 100 == 0:
                elapsed = time.time() - started
                print(f"  {completed}/{len(rows)}  ({elapsed:.0f}s)")

            yahoo_industry = info["industry"] if info else None
            yahoo_sector = info["sector"] if info else None
            market_cap = info["market_cap_hkd"] if info else None
            stock_type = determine_stock_type(info, row["code"])
            tags = determine_industry_tags(yahoo_industry, row["sub_category"])

            is_ch21 = 1 if "Investment Companies" in str(row["sub_category"]) else 0
            if row["code"] in CHAPTER_21_CODES:
                is_ch21 = 1

            db_rows.append({
                "code": row["code"],
                "code_padded": row["code_padded"],
                "name": row["name"],
                "name_zh": (info or {}).get("long_name"),
                "board": determine_board(row["code"]),
                "stock_type": stock_type,
                "industry": yahoo_industry,
                "industry_tags": json.dumps(tags),
                "market_cap_hkd": market_cap,
                "is_chapter21": is_ch21,
                "last_updated": datetime.utcnow().isoformat(),
            })

    print(f"[hkex_list] Writing {len(db_rows)} rows to DB...")
    # Batch insert chunks of 500
    for i in range(0, len(db_rows), 500):
        upsert_stocks(db_rows[i:i+500])
    print(f"[hkex_list] Done in {time.time()-started:.0f}s")
    return len(db_rows)


if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(limit=limit)
