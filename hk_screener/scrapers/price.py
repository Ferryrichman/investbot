"""
股價 + 成交額 scraper
- 用 Yahoo v8 chart endpoint (穩定免認證)
- 計算過去 RANGE_DAYS 嘅 high/low/range%
- 計算過去 60 日平均日成交額 (HKD)
"""
import time
import requests
import numpy as np
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import RANGE_DAYS, RANGE_MAX_PCT
from ..db import init_db, upsert_price_metrics, get_conn

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_chart(code_padded, days=400):
    """取 Yahoo chart - 返回 list of (ts, close_adj, volume)
    使用 adjclose 自動處理 split / consolidation / dividend"""
    code4 = code_padded.lstrip("0").zfill(4)
    end = int(time.time())
    start = end - days * 86400
    # events=split,div 強制 Yahoo 計算 adjclose
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code4}.HK"
           f"?interval=1d&period1={start}&period2={end}&events=split,div")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        d = r.json()
        result = d.get("chart", {}).get("result")
        if not result:
            return None
        res = result[0]
        timestamps = res.get("timestamp") or []
        quote = res["indicators"]["quote"][0]
        # adjclose 喺另一個 indicator key
        adjclose_block = (res["indicators"].get("adjclose") or [{}])[0]
        adjcloses = adjclose_block.get("adjclose") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        # Prefer adjclose; fallback close 如果未補
        out = []
        n = min(len(timestamps), len(closes), len(volumes))
        for i in range(n):
            ts = timestamps[i]
            adj = adjcloses[i] if i < len(adjcloses) else None
            c_raw = closes[i]
            c = adj if adj is not None else c_raw
            v = volumes[i]
            if c is not None and v is not None:
                # raw_close 用嚟計成交額 (HKD turnover = raw_price × shares,而非 adjusted)
                out.append((ts, float(c), float(v), float(c_raw) if c_raw is not None else float(c)))
        return out
    except Exception:
        return None


def compute_metrics(bars, range_days=RANGE_DAYS):
    """
    bars: list of (timestamp, close_adj, volume, close_raw)
    返回 dict: last_close, avg_turnover (近 60 日), range_pct, range_high, range_low
    range_pct 用 adjclose (自動處理合股/拆細/分紅)
    last_close + turnover 用 raw close (反映實際市場價)
    """
    if not bars or len(bars) < 30:
        return None
    adj = np.array([b[1] for b in bars], dtype=float)
    volumes = np.array([b[2] for b in bars], dtype=float)
    raw = np.array([b[3] for b in bars], dtype=float)

    # 取最近 range_days 日 (用 adjclose 計 range)
    recent_adj = adj[-range_days:] if len(adj) >= range_days else adj
    if len(recent_adj) < 30:
        return None
    high = float(recent_adj.max())
    low = float(recent_adj.min())
    avg = float(recent_adj.mean())
    range_pct = (high - low) / avg * 100 if avg > 0 else 0

    # 近 60 日成交額 = raw_close × volume (HKD turnover)
    n = min(60, len(raw))
    recent_raw = raw[-n:]
    recent_vol = volumes[-n:]
    turnover = recent_raw * recent_vol
    avg_turnover = float(turnover.mean()) if len(turnover) else 0

    return {
        "last_close": float(raw[-1]),  # 顯示市場實際價
        "avg_turnover": avg_turnover,
        "range_pct": float(range_pct),
        "range_high": high,
        "range_low": low,
    }


def run(parallel=20, code_filter=None):
    """跑全部 (或指定 code list) 嘅股價 metrics"""
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
    print(f"[price] Computing metrics for {len(stocks)} stocks (parallel={parallel})")

    started = time.time()
    completed = 0
    success = 0

    def process(stock):
        bars = fetch_chart(stock["code_padded"], days=RANGE_DAYS + 50)
        m = compute_metrics(bars)
        return stock["code"], m

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {ex.submit(process, s): s for s in stocks}
        for fut in as_completed(futures):
            code, m = fut.result()
            completed += 1
            if completed % 200 == 0:
                elapsed = time.time() - started
                print(f"  {completed}/{len(stocks)}  ({elapsed:.0f}s)  success={success}")
            if m:
                m["code"] = code
                m["last_updated"] = datetime.utcnow().isoformat()
                upsert_price_metrics(m)
                success += 1

    print(f"[price] Done in {time.time()-started:.0f}s — success={success}/{len(stocks)}")
    return success


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        # Limit mode — pick first N stocks from DB
        n = int(sys.argv[1])
        with get_conn() as conn:
            codes = [r["code"] for r in conn.execute(
                f"SELECT code FROM stocks LIMIT {n}").fetchall()]
        run(code_filter=codes)
    else:
        run()
