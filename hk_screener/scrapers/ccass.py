"""
CCASS shareholding scraper
從 sdw.hkexnews.hk 抓取每隻股嘅 CCASS 持股分布
- POST form with stock code + date
- Parse HTML table for participant ID, name, shares, %
- Compute top10_pct, top1_pct, broker_count
"""
import re
import time
import json
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

from ..config import TOP_HOLDER_MIN_PCT, BROKER_COUNT_MAX
from ..db import init_db, upsert_ccass, upsert_ccass_history, get_conn

CCASS_URL = "https://www3.hkexnews.hk/sdw/search/searchsdw.aspx"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _extract_form_field(html, name):
    m = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html, re.DOTALL)
    return m.group(1) if m else ""


def _new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(CCASS_URL, timeout=20)
    r.raise_for_status()
    s._viewstate = _extract_form_field(r.text, "__VIEWSTATE")
    s._viewstategen = _extract_form_field(r.text, "__VIEWSTATEGENERATOR")
    return s


def fetch_ccass(code_padded, query_date=None, session=None):
    """
    抓取單隻股嘅 CCASS 數據
    code_padded: 5-digit string (e.g. "01141")
    query_date: datetime object,默認用昨日
    返回 dict 或 None
    """
    if query_date is None:
        query_date = datetime.now() - timedelta(days=1)
        # If weekend, roll back to Friday
        while query_date.weekday() >= 5:
            query_date -= timedelta(days=1)
    date_str = query_date.strftime("%Y/%m/%d")

    sess = session or _new_session()

    post_data = {
        "__EVENTTARGET": "btnSearch",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": sess._viewstate,
        "__VIEWSTATEGENERATOR": sess._viewstategen,
        "today": "",
        "sortBy": "",
        "sortDirection": "asc",
        "txtShareholdingDate": date_str,
        "txtStockCode": code_padded,
        "txtStockName": "",
        "txtParticipantID": "",
        "txtParticipantName": "",
    }
    try:
        r = sess.post(CCASS_URL, data=post_data, timeout=30)
    except Exception as e:
        print(f"  [ccass] POST failed for {code_padded}: {e}")
        return None
    if r.status_code != 200:
        return None

    return _parse_ccass_html(r.text, code_padded, date_str)


def _parse_ccass_html(html, code_padded, date_str):
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    target = None
    for t in tables:
        rows = t.find_all("tr")
        if len(rows) >= 5:
            head = rows[0].get_text()
            if "Participant ID" in head and "Shareholding" in head:
                target = t
                break
    if not target:
        return None

    rows = target.find_all("tr")
    participants = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 5:
            continue
        # Cell text format: "Label: value" (mobile-friendly prepend) — take after first ":"
        def clean(c):
            txt = c.get_text(separator=" ", strip=True)
            if ":" in txt:
                # Take everything after the FIRST colon (label is always prefix)
                return txt.split(":", 1)[1].strip()
            return txt

        pid = clean(cells[0])
        pname = clean(cells[1])
        # cells[2] = address (skip)
        shares_raw = clean(cells[3])
        pct_raw = clean(cells[4]) if len(cells) > 4 else ""

        shares = _parse_number(shares_raw)
        pct = _parse_number(pct_raw)
        if shares is None or pct is None:
            continue
        participants.append({
            "id": pid,
            "name": pname,
            "shares": shares,
            "pct": pct,
        })

    if not participants:
        return None

    # Sort by shares desc
    participants.sort(key=lambda p: -p["shares"])
    top1 = participants[0]["pct"]
    top10 = sum(p["pct"] for p in participants[:10])
    # Broker count: exclude HKSCC Nominees (the central depositary itself, B01664)
    # And exclude the issuer itself (e.g. B00000 投资管理公司)
    # Just count distinct participant IDs that hold > 0
    broker_count = sum(1 for p in participants if p["shares"] > 0)

    # Top 20 for time-series tracking (history table)
    top20 = [{"id": p["id"], "name": p["name"][:50], "pct": p["pct"]}
             for p in participants[:20]]
    return {
        "snapshot_date": date_str,
        "top1_pct": top1,
        "top10_pct": top10,
        "broker_count": broker_count,
        "raw_json": json.dumps(participants, ensure_ascii=False),
        "top20_json": json.dumps(top20, ensure_ascii=False),
    }


def _parse_number(s):
    """Parse '1,234,567' or '5.43%' or '12.34' into float"""
    if not s:
        return None
    cleaned = re.sub(r"[,\s%]", "", s)
    try:
        return float(cleaned)
    except ValueError:
        return None


def run(code_filter=None, delay=0.3):
    """
    跑 CCASS scrape (sequential — 唔好 hammer HKEX)
    code_filter: 限制 universe (建議 only filter passing stocks)
    """
    init_db()
    with get_conn() as conn:
        if code_filter:
            placeholders = ",".join("?" * len(code_filter))
            stocks = conn.execute(
                f"SELECT code, code_padded FROM stocks WHERE code IN ({placeholders})",
                code_filter
            ).fetchall()
        else:
            stocks = conn.execute("SELECT code, code_padded FROM stocks").fetchall()

    print(f"[ccass] Scraping CCASS for {len(stocks)} stocks (delay={delay}s)")
    started = time.time()
    sess = _new_session()
    success = 0
    for i, stock in enumerate(stocks, 1):
        if i % 50 == 0:
            elapsed = time.time() - started
            rate = i / elapsed
            eta = (len(stocks) - i) / rate if rate > 0 else 0
            print(f"  {i}/{len(stocks)}  ({elapsed:.0f}s)  success={success}  ETA={eta:.0f}s")
            # Refresh session every 50 to avoid stale state
            sess = _new_session()
        data = fetch_ccass(stock["code_padded"], session=sess)
        if data:
            data["code"] = stock["code"]
            data["last_updated"] = datetime.now(timezone.utc).isoformat()
            # Latest snapshot (one row per stock)
            upsert_ccass(data)
            # Historical record (append per-date)
            upsert_ccass_history({
                "code": stock["code"],
                "snapshot_date": data["snapshot_date"],
                "top1_pct": data["top1_pct"],
                "top10_pct": data["top10_pct"],
                "broker_count": data["broker_count"],
                "top20_json": data["top20_json"],
                "last_updated": data["last_updated"],
            })
            success += 1
        time.sleep(delay)

    print(f"[ccass] Done in {time.time()-started:.0f}s — success={success}/{len(stocks)}")
    return success


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        n = int(sys.argv[1])
        with get_conn() as conn:
            codes = [r["code"] for r in conn.execute(
                f"SELECT code FROM stocks LIMIT {n}").fetchall()]
        run(code_filter=codes)
    elif len(sys.argv) > 1:
        # Specific code
        result = fetch_ccass(sys.argv[1].zfill(5))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        run()
