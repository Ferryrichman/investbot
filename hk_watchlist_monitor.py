#!/usr/bin/env python3
"""
HK 港股 Watchlist Monitor  v3
==============================
- 主板 / 創業板分開觸發市值
- 固定每注金額，逐層加倉（以每手lot計算，唔係單股）
- 止賺：浮盈>=100% 或 市值回升殼價 → 賣夠回成本 → 0成本持倉
- 0成本後：殼價×1.5/2.0/3.0 逐步套利
- 止損信號：CCASS IN / 異常盤路 / 大股東沽貨（提示，非自動）

殼價參考 (2024-2025估計):
  主板: 1.5-2.5億 HKD（高峰2018年為6-7億，已跌~75%）
  GEM:  6,000萬-1億 HKD（GEM 2024年改革後殼活動近乎停止）

使用方式:
  python hk_watchlist_monitor.py           # 全部報告 + TG
  python hk_watchlist_monitor.py check     # 只 print，唔發 TG
  python hk_watchlist_monitor.py alert     # 只推買入 / 止賺訊號 (每朝09:00)
  python hk_watchlist_monitor.py intraday  # 即時信號 (交易時段每15分鐘)
  python hk_watchlist_monitor.py plan      # 打印資金計劃表
  python hk_watchlist_monitor.py lotsize   # 更新每手股數快取
"""

import os, sys, json, time, requests, math, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# ★ 配置區 ★
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8795735773:AAEeKN_eIc1_TEcxqAZvFSVHh85GAbfE8vA")
CHAT_ID        = os.environ.get("CHAT_ID", "577581404")

# ── Watchlist ──────────────────────────────────────────────
# 格式: "股票號碼": {"board": "main"/"gem", "shell_m": 殼價百萬(可選), "lot": 每手股數(可選)}
# board: main=主板, gem=創業板
# shell_m: 可個別override殼價（唔填就用預設）
# lot: 每手股數（唔填就從HKEX自動抓取）
# WATCHLIST 已遷移至 data/watchlist_state.json
# 每隻股票嘅 "board" 欄位決定主板/創業板
# 用 TG Bot /add /remove 管理，唔再 hardcode

# ── 每注固定金額 (HKD) ────────────────────────────────────
TRANCHE_SIZE = 6_000    # default; 開 alert 時動態 = TOTAL / 100 (cash + market val)
MIN_BUY_HKD  = 1_000    # 最低買入信號 $1,000，細過唔出

# ── 資金管理 ─────────────────────────────────────────────
TOTAL_PORTFOLIO = 332_043   # 初始入金 (cash + invested). 入金/抽資需更新
MIN_CASH_PCT    = 0.20      # 保持至少20%現金

# ── 主板買入觸發市值 (百萬 HKD) ───────────────────────────
# 每個數字係一個買入層，市值跌穿就入一注 TRANCHE_SIZE
MAIN_TIERS_M = [200, 150, 120, 100, 80, 60]   # 單位：百萬

# ── 創業板買入觸發市值 (百萬 HKD) ────────────────────────
GEM_TIERS_M  = [80, 60, 50, 40, 30]

# ── 殼價參考（止賺用）────────────────────────────────────
# 2024-2025 估計：主板1.5-2.5億，GEM 6,000萬-1億
# 用保守下限，寧早訊號，自行判斷係咪真殼價回升
MAIN_SHELL_M = 150   # 主板殼價保守下限（百萬）
GEM_SHELL_M  = 60    # 創業板殼價保守下限（百萬）

# ── 止賺門檻 ─────────────────────────────────────────────
# 第一目標：浮盈達此 % → 賣夠回成本，0成本持倉
ZERO_COST_TRIGGER_PCT = 100.0

# 第二目標：市值回升至殼價 + 浮盈>=此% → 賣夠回成本（如未做）
SHELL_RECOVER_PROFIT_PCT = 80.0   # 殼價回升時門檻可低一點

# ── 0成本後 Post-Zero 里程碑 ──────────────────────────────
# 格式: {"mcap_m": 市值門檻(百萬), "gain_pct": 浮盈%門檻(None=不用), "sell_frac": 賣剩餘比例, "label": 標籤}
# M1 雙觸發：市值 OR 浮盈% (任一成立)
# M1 sell_frac=None → 系統提示，賣幾多視乎0成本情況自行決定（建議：鏡像0成本賣出量）
# M2-M5 自動計算 25% 剩餘持股
# 最後持股不設目標，留待 CCASS OUT / 大戶信號手動清

# 主板
# M1 觸發邏輯：(市值≥4億 AND 浮盈≥100%)  OR  浮盈≥200%
# mcap_m + mcap_gain_pct = 市值條件組合；gain_pct = 純浮盈獨立觸發
POST_ZERO_MAIN = [
    {"mcap_m": 400,  "mcap_gain_pct": 100.0, "gain_pct": 200.0, "sell_frac": None, "label": "M1 (4億+100% / 200%)"},
    {"mcap_m": 800,  "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M2 (8億)"},
    {"mcap_m": 1200, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M3 (12億)"},
    {"mcap_m": 1600, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M4 (16億)"},
    {"mcap_m": 2000, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M5 (20億)"},
]
# 每個 M 賣 0成本初始股數 × 20%, M2-M5 合共 80%, 餘 20% 自由操作

# 創業板（主板門檻 × 0.4）
POST_ZERO_GEM = [
    {"mcap_m": 150, "mcap_gain_pct": 100.0, "gain_pct": 200.0, "sell_frac": None, "label": "M1 (1.5億+100% / 200%)"},
    {"mcap_m": 300, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M2 (3億)"},
    {"mcap_m": 450, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M3 (4.5億)"},
    {"mcap_m": 600, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M4 (6億)"},
    {"mcap_m": 750, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.20, "label": "M5 (7.5億)"},
]

# ── 狀態檔案 ─────────────────────────────────────────────
STATE_FILE   = Path(__file__).parent / "data" / "watchlist_state.json"
LOT_CACHE    = Path(__file__).parent / "data" / "hkex_lot_sizes.json"
SCREENER_DB  = Path(__file__).parent / "data" / "screener.db"

# ── CCASS 集中度警戒設定 ──────────────────────────────────
# top10_pct 在最近 N 個交易日內升幅達此門檻 → 觸發 CCASS IN 警示
CCASS_LOOKBACK_ROWS   = 30    # 比較最近多少條歷史記錄
CCASS_CONCENTRATION_THRESHOLD = 5.0   # top10_pct 升幅門檻（百分點）

# ============================================================
# HKEX 每手股數 (Board Lot Size)
# 來源: HKEX Securities List Excel
# ============================================================
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_lot_cache: dict[str, int] = {}  # 程式運行期間快取


def load_lot_cache() -> dict[str, int]:
    global _lot_cache
    if _lot_cache:
        return _lot_cache
    if LOT_CACHE.exists():
        _lot_cache = json.loads(LOT_CACHE.read_text(encoding="utf-8"))
    return _lot_cache


def fetch_hkex_lot_sizes() -> dict[str, int]:
    """
    從 HKEX 官方 Excel 下載全部股票每手股數
    返回 {"0001": 500, "8001": 10000, ...}
    """
    url = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
    print("下載 HKEX Securities List...")
    try:
        import io
        import openpyxl
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
        ws = wb.active

        lot_map: dict[str, int] = {}
        header_row = None
        code_col = lot_col = None

        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if header_row is None:
                # 搵 header row（含 "Stock Code" 同 "Board Lot"）
                row_str = [str(c).lower() if c else "" for c in row]
                if any("stock code" in s for s in row_str):
                    header_row = i
                    code_col = next((j for j, s in enumerate(row_str) if "stock code" in s), None)
                    lot_col  = next((j for j, s in enumerate(row_str) if "board lot" in s), None)
                continue
            if code_col is None or lot_col is None:
                continue
            code_raw = row[code_col]
            lot_raw  = row[lot_col]
            if not code_raw or not lot_raw:
                continue
            try:
                code = str(int(str(code_raw).strip())).zfill(4)
                lot  = int(str(lot_raw).replace(",", "").strip())
                lot_map[code] = lot
            except (ValueError, TypeError):
                continue

        print(f"  取得 {len(lot_map)} 隻股票每手股數")
        LOT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        LOT_CACHE.write_text(json.dumps(lot_map, ensure_ascii=False), encoding="utf-8")
        global _lot_cache
        _lot_cache = lot_map
        return lot_map

    except Exception as e:
        print(f"  [lot_size] 下載失敗: {e}")
        return {}


def get_lot_size(code: str, watchlist_entry: dict | None = None) -> int:
    """
    取得某股票每手股數，優先級:
    1. WATCHLIST 裡手動指定 lot
    2. HKEX 快取
    3. 預設值 2000
    """
    if watchlist_entry and watchlist_entry.get("lot"):
        return int(watchlist_entry["lot"])
    cache = load_lot_cache()
    code4 = str(int(code)).zfill(4)
    return cache.get(code4, 2000)


def round_to_lots(shares: float, lot_size: int, direction: str = "down") -> int:
    """
    將股數四捨五入到整手
    direction: "down"=向下取整(賣出用), "up"=向上取整(買入用)
    """
    if lot_size <= 0:
        return int(shares)
    lots = shares / lot_size
    if direction == "down":
        return math.floor(lots) * lot_size
    else:
        return math.ceil(lots) * lot_size


_yf_session_obj: requests.Session | None = None
_yf_crumb: str | None = None


def _get_yf_crumb() -> tuple[requests.Session, str]:
    """建立 Yahoo Finance crumb session（用 truststore 注入 Windows 系統憑證）"""
    global _yf_session_obj, _yf_crumb
    if _yf_session_obj and _yf_crumb:
        return _yf_session_obj, _yf_crumb
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    s.get("https://fc.yahoo.com", timeout=15)
    r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    crumb = r.text.strip()
    if not crumb or "{" in crumb:
        raise RuntimeError(f"Failed to get Yahoo crumb: {crumb[:80]}")
    _yf_crumb = crumb
    _yf_session_obj = s
    return s, _yf_crumb


def fetch_hk_quote(code: str) -> dict | None:
    """取即時報價 + 市值，使用 Yahoo Finance v7 API + crumb"""
    symbol = f"{int(code):04d}.HK"
    try:
        s, crumb = _get_yf_crumb()
        url = (
            f"https://query1.finance.yahoo.com/v7/finance/quote"
            f"?symbols={symbol}&crumb={crumb}"
        )
        r   = s.get(url, timeout=12, headers={"Accept": "application/json"})
        res = r.json().get("quoteResponse", {}).get("result")
        if not res:
            return None
        q = res[0]
        short = q.get("shortName", symbol)
        long_ = q.get("longName", "")
        # prefer longName if it contains Chinese characters
        name = long_ if long_ and any('一' <= c <= '鿿' for c in long_) else short
        return {
            "symbol": symbol,
            "name":   name,
            "name_en": short,
            "price":  q.get("regularMarketPrice"),
            "chg":    q.get("regularMarketChangePercent", 0),
            "mcap":   q.get("marketCap"),
            "shares": q.get("sharesOutstanding"),
        }
    except Exception as e:
        print(f"  [quote] {symbol} error: {e}")
        return None


def fetch_volume_stats(code: str) -> dict | None:
    """取最近 30 日歷史，返回 vol ratio + 近期高位 + 今日 OHLC 派貨形態 check"""
    symbol = f"{int(code):04d}.HK"
    try:
        s, crumb = _get_yf_crumb()
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?range=1mo&interval=1d&crumb={crumb}"
        )
        r = s.get(url, timeout=12, headers={"Accept": "application/json"})
        chart = r.json().get("chart", {}).get("result")
        if not chart:
            return None
        result = chart[0]
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        vols   = quote.get("volume", [])
        closes = quote.get("close", [])
        opens  = quote.get("open", [])
        highs  = quote.get("high", [])
        lows   = quote.get("low", [])

        # 清走 None
        idx = [i for i in range(len(closes)) if closes[i] is not None]
        if len(idx) < 5:
            return None
        vols   = [vols[i] for i in idx]
        closes = [closes[i] for i in idx]
        opens  = [opens[i] for i in idx]
        highs  = [highs[i] for i in idx]
        lows   = [lows[i] for i in idx]

        today_vol = vols[-1]
        prev_vols = [v for v in vols[:-1] if v is not None and v > 0]
        if not prev_vols or today_vol is None:
            return None
        avg_vol = sum(prev_vols) / len(prev_vols)
        if avg_vol <= 0:
            return None
        vol_ratio = today_vol / avg_vol
        # 近期高位
        recent_high = max(closes[-30:])
        current = closes[-1]
        near_high = current >= recent_high * 0.90

        # A: 長上影線檢測 (今日)
        long_upper_shadow = False
        if all(x is not None for x in [opens[-1], highs[-1], lows[-1], closes[-1]]):
            o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
            upper = h - max(o, c)
            body = abs(c - o) or (h - l) * 0.01  # 避免 div by 0
            day_range = h - l
            # 上影線 > 2x body 同 上影線 > 50% 日內 range
            if day_range > 0 and upper >= body * 2 and upper >= day_range * 0.5:
                long_upper_shadow = True

        # 連續陰燭近高位 (3+ 日)
        red_streak = 0
        for i in range(len(closes) - 1, max(-1, len(closes) - 5), -1):
            if opens[i] is not None and closes[i] is not None and closes[i] < opens[i]:
                red_streak += 1
            else:
                break
        consec_red_near_high = red_streak >= 3 and near_high

        return {
            "vol_ratio": vol_ratio,
            "today_vol": today_vol,
            "avg_vol": avg_vol,
            "near_high": near_high,
            "recent_high": recent_high,
            "current": current,
            "long_upper_shadow": long_upper_shadow,
            "consec_red_near_high": consec_red_near_high,
            "red_streak": red_streak,
        }
    except Exception:
        return None


def fetch_ipo_date(code: str) -> datetime | None:
    """取 IPO 日期 (firstTradeDate from Yahoo). 用 cache 避免重複 fetch."""
    symbol = f"{int(code):04d}.HK"
    try:
        s, crumb = _get_yf_crumb()
        url = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            f"?modules=summaryDetail,price&crumb={crumb}"
        )
        r = s.get(url, timeout=10, headers={"Accept": "application/json"})
        data = r.json().get("quoteSummary", {}).get("result")
        if not data:
            return None
        # firstTradeDate 通常喺 price module 入面，但 quoteSummary 唔一定有
        # 用 quote API 個 firstTradeDateMilliseconds 比較穩定
        url2 = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}&crumb={crumb}"
        r2 = s.get(url2, timeout=10, headers={"Accept": "application/json"})
        q = r2.json().get("quoteResponse", {}).get("result")
        if not q:
            return None
        ms = q[0].get("firstTradeDateMilliseconds")
        if ms is None:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8)))
    except Exception:
        return None


def fetch_insider_transactions(code: str, days: int = 30) -> list[dict] | None:
    """取董事/大股東交易披露 (來自 Yahoo's insiderTransactions module).
    返回最近 days 日內嘅交易 list, 每個 entry 含:
      filerName, filerRelation, shares, value, transactionText, startDate, action
    action: 'buy' / 'sell'
    """
    symbol = f"{int(code):04d}.HK"
    try:
        s, crumb = _get_yf_crumb()
        url = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            f"?modules=insiderTransactions&crumb={crumb}"
        )
        r = s.get(url, timeout=10, headers={"Accept": "application/json"})
        data = r.json().get("quoteSummary", {}).get("result")
        if not data:
            return None
        txs = data[0].get("insiderTransactions", {}).get("transactions", [])
        if not txs:
            return []

        cutoff = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        results = []
        for tx in txs:
            ts = tx.get("startDate", {}).get("raw")
            if ts is None or ts < cutoff_ts:
                continue
            text = (tx.get("transactionText") or "").lower()
            if "disposition" in text or "disposed" in text or "sale" in text or "sold" in text:
                action = "sell"
            elif "acquisition" in text or "acquired" in text or "purchased" in text:
                action = "buy"
            else:
                action = "?"
            results.append({
                "filer":   tx.get("filerName", ""),
                "role":    tx.get("filerRelation", ""),
                "shares":  tx.get("shares", {}).get("raw", 0),
                "value":   tx.get("value", {}).get("raw", 0),
                "text":    tx.get("transactionText", ""),
                "date":    tx.get("startDate", {}).get("fmt", ""),
                "action":  action,
            })
        return results
    except Exception:
        return None


def fetch_debt_ratio(code: str) -> float | None:
    """取負債比率 (debt ratio %)，Yahoo D/E → debt/(debt+equity)"""
    symbol = f"{int(code):04d}.HK"
    try:
        s, crumb = _get_yf_crumb()
        url = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            f"?modules=financialData&crumb={crumb}"
        )
        r = s.get(url, timeout=10, headers={"Accept": "application/json"})
        data = r.json().get("quoteSummary", {}).get("result")
        if not data:
            return None
        fd = data[0].get("financialData", {})
        d2e_raw = fd.get("debtToEquity", {})
        d2e = d2e_raw.get("raw") if isinstance(d2e_raw, dict) else None
        if d2e is not None:
            d2e_dec = d2e / 100
            return d2e_dec / (1 + d2e_dec) * 100
        return None
    except Exception:
        return None


def debt_warning(debt_ratio: float | None, stock_st: dict | None = None) -> str:
    """負債比率警告文字
    規則:
      >125% 有持倉 → 建議平倉; 冇持倉 → 暫停買入
      >100% 有持倉 → 觀察180日, 逾期→強制平倉; 冇持倉 → 暫停買入
      >80%  → 暫停買入
      >60%  → 警告
    好轉 = 跌回 <70% 才清除追蹤
    """
    if debt_ratio is None:
        return ""
    since = stock_st.get("debt_over100_since") if stock_st else None
    has_holdings = False
    if stock_st:
        tr = stock_st.get("tranches", [])
        has_holdings = sum(t.get("shares", 0) for t in tr) > 0
    days_str = ""
    days = 0
    if since:
        try:
            start = datetime.strptime(since, "%Y-%m")
            now = datetime.now()
            days = (now - start.replace(day=1)).days
            days_str = f"（已{days}日）"
        except Exception:
            pass
    if debt_ratio > 125:
        if has_holdings:
            return f"  🔴 負資產! 負債率{debt_ratio:.0f}%{days_str} — 建議平倉"
        return f"  🔴 負資產! 負債率{debt_ratio:.0f}% — 暫停買入"
    if debt_ratio > 100:
        if has_holdings:
            if since and days >= 180:
                return f"  🔴 負資產! 負債率{debt_ratio:.0f}%{days_str} — 逾180日未回落<70% ❗強制平倉"
            return f"  🔴 負資產! 負債率{debt_ratio:.0f}%{days_str} — 觀察中(180日)"
        return f"  🔴 負資產! 負債率{debt_ratio:.0f}% — 暫停買入"
    if debt_ratio > 80:
        if since:
            return f"  🔴 強警告! 負債率{debt_ratio:.0f}%{days_str} — 曾>100%未回<70% · 暫停買入"
        return f"  🔴 強警告! 負債率{debt_ratio:.0f}% — 暫停買入"
    if debt_ratio > 60:
        return f"  ⚠️ 負債警告 負債率{debt_ratio:.0f}%"
    return ""


# ============================================================
# 層位計算
# ============================================================

def get_board(entry: dict | str) -> str:
    """兼容新舊 WATCHLIST 格式"""
    if isinstance(entry, str):
        return entry
    return entry.get("board", "main")


def get_tiers(board: str) -> list[int]:
    """返回對應板塊的觸發市值列表（百萬HKD），由大到小"""
    tiers = MAIN_TIERS_M if board == "main" else GEM_TIERS_M
    return sorted(tiers, reverse=True)


def get_shell_m(board: str, entry: dict | str | None = None) -> int:
    """殼價：WATCHLIST 個別設定 > 板塊預設"""
    if isinstance(entry, dict) and entry.get("shell_m"):
        return int(entry["shell_m"])
    return MAIN_SHELL_M if board == "main" else GEM_SHELL_M


def current_tier_reached(mcap_m: float, board: str) -> int:
    """
    返回已觸發的層數（0 = 未觸發任何層）
    e.g. mcap_m=130, tiers=[200,150,120,100] → 觸發了 200、150 兩層 → 返回 2
    """
    count = 0
    for t in get_tiers(board):
        if mcap_m <= t:
            count += 1
        else:
            break
    return count


def tiers_triggered(mcap_m: float, board: str) -> list[int]:
    """返回所有已觸發層位的市值門檻列表"""
    return [t for t in get_tiers(board) if mcap_m <= t]


# ============================================================
# 平均成本 & 浮盈計算
# ============================================================

def calc_avg_cost(tranches: list[dict]) -> float | None:
    """
    tranches: [{"tier_m": 200, "price": 0.50, "hkd": 10000}, ...]
    返回平均成本/股
    """
    total_hkd    = sum(t["hkd"] for t in tranches)
    total_shares = sum(t["hkd"] / t["price"] for t in tranches if t["price"] > 0)
    if total_shares == 0:
        return None
    return total_hkd / total_shares


def calc_gain_pct(avg_cost: float, current_price: float) -> float:
    if avg_cost <= 0:
        return 0.0
    return (current_price - avg_cost) / avg_cost * 100


# ============================================================
# 狀態追蹤
# ============================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_stock_state(state: dict, code: str) -> dict:
    default = {
        "tier_reached": 0,
        "tranches": [],              # [{"tier_m":200,"price":0.5,"hkd":10000,"date":"..."}]
        "zero_cost_achieved": False, # 已完成回收成本
        "zero_cost_shares": None,    # 0成本後剩餘股數
        "zero_cost_price": None,     # 執行0成本時股價
        "post_zero_done": [],        # 已完成的後市目標 index [0,1,2]
        "notes": [],                 # 手動止損備注
    }
    saved = state.get(code, {})
    # 合並，確保舊state有新欄位
    return {**default, **saved}


# ============================================================
# 止賺邏輯
# ============================================================

def calc_zero_cost_sell(
    tranches: list[dict], current_price: float, lot_size: int = 1
) -> tuple[int, int, float]:
    """
    計算達到0成本需要賣出的股數（向下取整至整手）
    返回: (lots_to_sell_shares, remaining_shares, total_invested)

    注意：向上取整(CEIL)確保回收金額 >= 成本
    """
    total_invested = sum(t["hkd"] for t in tranches)
    total_shares   = sum(
        round_to_lots(t["hkd"] / t["price"], lot_size, "down")
        for t in tranches if t.get("price", 0) > 0
    )
    raw_sell       = total_invested / current_price if current_price > 0 else 0
    shares_to_sell = round_to_lots(raw_sell, lot_size, "up")  # CEIL 確保回收 >= 成本
    remaining      = total_shares - shares_to_sell
    return shares_to_sell, remaining, total_invested


def check_take_profit(
    current_price: float, mcap_m: float, board: str,
    avg_cost: float | None, gain_pct: float,
    tranches: list[dict], stock_st: dict,
    lot_size: int = 1, entry: dict | None = None,
) -> list[dict]:
    """
    返回止賺訊號 list，每個訊號係一個 dict:
      {"type": str, "msg": str, "shares_to_sell": int, "lots_to_sell": int, "hkd_to_receive": float}
    所有股數已對齊至整手
    """
    signals = []
    shell_m = get_shell_m(board, entry)

    if avg_cost is None or not tranches:
        return signals

    zero_done = stock_st.get("zero_cost_achieved", False)

    # ── 階段一：尚未達到0成本 ──────────────────────────────
    if not zero_done:
        sell_shares, remain, total_inv = calc_zero_cost_sell(tranches, current_price, lot_size)
        total_shares = sum(
            round_to_lots(t["hkd"] / t["price"], lot_size, "down")
            for t in tranches if t.get("price", 0) > 0
        )
        # 剩餘只有 1 手 → skip (賣埋會清倉, 無 0成本可言)
        if total_shares <= lot_size:
            return signals
        sell_pct  = sell_shares / total_shares * 100 if total_shares > 0 else 0
        sell_lots = sell_shares // lot_size
        recv_hkd  = sell_shares * current_price

        # 0成本觸發條件 = 與 M1 完全一致，觸發後 M1 自動視為已完成
        # (市值 ≥ M1門檻  AND  浮盈 ≥ 100%)  OR  (浮盈 ≥ 200%)
        m1 = (POST_ZERO_MAIN if board == "main" else POST_ZERO_GEM)[0]
        m1_mcap_ok = mcap_m >= m1["mcap_m"] and gain_pct >= (m1.get("mcap_gain_pct") or 100.0)
        m1_gain_ok = m1["gain_pct"] is not None and gain_pct >= m1["gain_pct"]

        if m1_mcap_ok or m1_gain_ok:
            signals.append({
                "type": "ZERO_COST",
                "sell_lots": sell_lots,
                "sell_shares": sell_shares,
                "recv_hkd": recv_hkd,
                "remain": remain,
                "auto_m1_done": True,
            })

    # ── 階段二：已達0成本，追蹤後市目標 ──────────────────
    else:
        net_inv = sum(t.get("hkd", 0) for t in tranches)
        total_shares_held = sum(t.get("shares", 0) for t in tranches)

        # 若 net_inv > 0 (重新買入後)，唔出 milestone 信號（免費股已被稀釋）
        # 但要 check 新買入嗰部分有冇達到自己嘅0成本條件
        if net_inv > 0:
            zero_date = stock_st.get("zero_cost_date", "")
            pz_tr = [t for t in tranches
                     if t.get("hkd", 0) > 0 and zero_date
                     and t.get("date", "")[:10] > zero_date]
            if pz_tr and current_price > 0:
                pz_inv = sum(t["hkd"] for t in pz_tr)
                pz_shares_raw = sum(t.get("shares", 0) for t in pz_tr)
                pz_avg = pz_inv / pz_shares_raw if pz_shares_raw > 0 else 0
                pz_gain = ((current_price - pz_avg) / pz_avg * 100) if pz_avg > 0 else 0

                m1 = (POST_ZERO_MAIN if board == "main" else POST_ZERO_GEM)[0]
                m1_mcap_ok = mcap_m >= m1["mcap_m"] and pz_gain >= (m1.get("mcap_gain_pct") or 100.0)
                m1_gain_ok = m1["gain_pct"] is not None and pz_gain >= m1["gain_pct"]

                if m1_mcap_ok or m1_gain_ok:
                    # 賣足夠新買股數收回 pz_inv → 新買嗰部分都達0成本
                    raw_sell = pz_inv / current_price
                    sell_shares = round_to_lots(raw_sell, lot_size, "up")
                    sell_shares = min(sell_shares, pz_shares_raw)
                    if sell_shares > 0:
                        sell_lots = sell_shares // lot_size if lot_size > 0 else 0
                        signals.append({
                            "type": "POST_ZERO_REBUY",
                            "sell_lots": sell_lots,
                            "sell_shares": sell_shares,
                            "recv_hkd": sell_shares * current_price,
                            "remain": pz_shares_raw - sell_shares,
                            "pz_inv": pz_inv,
                            "pz_gain": pz_gain,
                        })
            return signals

        zero_shares  = int(stock_st.get("zero_cost_shares") or 0)
        done_indices = stock_st.get("post_zero_done", [])
        milestones   = POST_ZERO_MAIN if board == "main" else POST_ZERO_GEM

        # Backfill: 舊 0成本股冇記初始股數, 用當前 zero_shares 補
        if stock_st.get("zero_cost_initial_shares") is None and zero_shares > 0:
            stock_st["zero_cost_initial_shares"] = zero_shares

        # 若剩餘只有 1 手 (或更少)，唔再出止賺，留尾倉博升幅
        if zero_shares <= lot_size:
            return signals

        # 計算0成本時賣出量（用於M1鏡像建議）
        original_shares = sum(
            round_to_lots(t["hkd"] / t["price"], lot_size, "down")
            for t in tranches if t.get("price", 0) > 0
        )
        zero_sold = max(0, original_shares - zero_shares)

        for i, ms in enumerate(milestones):
            if i in done_indices:
                continue

            # 市值條件：mcap_m + 可選的最低浮盈%
            mcap_gain_req = ms.get("mcap_gain_pct")
            mcap_trigger  = (mcap_m >= ms["mcap_m"] and
                             (mcap_gain_req is None or gain_pct >= mcap_gain_req))
            # 純浮盈獨立觸發
            gain_trigger  = ms["gain_pct"] is not None and gain_pct >= ms["gain_pct"]
            if not (mcap_trigger or gain_trigger):
                continue

            # 決定觸發描述
            triggers = []
            if mcap_trigger:
                gain_cond = f"+浮盈{gain_pct:.0f}%≥{mcap_gain_req:.0f}%" if mcap_gain_req else ""
                triggers.append(f"市值{mcap_m:.0f}M≥{ms['mcap_m']}M{gain_cond}")
            if gain_trigger:
                triggers.append(f"浮盈{gain_pct:.0f}%≥{ms['gain_pct']:.0f}%")
            trigger_desc = " / ".join(triggers)

            sell_frac = ms["sell_frac"]
            if sell_frac is None:
                mirror = round_to_lots(zero_sold, lot_size, "down") or lot_size
                mirror = min(mirror, zero_shares)
                mirror_lots = mirror // lot_size
                signals.append({
                    "type": f"POST_ZERO_{i}",
                    "label": ms["label"],
                    "sell_lots": mirror_lots,
                    "sell_shares": mirror,
                    "recv_hkd": mirror * current_price,
                    "remain": zero_shares - mirror,
                    "milestone_idx": i,
                })
            else:
                # 20% 係 0成本初始股數 × 20% (每個 M 都係固定比例, 5 次共 80%, 餘 20% 自由操作)
                initial_shares = stock_st.get("zero_cost_initial_shares") or zero_shares
                raw_sell    = initial_shares * sell_frac
                shares_sell = round_to_lots(raw_sell, lot_size, "down")
                shares_sell = min(shares_sell, zero_shares)  # cap at remaining
                lots_sell   = shares_sell // lot_size if lot_size > 0 else 0
                if shares_sell <= 0:
                    continue
                signals.append({
                    "type": f"POST_ZERO_{i}",
                    "label": ms["label"],
                    "sell_lots": lots_sell,
                    "sell_shares": shares_sell,
                    "recv_hkd": shares_sell * current_price,
                    "remain": zero_shares - shares_sell,
                    "milestone_idx": i,
                })

    return signals


# ============================================================
# 格式化
# ============================================================

def fmt_m(val_hkd: float) -> str:
    return f"{val_hkd/1e6:.1f}M"


def fmt_hkd(val: float) -> str:
    if val >= 1e8:
        return f"{val/1e8:.2f}億"
    if val >= 1e6:
        return f"{val/1e6:.1f}M"
    return f"${val:,.0f}"


def fmt_mcap(mcap_m: float) -> str:
    """Market cap in 億 HKD"""
    yi = mcap_m / 100
    return f"{yi:.2f}億" if yi < 10 else f"{yi:.1f}億"


BOARD_LABEL = {"main": "主板", "gem": "創業板"}


def _get_shares(tranches: list[dict], lot_size: int) -> int:
    return sum(
        t.get("shares") or round_to_lots(t["hkd"] / t["price"], lot_size, "down")
        for t in tranches if t.get("price", 0) > 0
    )


def build_stock_block(
    code: str, board: str, quote: dict,
    stock_st: dict, shortfall: float,
    tp_signals: list[dict],
    ccass_alerts: list[str] | None = None,
    debt_ratio: float | None = None,
    debt_stale: bool = False,
) -> str:
    mcap_m    = quote["mcap"] / 1e6
    lot_size  = stock_st.get("lot_size", 1)
    price     = quote["price"]
    chg       = quote.get("chg") or 0
    name      = quote["name"]
    tranches  = stock_st.get("tranches", [])
    zero_done = stock_st.get("zero_cost_achieved", False)

    tiers_now = current_tier_reached(mcap_m, board)
    expected  = tiers_now * TRANCHE_SIZE
    actual_inv = sum(t["hkd"] for t in tranches if t.get("hkd", 0) > 0)

    sign = "+" if chg >= 0 else ""
    total_invested = sum(t["hkd"] for t in tranches)
    avg_cost = calc_avg_cost(tranches) if tranches else None
    gain_pct = calc_gain_pct(avg_cost, price) if avg_cost else None

    lines = []
    code4 = f"{int(code):04d}"
    chg_str = f" {sign}{chg:.1f}%" if chg != 0 else ""

    # ── 股票名 ──
    lines.append(f"{code4} {name}")
    # ── 價格 + 市值 ──
    lines.append(f"  ${price:.3f}{chg_str} [{fmt_mcap(mcap_m)}]")

    # ── 持倉 ──
    if zero_done:
        z_shares = stock_st.get("zero_cost_shares", 0) or 0
        z_val = z_shares * price
        # 已套現金額 = abs(淨 hkd) — 若 net_inv < 0 表示套現超出本金
        net_inv = sum(t.get("hkd", 0) for t in tranches)
        if net_inv < 0:
            lines.append(f"  🆓 {z_shares:,}股 值${z_val:,.0f} (已套現+${abs(net_inv):,.0f})")
        else:
            lines.append(f"  🆓 {z_shares:,}股 值${z_val:,.0f} (本金已回收)")
        # 顯示0成本之後嘅新買入
        zero_date = stock_st.get("zero_cost_date", "")
        pz_tr = [t for t in tranches
                 if t.get("hkd", 0) > 0 and zero_date
                 and t.get("date", "")[:10] > zero_date]
        if pz_tr:
            pz_inv = sum(t["hkd"] for t in pz_tr)
            pz_shares = sum(t.get("shares", 0) for t in pz_tr)
            pz_avg = pz_inv / pz_shares if pz_shares > 0 else 0
            pz_val = pz_shares * price
            pz_gain = ((price - pz_avg) / pz_avg * 100) if pz_avg > 0 else 0
            lines.append(f"  ➕ 新買{pz_shares:,}股 @${pz_avg:.3f} 值${pz_val:,.0f} [{pz_gain:+.0f}%]")
    elif tranches:
        shares = _get_shares(tranches, lot_size)
        val = shares * price
        position = max(actual_inv, val)
        lines.append(f"  持{shares:,}股 投${actual_inv:,.0f} 值${val:,.0f}")
        gain_str = f" [{gain_pct:+.0f}%]" if gain_pct is not None else ""
        if expected > 0:
            lines.append(f"  應投${expected:,.0f} 倉位${position:,.0f}{gain_str}")

    # ── 負債警告 ──
    dw = debt_warning(debt_ratio, stock_st)
    if dw:
        lines.append(dw)
    if debt_stale and debt_ratio is not None:
        last_upd = stock_st.get("debt_updated", "?")
        lines.append(f"  ⚠️ 負債數據過時 (上次更新: {last_upd}) — 建倉暫停")

    # ── 建倉訊號（差額補倉）── 不足1手就 skip
    # 負債 block: >100%冇持倉/有持倉>125% → block; >80% → block; 過時 + 冇倉 → block
    has_pos = bool(tranches) and _get_shares(tranches, lot_size) > 0
    if debt_stale and not has_pos:
        debt_block_buy = True
    elif debt_ratio is not None and debt_ratio > 125:
        debt_block_buy = True
    elif debt_ratio is not None and debt_ratio > 100 and not has_pos:
        debt_block_buy = True
    elif debt_ratio is not None and debt_ratio > 80:
        debt_block_buy = True
    else:
        debt_block_buy = False
    if shortfall >= MIN_BUY_HKD and not debt_block_buy:
        est_shares = round_to_lots(shortfall / price, lot_size, "down")
        est_lots   = est_shares // lot_size if lot_size > 0 else 0
        if est_shares > 0:
            if zero_done:
                lines.append(f"  >> 重新建倉 ${shortfall:,.0f} ({est_lots}手/{est_shares:,}股)")
            elif not tranches:
                lines.append(f"  >> 建倉 ${shortfall:,.0f} ({est_lots}手/{est_shares:,}股)")
            else:
                lines.append(f"  >> 補倉 差${shortfall:,.0f} ({est_lots}手/{est_shares:,}股)")
            lines.append(f"  /buy {code} {est_shares} {price}")
    elif shortfall >= MIN_BUY_HKD and debt_block_buy:
        lines.append(f"  ⛔ 負債率{debt_ratio:.0f}%>80% — 暫停買入")

    # ── 止賺訊號 ── sell 0股 skip
    for sig in tp_signals:
        ss = sig.get("sell_shares", 0)
        if ss <= 0:
            continue
        sl = sig.get("sell_lots", 0)
        rv = sig.get("recv_hkd", 0)
        rm = sig.get("remain", 0)
        label = sig.get("label", "")
        if sig["type"] == "ZERO_COST":
            lines.append(f"  >> 賣{sl}手({ss:,}股) 收${rv:,.0f} 剩{rm:,}股0成本")
            lines.append(f"  /sell {code} {ss} {price}")
        elif sig["type"] == "POST_ZERO_REBUY":
            pz_gain = sig.get("pz_gain", 0)
            lines.append(f"  >> 新買部分+{pz_gain:.0f}% 賣{sl}手({ss:,}股) 收${rv:,.0f} 剩{rm:,}股0成本")
            lines.append(f"  /sell {code} {ss} {price}")
        else:
            lines.append(f"  >> {label} 賣{sl}手({ss:,}股) 收${rv:,.0f} 剩{rm:,}股")
            lines.append(f"  /sell {code} {ss} {price}")

    # ── 0成本後市目標 ──
    # 跳過已觸發 + 跳過 mcap 已過嘅 milestone, 顯示真正下一個目標
    if zero_done:
        z_shares = stock_st.get("zero_cost_shares", 0) or 0
        milestones = POST_ZERO_MAIN if board == "main" else POST_ZERO_GEM
        done_idx = set(stock_st.get("post_zero_done", []))
        triggered_idx = {s.get("milestone_idx") for s in tp_signals
                         if s.get("milestone_idx") is not None}
        skip_idx = done_idx | triggered_idx
        # 第一個冇 done 又 mcap 未過嘅 milestone
        next_ms = None
        for i, ms in enumerate(milestones):
            if i in skip_idx:
                continue
            if mcap_m < ms["mcap_m"]:
                next_ms = ms
                break
        if next_ms and z_shares > lot_size:
            lines.append(f"  → 下一目標 {next_ms['label']}")

    # ── CCASS + 備注 ──
    for a in (ccass_alerts or []):
        lines.append(f"  ! {a}")
    for n in stock_st.get("notes", []):
        lines.append(f"  - {n}")

    return "\n".join(lines)


# ============================================================
# CCASS 集中度自動警示
# ============================================================

def check_ccass_concentration(
    code: str,
    lookback_rows: int = CCASS_LOOKBACK_ROWS,
    threshold_pct: float = CCASS_CONCENTRATION_THRESHOLD,
) -> str | None:
    """
    查詢 screener.db ccass_history，偵測雙向異動:
      - CCASS IN: top10集中度急升 (籌碼集中, 收集信號)
      - CCASS OUT: top10集中度急跌 (派貨信號)
      - Broker SURGE: 券商數目急增 (派發到散戶)
      - Broker DROP: 券商數目急減 (籌碼集中)
    閾值: lookback 期間變動 >= threshold_pct
    """
    if not SCREENER_DB.exists():
        return None

    db_code = str(int(code))

    try:
        conn = sqlite3.connect(str(SCREENER_DB))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT snapshot_date, top10_pct, top1_pct, broker_count
                FROM ccass_history
                WHERE code = ?
                ORDER BY snapshot_date DESC
                LIMIT ?
                """,
                (db_code, lookback_rows),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
    except Exception:
        return None

    if len(rows) < 2:
        return None

    recent = rows[0]
    oldest = rows[-1]

    recent_pct = recent["top10_pct"]
    old_pct    = oldest["top10_pct"]
    recent_brk = recent["broker_count"]
    old_brk    = oldest["broker_count"]
    date_range = f"{oldest['snapshot_date']}→{recent['snapshot_date']}"

    # Top10 集中度雙向 check
    if recent_pct is not None and old_pct is not None:
        delta = recent_pct - old_pct
        if delta >= threshold_pct:
            return (
                f"[CCASS IN] top10集中度 +{delta:.1f}% "
                f"({old_pct:.1f}%→{recent_pct:.1f}%, {date_range}, broker={recent_brk})"
            )
        if delta <= -threshold_pct:
            return (
                f"[CCASS OUT] top10集中度 {delta:.1f}% "
                f"({old_pct:.1f}%→{recent_pct:.1f}%, {date_range}, broker={recent_brk}) — 派貨"
            )

    # Broker 數目異動 (大於 30% 變動)
    if recent_brk and old_brk and old_brk > 0:
        brk_delta_pct = (recent_brk - old_brk) / old_brk * 100
        if brk_delta_pct >= 30:
            return (
                f"[Broker SURGE] 券商數+{brk_delta_pct:.0f}% "
                f"({old_brk}→{recent_brk}, {date_range}) — 派散戶信號"
            )
        if brk_delta_pct <= -30:
            return (
                f"[Broker DROP] 券商數{brk_delta_pct:.0f}% "
                f"({old_brk}→{recent_brk}, {date_range}) — 籌碼集中"
            )

    return None


# ============================================================
# 主報告
# ============================================================

def _compute_dynamic_tranche(state: dict) -> int:
    """TRANCHE_SIZE = (cash + market value) / 100, round to nearest $100.
    Uses last_price 數據, 即上次 alert 嘅 cached price. 第一次 fresh state 用 default.
    """
    total_inv = 0
    total_val = 0
    cleared_pnl = 0
    for code, st in state.items():
        if code.startswith("_"):
            continue
        tr = st.get("tranches", [])
        if not tr:
            if st.get("cleared") or st.get("realized_pnl"):
                cleared_pnl += st.get("realized_pnl", 0)
            continue
        total_inv += sum(t.get("hkd", 0) for t in tr)
        shares = sum(t.get("shares", 0) for t in tr)
        price = st.get("last_price") or 0
        total_val += shares * price
    cash = TOTAL_PORTFOLIO - total_inv + cleared_pnl
    total_wealth = cash + total_val
    if total_wealth <= 0:
        return 6000  # fallback
    return max(100, round(total_wealth / 10000) * 100)


def monitor_report(alert_only: bool = False) -> str:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    state = load_state()
    new_state = dict(state)

    # Dynamic TRANCHE: 用 state 入面 last_price 算 total wealth, TRANCHE = TOTAL/100
    global TRANCHE_SIZE
    new_tranche = _compute_dynamic_tranche(state)
    if new_tranche != TRANCHE_SIZE:
        TRANCHE_SIZE = new_tranche

    all_blocks       = []
    buy_blocks       = []   # 建倉信號
    sell_blocks      = []   # 止賺信號 (有 /sell 指令)
    debt_warn_blocks = []   # 負債警告 (有持倉, dr>60%, 冇 sell 指令)
    anomaly_blocks   = []   # 異常動向 (vol spike 但冇其他信號)
    no_data       = []
    total_buy_recommend = 0.0  # 全部建議買入金額

    load_lot_cache()  # 預先載入每手快取

    # 從 state.json 讀 watchlist（每隻股有 board 欄位）
    watchlist = {code: st for code, st in state.items() if st.get("board")}

    for code, entry in watchlist.items():
        board    = entry.get("board", "main")
        lot_size = get_lot_size(code, entry)

        quote = fetch_hk_quote(code)
        time.sleep(0.2)

        if not quote or not quote["mcap"] or not quote["price"]:
            no_data.append(code)
            continue

        mcap_m   = quote["mcap"] / 1e6
        price    = quote["price"]
        tiers    = get_tiers(board)
        shell_m  = get_shell_m(board, entry)
        stock_st = get_stock_state(state, code)

        tranches = stock_st.get("tranches", [])
        zero_done = stock_st.get("zero_cost_achieved", False)

        # ── 建倉差額：應投入 vs max(投入, 現值) ──
        tiers_now    = current_tier_reached(mcap_m, board)
        actual_inv   = sum(t["hkd"] for t in tranches if t.get("hkd", 0) > 0)
        shares_held  = sum(t.get("shares", 0) for t in tranches)
        current_val  = shares_held * price if shares_held > 0 else 0
        if zero_done:
            # 0成本股：用 zero_cost_tier 做 floor (執行0成本嗰刻嘅 tier)
            # 必須跌穿 zero_cost_tier 先重新建倉, 避免循環sell-buy
            zero_tier = stock_st.get("zero_cost_tier")
            if zero_tier is None or zero_tier == 0:
                # Backfill / Migration: 至少鎖喺 tier 1，避免太寬鬆觸發
                zero_tier = max(1, tiers_now)
                stock_st["zero_cost_tier"] = zero_tier
            effective_tiers = max(0, tiers_now - zero_tier)
            expected_inv    = effective_tiers * TRANCHE_SIZE
            # 計算0成本之後嘅新買入 (post-zero buys)
            zero_date = stock_st.get("zero_cost_date", "")
            post_zero_inv = sum(
                t.get("hkd", 0) for t in tranches
                if t.get("hkd", 0) > 0 and zero_date and t.get("date", "")[:10] > zero_date
            )
            position  = post_zero_inv
            shortfall = max(0, expected_inv - position) if expected_inv > 0 else 0
        else:
            expected_inv = tiers_now * TRANCHE_SIZE
            position     = max(actual_inv, current_val)
            shortfall    = max(0, expected_inv - position) if expected_inv > 0 else 0

        avg_cost = calc_avg_cost(tranches) if tranches else None
        gain_pct = calc_gain_pct(avg_cost, price) if avg_cost else 0.0

        tp_signals = check_take_profit(
            price, mcap_m, board, avg_cost, gain_pct,
            tranches, stock_st, lot_size,
            entry,
        )

        # CCASS 集中度自動警示（唔寫入 state，每次動態查詢）
        ccass_alert = check_ccass_concentration(code)
        ccass_alerts = [ccass_alert] if ccass_alert else []

        # 成交量異常偵測 + 派貨形態 (只查有持倉嘅股票, 省 API call)
        vol_alert_str = None
        vol_stats = None
        if tranches or zero_done:
            vol_stats = fetch_volume_stats(code)
            time.sleep(0.15)
            if vol_stats:
                # 異常成交
                if vol_stats["vol_ratio"] >= 3.0:
                    pos_str = " 高位" if vol_stats["near_high"] else ""
                    vol_alert_str = f"📊 異常成交{pos_str} ({vol_stats['vol_ratio']:.1f}x 30日均)"
                    ccass_alerts.append(vol_alert_str)
                # A: 長上影線 + 高位 = 莊家出貨蠟燭
                if vol_stats.get("long_upper_shadow") and vol_stats["near_high"]:
                    ccass_alerts.append("🕯️ 長上影線高位 — 莊家出貨蠟燭")
                # F: 連續陰燭近高位
                if vol_stats.get("consec_red_near_high"):
                    rs = vol_stats.get("red_streak", 3)
                    ccass_alerts.append(f"📉 高位連{rs}日陰燭 — 逐步派發")

        # 董事/大股東交易披露 (最近 30 日 + 連環減持 detect)
        insider_alert_str = None
        insider_sell_recent = False
        if tranches or zero_done:
            insider_txs = fetch_insider_transactions(code, days=30)
            time.sleep(0.15)
            if insider_txs:
                sells = [t for t in insider_txs if t["action"] == "sell"]
                buys = [t for t in insider_txs if t["action"] == "buy"]
                # C: 連環減持 - 10 日內 ≥3 次 SDI 減持
                cutoff_10d = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=10)
                recent_sells = []
                for t in sells:
                    try:
                        sell_dt = datetime.strptime(t["date"], "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
                        if sell_dt >= cutoff_10d:
                            recent_sells.append(t)
                    except (ValueError, TypeError):
                        pass
                if len(recent_sells) >= 3:
                    total_recent_shares = sum(t["shares"] for t in recent_sells)
                    ccass_alerts.append(
                        f"⛔ 連環減持: 10日內{len(recent_sells)}次 SDI 賣出 "
                        f"共{total_recent_shares:,}股 — 強烈派貨"
                    )
                    insider_sell_recent = True
                elif sells:
                    total_sell_shares = sum(t["shares"] for t in sells)
                    first = sells[0]
                    insider_alert_str = (
                        f"📋 SDI 減持 ({first['date']}): {first['role']} 賣 "
                        f"{total_sell_shares:,}股"
                    )
                    insider_sell_recent = True
                    ccass_alerts.append(insider_alert_str)
                elif buys:
                    total_buy_shares = sum(t["shares"] for t in buys)
                    first = buys[0]
                    insider_alert_str = (
                        f"📋 SDI 增持 ({first['date']}): {first['role']} 買 "
                        f"{total_buy_shares:,}股"
                    )
                    ccass_alerts.append(insider_alert_str)

        # E: IPO 解禁日警示 (6m / 12m / 24m)
        ipo_dt = stock_st.get("ipo_date")
        if not ipo_dt:
            # Fetch only if not cached
            ipo_obj = fetch_ipo_date(code)
            if ipo_obj:
                stock_st["ipo_date"] = ipo_obj.strftime("%Y-%m-%d")
                ipo_dt = stock_st["ipo_date"]
                time.sleep(0.15)
        if ipo_dt:
            try:
                ipo_d = datetime.strptime(ipo_dt, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
                now_d = datetime.now(timezone(timedelta(hours=8)))
                days_since = (now_d - ipo_d).days
                # 6m = 180, 12m = 365, 24m = 730. Alert window = +/- 30 日
                for milestone, label in [(180, "6個月"), (365, "12個月"), (730, "24個月")]:
                    if abs(days_since - milestone) <= 30:
                        ccass_alerts.append(
                            f"🔓 IPO {label}解禁期 (上市{days_since}日) — 大股東可大手減持"
                        )
                        break
            except (ValueError, TypeError):
                pass

        # 🚨 高危派貨複合信號: 高位 + 大成交 + 高gain + (SDI減持 OR vol spike)
        # 識別「派貨進行中」嘅多重證據
        if tranches and shares_held > 0:
            high_risk_flags = []
            if vol_stats and vol_stats.get("near_high") and vol_stats.get("vol_ratio", 0) >= 2.0:
                high_risk_flags.append("高位+大成交")
            if insider_sell_recent:
                high_risk_flags.append("董事減持")
            if gain_pct is not None and gain_pct >= 100:
                high_risk_flags.append(f"+{gain_pct:.0f}%獲利")
            # 至少 2 個 flag 先觸發 (避免 false alarm)
            if len(high_risk_flags) >= 2:
                ccass_alerts.append(
                    f"🚨 高危派貨警示: {' + '.join(high_risk_flags)}"
                )

        # 負債比率（只 check 有持倉嘅股票，省 API call）
        dr = None
        dr_is_fresh = False
        if tranches or zero_done:
            dr = fetch_debt_ratio(code)
            time.sleep(0.15)
            if dr is not None:
                dr_is_fresh = True
        # Fallback: 若 API 失敗，用 state 中上次記錄嘅負債率（可能過時）
        if dr is None and stock_st.get("debt_ratio") is not None:
            dr = stock_st["debt_ratio"]

        stock_st["lot_size"]     = lot_size
        stock_st["last_mcap_m"]  = round(mcap_m, 2)
        stock_st["last_price"]   = price
        stock_st["last_check"]   = now
        if dr is not None:
            stock_st["debt_ratio"] = round(dr, 1)
            if dr_is_fresh:
                stock_st["debt_updated"] = datetime.now().strftime("%Y-%m-%d")
            if dr > 100:
                if not stock_st.get("debt_over100_since"):
                    stock_st["debt_over100_since"] = datetime.now().strftime("%Y-%m")
            elif dr < 70:
                # 好轉 = 跌回70%以下才清除追蹤
                stock_st.pop("debt_over100_since", None)

        # 檢查 debt 數據新鮮度 - > 60日視為過時
        dr_stale = False
        debt_updated = stock_st.get("debt_updated")
        if dr is not None and not dr_is_fresh and debt_updated:
            try:
                last_upd = datetime.strptime(debt_updated, "%Y-%m-%d")
                if (datetime.now() - last_upd).days > 60:
                    dr_stale = True
            except (ValueError, TypeError):
                pass
        new_state[code] = stock_st

        # 計算實際可買股數，用嚟決定係咪出建倉信號
        buy_shares = round_to_lots(shortfall / price, lot_size, "down") if (shortfall > 0 and price > 0) else 0
        # 過濾 sell 0股 嘅 tp_signals
        valid_tp = [s for s in tp_signals if s.get("sell_shares", 0) > 0]

        block = build_stock_block(code, board, quote, stock_st, shortfall, tp_signals, ccass_alerts, dr, dr_stale)
        all_blocks.append(block)

        # ── 分類: sell / debt_warn / buy ──
        has_pos = shares_held > 0

        # debt_block 邏輯（過時數據 → 保守暫停買入）
        if dr_stale and has_pos:
            debt_block = False  # 有倉時，過時數據唔阻買，只警告
        elif dr_stale:
            debt_block = True   # 新建倉時，過時數據暫停買入
        elif dr is not None and dr > 125:
            debt_block = True
        elif dr is not None and dr > 100 and not has_pos:
            debt_block = True
        elif dr is not None and dr > 80:
            debt_block = True
        else:
            debt_block = False

        # Fix #4: 新建倉/0成本重新建倉 最多2層 ($12,000)，避免一次落重注
        # 細於 MIN_BUY_HKD 嘅 shortfall 唔出買信號
        MAX_NEW_BUY = TRANCHE_SIZE * 2  # $12,000
        if shortfall >= MIN_BUY_HKD and buy_shares > 0 and not debt_block:
            final_buy_hkd = shortfall
            if not tranches or zero_done:
                # 新建倉 / 0成本重新建倉: cap shortfall
                capped = min(shortfall, MAX_NEW_BUY)
                capped_shares = round_to_lots(capped / price, lot_size, "down") if price > 0 else 0
                if capped_shares > 0 and capped_shares != buy_shares:
                    block = build_stock_block(code, board, quote, stock_st, capped, tp_signals, ccass_alerts, dr, dr_stale)
                    final_buy_hkd = capped_shares * price
                else:
                    final_buy_hkd = capped
            else:
                final_buy_hkd = buy_shares * price
            buy_blocks.append(block)
            total_buy_recommend += final_buy_hkd

        # 分類 alert: sell trigger vs anomaly only
        # sell trigger: 減持 / CCASS OUT / Broker SURGE (派貨類)
        # anomaly only: vol spike, 增持, CCASS IN, Broker DROP (中性/利好類)
        def _is_sell_trigger(a: str) -> bool:
            return any(k in a for k in [
                "減持", "CCASS OUT", "Broker SURGE", "派貨", "派散戶",
                "🚨", "⛔ 連環減持", "🕯️", "📉 高位連", "🔓 IPO"
            ])
        sell_trigger_alerts = [a for a in ccass_alerts if _is_sell_trigger(a)]
        anomaly_only_alerts = [a for a in ccass_alerts if not _is_sell_trigger(a)]

        # 真正止賺信號 — 必須有持倉先有意義
        if has_pos and (valid_tp or sell_trigger_alerts):
            sell_blocks.append(block)
        if dr is not None and dr > 60 and has_pos:
            debt_warn_blocks.append(block)

        # 異常動向: 1) watch-only 嘅 sell trigger alerts (冇 /sell 命令)
        #          2) anomaly only alerts + 唔出現喺其他 section
        in_sell_section = has_pos and (valid_tp or sell_trigger_alerts)
        in_buy_section  = shortfall >= MIN_BUY_HKD and buy_shares > 0 and not debt_block
        in_debt_section = dr is not None and dr > 60 and has_pos
        in_other_section = in_sell_section or in_buy_section or in_debt_section
        # Watch-only stock with sell trigger → 異常動向 (informational)
        watch_only_with_alert = not has_pos and (sell_trigger_alerts or anomaly_only_alerts)
        if (anomaly_only_alerts and not in_other_section) or watch_only_with_alert:
            anomaly_blocks.append(block)

    # ── Auto-fix: 淨投入 ≤ 0 但未標0成本 → 自動補標 ──
    for code_fix, st_fix in new_state.items():
        tr_fix = st_fix.get("tranches", [])
        if not tr_fix:
            continue
        sh_fix = sum(t.get("shares", 0) for t in tr_fix)
        inv_fix = sum(t.get("hkd", 0) for t in tr_fix)
        if sh_fix > 0 and inv_fix <= 0 and not st_fix.get("zero_cost_achieved"):
            st_fix["zero_cost_achieved"] = True
            st_fix["zero_cost_shares"] = sh_fix
            st_fix["zero_cost_initial_shares"] = sh_fix  # 記低 M1 達成時嘅初始免費股數
            if not st_fix.get("zero_cost_date"):
                st_fix["zero_cost_date"] = now[:10]
            # 鎖定 zero_cost_tier (建倉 floor)，至少 tier 1
            mcap_fix = st_fix.get("last_mcap_m")
            board_fix = st_fix.get("board")
            if mcap_fix is not None and board_fix:
                st_fix["zero_cost_tier"] = max(1, current_tier_reached(mcap_fix, board_fix))

    # save_state moved to after signal tracking (below)

    # ── 持倉總覽 ──
    total_inv = total_val = 0.0
    cleared_realized_pnl = 0.0  # 已清倉股票嘅累計 realized profit
    n_holdings = n_zero = n_can_zero = 0
    for code2, st2 in new_state.items():
        tr = st2.get("tranches", [])
        if not tr:
            # 已清倉股票: 加埋 realized_pnl 入返 cash
            if st2.get("cleared") or st2.get("realized_pnl"):
                cleared_realized_pnl += st2.get("realized_pnl", 0)
            continue
        n_holdings += 1
        inv = sum(t["hkd"] for t in tr)
        total_inv += inv
        p2 = st2.get("last_price") or 0
        ls = st2.get("lot_size", 1)
        shares2 = _get_shares(tr, ls)
        if st2.get("zero_cost_achieved"):
            n_zero += 1
            val2 = (st2.get("zero_cost_shares") or 0) * p2
        else:
            val2 = shares2 * p2
            avg2 = calc_avg_cost(tr)
            g2 = calc_gain_pct(avg2, p2) if avg2 and p2 else 0
            mcap2 = st2.get("last_mcap_m", 0)
            board2 = new_state.get(code2, {}).get("board", "main")
            m1 = (POST_ZERO_MAIN if board2 == "main" else POST_ZERO_GEM)[0]
            if (g2 >= (m1.get("gain_pct") or 999)) or \
               (mcap2 >= m1["mcap_m"] and g2 >= (m1.get("mcap_gain_pct") or 100)):
                n_can_zero += 1
        total_val += val2

    total_gain     = total_val - total_inv
    gain_pct_total = total_gain / total_inv * 100 if total_inv > 0 else 0
    gs             = "+" if total_gain >= 0 else ""
    # 可用現金 = 初始現金 - 淨投入活躍倉位 + 已清倉累計 realized 利潤
    cash_est       = TOTAL_PORTFOLIO - total_inv + cleared_realized_pnl
    cash_pct       = cash_est / TOTAL_PORTFOLIO * 100 if TOTAL_PORTFOLIO > 0 else 0
    cash_ok        = cash_pct >= MIN_CASH_PCT * 100
    cash_icon      = "OK" if cash_ok else "LOW"
    deploy_limit   = TOTAL_PORTFOLIO * (1 - MIN_CASH_PCT)

    sep = "\n"

    zero_str = f"已0成本{n_zero}隻"
    if n_can_zero:
        zero_str += f" 可0成本{n_can_zero}隻"

    summary = (
        f"Watchlist {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"持倉{n_holdings}隻 | {zero_str}\n"
        f"已投${total_inv:,.0f} 現值${total_val:,.0f}\n"
        f"浮盈{gs}${total_gain:,.0f} ({gs}{gain_pct_total:.0f}%)\n"
        f"現金{cash_pct:.0f}% [{cash_icon}] "
        f"可用${cash_est:,.0f}/${TOTAL_PORTFOLIO:,.0f}\n"
        f"每注${TRANCHE_SIZE:,} (TOTAL/100)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    if not cash_ok:
        summary += f"\n⚠ 現金低於{MIN_CASH_PCT*100:.0f}%! 暫停買入"

    # ── Data integrity check ──
    integrity_warns = []
    for code3, st3 in new_state.items():
        tr3 = st3.get("tranches", [])
        if not tr3:
            continue
        s3 = sum(t.get("shares", 0) for t in tr3)
        lot3 = st3.get("lot_size", 1)
        if lot3 > 1 and s3 > 0 and s3 % lot3 != 0:
            integrity_warns.append(f"  {int(code3):04d}: {s3:,}股 唔係{lot3:,}整手")
    if integrity_warns:
        summary += "\n\n⚠ 數據異常:\n" + "\n".join(integrity_warns)

    dash = "-------------------"

    # Fix #8: 標記新信號 — 比對上次 alert 嘅信號 list
    last_sell_codes = set(new_state.get("_meta", {}).get("last_sell_codes", []))
    last_buy_codes  = set(new_state.get("_meta", {}).get("last_buy_codes", []))

    def _extract_code(block: str) -> str:
        """從 block 第一行提取股票代碼"""
        first = block.split("\n", 1)[0].strip()
        return first.split(" ")[0] if first else ""

    cur_sell_codes = [_extract_code(b) for b in sell_blocks]
    cur_buy_codes  = [_extract_code(b) for b in buy_blocks]

    def _numbered(blocks, prev_codes=None):
        out = []
        for i, b in enumerate(blocks, 1):
            first_line, rest = b.split("\n", 1)
            code_str = first_line.strip().split(" ")[0]
            new_tag = " 🆕" if prev_codes is not None and code_str not in prev_codes else ""
            out.append(f"{i}. {first_line}{new_tag}\n{rest}")
        return f"\n{dash}\n".join(out)

    # Save current signal codes + dynamic TRANCHE for dashboard
    meta = new_state.get("_meta", {})
    meta["last_sell_codes"] = cur_sell_codes
    meta["last_buy_codes"]  = cur_buy_codes
    meta["tranche_size"]    = TRANCHE_SIZE
    new_state["_meta"] = meta
    save_state(new_state)

    signal_blocks = sell_blocks + buy_blocks + debt_warn_blocks + anomaly_blocks

    # 建議買入總額 vs 可用現金
    budget_summary = ""
    if total_buy_recommend > 0:
        budget_icon = "OK" if total_buy_recommend <= cash_est else "⚠️ 超出"
        budget_summary = (
            f"\n建議買入總額: ${total_buy_recommend:,.0f} / 可用 ${cash_est:,.0f} [{budget_icon}]"
        )
        if total_buy_recommend > cash_est:
            budget_summary += "\n→ 現金不足，請選擇優先股票"

    if alert_only:
        if not sell_blocks and not buy_blocks and not debt_warn_blocks and not anomaly_blocks:
            return f"{summary}\n\n暫無新訊號"
        msg = summary
        if sell_blocks:
            msg += f"\n\n止賺信號 ({len(sell_blocks)}隻)\n{dash}\n" + _numbered(sell_blocks, last_sell_codes)
        if buy_blocks:
            msg += f"\n\n建倉信號 ({len(buy_blocks)}隻)\n{dash}\n" + _numbered(buy_blocks, last_buy_codes)
            msg += budget_summary
        if debt_warn_blocks:
            msg += f"\n\n⚠ 負債關注 ({len(debt_warn_blocks)}隻)\n{dash}\n" + _numbered(debt_warn_blocks)
        if anomaly_blocks:
            msg += f"\n\n📊 異常動向 ({len(anomaly_blocks)}隻)\n{dash}\n" + _numbered(anomaly_blocks)
        return msg

    # ── Full report: signals first, then others ──
    parts = [summary]
    if sell_blocks:
        parts.append(f"\n止賺信號 ({len(sell_blocks)}隻)\n{dash}")
        parts.append(_numbered(sell_blocks, last_sell_codes))
    if buy_blocks:
        parts.append(f"\n建倉信號 ({len(buy_blocks)}隻)\n{dash}")
        parts.append(_numbered(buy_blocks, last_buy_codes))
        if budget_summary:
            parts.append(budget_summary.lstrip("\n"))
    if debt_warn_blocks:
        parts.append(f"\n⚠ 負債關注 ({len(debt_warn_blocks)}隻)\n{dash}")
        parts.append(_numbered(debt_warn_blocks))
    if anomaly_blocks:
        parts.append(f"\n📊 異常動向 ({len(anomaly_blocks)}隻)\n{dash}")
        parts.append(_numbered(anomaly_blocks))
    if sell_blocks or buy_blocks or debt_warn_blocks or anomaly_blocks:
        parts.append("━━━━━━━━━━━━━━━━━━━━")

    quiet = [b for b in all_blocks if b not in signal_blocks]
    if quiet:
        parts.append("其餘持倉")
        parts.extend(quiet)

    if no_data:
        parts.append(f"\n[無數據] {', '.join(no_data)}")
    return sep.join(parts)


# ============================================================
# 手動記錄買入 / 更新成本（CLI 工具）
# ============================================================

def record_buy(code: str, price: float, hkd: float, tier_m: int):
    """
    手動記錄一筆買入，用於修正自動記錄的入場價
    python hk_watchlist_monitor.py record 1234 0.50 10000 200
    """
    state    = load_state()
    stock_st = get_stock_state(state, code)
    stock_st.setdefault("tranches", []).append({
        "tier_m": tier_m,
        "price":  price,
        "hkd":    hkd,
        "date":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "manual": True,
    })
    state[code] = stock_st
    save_state(state)
    avg = calc_avg_cost(stock_st["tranches"])
    print(f"[記錄] {int(code):04d}.HK  入場價={price}  金額={hkd}  更新後平均成本={avg:.4f}")


def add_note(code: str, note: str):
    """
    加止損備注（CCASS IN / 大股東減持 / 異常盤路）
    python hk_watchlist_monitor.py note 1234 "CCASS IN 發現集中持倉"
    """
    state    = load_state()
    stock_st = get_stock_state(state, code)
    ts       = datetime.now().strftime("%m-%d")
    stock_st.setdefault("notes", []).append(f"[{ts}] {note}")
    state[code] = stock_st
    save_state(state)
    print(f"[備注] {int(code):04d}.HK  已加: {note}")


def mark_zero_cost(code: str, remaining_shares: float, sell_price: float):
    """
    標記已完成0成本操作，記錄剩餘股數
    python hk_watchlist_monitor.py zerocost 1234 114286 0.35
    """
    state    = load_state()
    stock_st = get_stock_state(state, code)
    tranches = stock_st.get("tranches", [])
    total_inv = sum(t["hkd"] for t in tranches)

    stock_st["zero_cost_achieved"] = True
    stock_st["zero_cost_shares"]   = remaining_shares
    stock_st["zero_cost_initial_shares"] = remaining_shares
    stock_st["zero_cost_price"]    = sell_price
    stock_st["zero_cost_date"]     = datetime.now().strftime("%Y-%m-%d")
    # 鎖定 zero_cost_tier (建倉 floor)
    mcap_now = stock_st.get("last_mcap_m")
    board_now = stock_st.get("board")
    if mcap_now is not None and board_now:
        stock_st["zero_cost_tier"] = current_tier_reached(mcap_now, board_now)
    # 0成本觸發條件 = M1 條件，故 M1 視為同時完成
    done = stock_st.get("post_zero_done", [])
    if 0 not in done:
        done = [0] + done
    stock_st["post_zero_done"] = done
    state[code] = stock_st
    save_state(state)
    val = remaining_shares * sell_price
    print(
        f"[0成本+M1] {int(code):04d}.HK  剩餘 {remaining_shares:,.0f} 股  "
        f"@ {sell_price}  市值 HKD {val:,.0f}  (已回收 HKD {total_inv:,.0f})  M1 自動done"
    )


def mark_post_zero(code: str, milestone_idx: int):
    """
    標記後市目標已執行
    python hk_watchlist_monitor.py postzero 1234 0   (第1個里程碑 index=0)
    """
    state    = load_state()
    stock_st = get_stock_state(state, code)
    done     = stock_st.get("post_zero_done", [])
    if milestone_idx not in done:
        done.append(milestone_idx)
    stock_st["post_zero_done"] = done

    # 更新剩餘股數
    zero_shares = stock_st.get("zero_cost_shares", 0) or 0
    if milestone_idx < len(POST_ZERO_MILESTONES):
        _, frac, label = POST_ZERO_MILESTONES[milestone_idx]
        sold = zero_shares * frac
        stock_st["zero_cost_shares"] = zero_shares - sold
        print(f"[後市止賺] {int(code):04d}.HK  {label} 執行  賣 {sold:,.0f} 股  剩餘 {stock_st['zero_cost_shares']:,.0f} 股")

    state[code] = stock_st
    save_state(state)


# ============================================================
# 資金計劃打印
# ============================================================

def print_allocation_plan():
    print(f"\n{'='*60}")
    print(f"  資金分配計劃  (每注: HKD {TRANCHE_SIZE:,})")
    print(f"{'='*60}")

    for board, tiers, shell in [
        ("主板",   MAIN_TIERS_M, MAIN_SHELL_M),
        ("創業板", GEM_TIERS_M,  GEM_SHELL_M),
    ]:
        tiers_sorted = sorted(tiers, reverse=True)
        total = TRANCHE_SIZE * len(tiers_sorted)
        print(f"\n  [{board}]  最大持倉: HKD {total:,}  |  殼價參考: {shell}M")
        print(f"  {'層':<4} {'市值門檻':<12} {'買入':<12} {'累計投入':<12}")
        print(f"  {'-'*45}")
        cumulative = 0
        for i, t in enumerate(tiers_sorted):
            cumulative += TRANCHE_SIZE
            print(f"  第{i+1}層  < {t}M HKD      HKD {TRANCHE_SIZE:>8,}    HKD {cumulative:>8,}")

    print(f"\n  止賺規則:")
    print(f"    浮盈 >= {ZERO_COST_TRIGGER_PCT:.0f}%              → 賣剛好夠回成本的股數 → 0成本持倉")
    print(f"    市值回升殼價 + 浮盈>={SHELL_RECOVER_PROFIT_PCT:.0f}%  → 同上，賣夠回成本 → 0成本持倉")
    def _ms_trigger_str(ms):
        parts = []
        mcap_g = ms.get("mcap_gain_pct")
        if mcap_g:
            parts.append(f"市值≥{ms['mcap_m']}M+浮盈≥{mcap_g:.0f}%")
        else:
            parts.append(f"市值≥{ms['mcap_m']}M")
        if ms["gain_pct"]:
            parts.append(f"浮盈≥{ms['gain_pct']:.0f}%")
        return " / ".join(parts)

    print(f"\n  0成本後後市目標 [主板]:")
    for ms in POST_ZERO_MAIN:
        sell_str = "自行決定(建議鏡像0成本賣出量)" if ms["sell_frac"] is None else f"賣 {ms['sell_frac']*100:.0f}% 剩餘持股"
        print(f"    {ms['label']:30s}  {_ms_trigger_str(ms)}  → {sell_str}")
    print(f"    最後持股  → 持有，等 CCASS OUT / 大戶操控信號")
    print(f"\n  0成本後後市目標 [創業板]:")
    for ms in POST_ZERO_GEM:
        sell_str = "自行決定(建議鏡像0成本賣出量)" if ms["sell_frac"] is None else f"賣 {ms['sell_frac']*100:.0f}% 剩餘持股"
        print(f"    {ms['label']:30s}  {_ms_trigger_str(ms)}  → {sell_str}")
    print(f"{'='*60}\n")


# ============================================================
# Intraday Alert（交易時段即時觸發）
# ============================================================

def intraday_alert() -> str | None:
    """
    輕量級即時 check — 只取價格+市值，比對 state 中已知層位。
    有新觸發（買入/止賺）先出 TG，冇就 silent。
    唔做 CCASS、負債 check（留俾每朝 full report）。
    """
    hkt = timezone(timedelta(hours=8))
    now = datetime.now(hkt)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    # 只喺交易時段跑 (Mon-Fri, 寬鬆窗口 08:00-17:00 HKT 因 GitHub cron 延遲)
    if now.weekday() >= 5:  # Sat/Sun
        return None
    hour_min = now.hour * 100 + now.minute
    if hour_min < 800 or hour_min > 1700:
        return None

    state = load_state()
    new_state = dict(state)
    load_lot_cache()

    alerts = []  # (code, alert_text)
    watchlist = {code: st for code, st in state.items() if st.get("board")}

    for code, entry in watchlist.items():
        board = entry.get("board", "main")
        lot_size = get_lot_size(code, entry)
        stock_st = get_stock_state(state, code)
        tranches = stock_st.get("tranches", [])
        zero_done = stock_st.get("zero_cost_achieved", False)

        quote = fetch_hk_quote(code)
        time.sleep(0.12)
        if not quote or not quote["mcap"] or not quote["price"]:
            continue

        mcap_m = quote["mcap"] / 1e6
        price = quote["price"]
        code4 = f"{int(code):04d}"
        name = quote.get("name", code4)

        # Update price in state
        stock_st["last_mcap_m"] = round(mcap_m, 2)
        stock_st["last_price"] = price
        stock_st["last_check"] = now_str

        # ── 買入觸發 check ──
        dr = stock_st.get("debt_ratio")
        debt_block = dr is not None and dr > 80

        tiers_now = current_tier_reached(mcap_m, board)
        last_alerted_tier = stock_st.get("last_alert_tier", 0)

        if tiers_now > last_alerted_tier and not debt_block:
            expected_inv = tiers_now * TRANCHE_SIZE
            actual_inv = sum(t["hkd"] for t in tranches if t.get("hkd", 0) > 0)
            shares_held = sum(t.get("shares", 0) for t in tranches)
            current_val = shares_held * price if shares_held > 0 else 0
            position = max(actual_inv, current_val)
            shortfall = max(0, expected_inv - position)

            if shortfall > 0:
                est_shares = round_to_lots(shortfall / price, lot_size, "down") if price > 0 else 0
                if est_shares > 0:
                    est_lots = est_shares // lot_size if lot_size > 0 else 0
                    label = "再買入" if zero_done else ("補倉" if tranches else "建倉")
                    alerts.append((code4, (
                        f"🔔 {code4} {name} — {label}\n"
                        f"  ${price:.3f} 市值{mcap_m:.0f}M (層{tiers_now})\n"
                        f"  差${shortfall:,.0f} ({est_lots}手/{est_shares:,}股)\n"
                        f"  /buy {code} {est_shares} {price}"
                    )))
                    stock_st["last_alert_tier"] = tiers_now

        # ── 止賺觸發 check ──
        if tranches and not zero_done:
            avg_cost = calc_avg_cost(tranches)
            gain_pct = calc_gain_pct(avg_cost, price) if avg_cost else 0.0
            tp_signals = check_take_profit(
                price, mcap_m, board, avg_cost, gain_pct,
                tranches, stock_st, lot_size, entry,
            )
            valid_tp = [s for s in tp_signals if s.get("sell_shares", 0) > 0]
            # 只 alert 如果之前未 alert 過止賺
            if valid_tp and not stock_st.get("tp_alerted"):
                sig = valid_tp[0]
                ss = sig.get("sell_shares", 0)
                sl = sig.get("sell_lots", 0)
                rv = sig.get("recv_hkd", 0)
                alerts.append((code4, (
                    f"💰 {code4} {name} — 止賺信號\n"
                    f"  ${price:.3f} 浮盈{gain_pct:+.0f}%\n"
                    f"  賣{sl}手({ss:,}股) 收${rv:,.0f} → 0成本\n"
                    f"  /sell {code} {ss} {price}"
                )))
                stock_st["tp_alerted"] = now_str

        new_state[code] = stock_st

    save_state(new_state)

    if not alerts:
        return None

    header = f"⚡ 即時信號 {now_str}\n{'━' * 20}"
    body = "\n\n".join(a[1] for a in alerts)
    return f"{header}\n\n{body}"


# ============================================================
# Telegram
# ============================================================

def tg_send(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True
        }, timeout=15)
        res = r.json()
        if not res.get("ok"):
            print(f"TG 失敗: {res.get('description')}")
        time.sleep(0.3)


# ============================================================
# Entry point
# ============================================================

def _git_pull_state():
    """Best-effort sync local state.json with remote (no-op if not a repo or no remote)."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            cwd=Path(__file__).parent,
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 and "up to date" not in result.stdout.lower():
            print(f"[git pull] {result.stderr.strip() or result.stdout.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(f"[git pull] skipped: {e}")


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else "send"

    # Local alert/intraday: 同步 remote state 先 (防 Cloudflare Worker 更新後 stale)
    # GitHub Actions cron: 啱啱 checkout, 自然係 fresh, _git_pull_state 唔會出錯
    if mode in ("alert", "send", "intraday", "check"):
        _git_pull_state()

    if mode == "plan":
        print_allocation_plan()

    elif mode == "check":
        print(monitor_report(alert_only=False))

    elif mode == "alert":
        report = monitor_report(alert_only=True)
        print(report)
        tg_send(report)
        # Reset intraday flags after morning report
        st = load_state()
        for code, v in st.items():
            v.pop("last_alert_tier", None)
            v.pop("tp_alerted", None)
        save_state(st)

    elif mode == "intraday":
        msg = intraday_alert()
        if msg:
            print(msg)
            tg_send(msg)
        else:
            print(f"[{datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M')}] 暫無新觸發")

    elif mode == "record" and len(args) == 5:
        # python hk_watchlist_monitor.py record <code> <price> <hkd> <tier_m>
        _, code, price, hkd, tier_m = args
        record_buy(code, float(price), float(hkd), int(tier_m))

    elif mode == "note" and len(args) >= 3:
        # python hk_watchlist_monitor.py note <code> <備注文字>
        code = args[1]
        note = " ".join(args[2:])
        add_note(code, note)

    elif mode == "zerocost" and len(args) == 4:
        # python hk_watchlist_monitor.py zerocost <code> <remaining_shares> <sell_price>
        mark_zero_cost(args[1], float(args[2]), float(args[3]))

    elif mode == "postzero" and len(args) == 3:
        # python hk_watchlist_monitor.py postzero <code> <milestone_index 0/1/2>
        mark_post_zero(args[1], int(args[2]))

    elif mode == "lotsize":
        # 下載 HKEX 全部股票每手股數並快取
        result = fetch_hkex_lot_sizes()
        if result:
            print(f"成功更新 {len(result)} 隻股票每手股數快取")
            # 顯示 Watchlist 股票的每手資訊
            wl = load_state()
            if wl:
                print("\nWatchlist 每手股數:")
                for code, entry in wl.items():
                    if entry.get("board"):
                        lot = get_lot_size(code, entry)
                        print(f"  {int(code):04d}.HK  每手 {lot:,} 股")

    else:
        # 預設：全部報告 + 發 TG
        report = monitor_report(alert_only=False)
        print(report)
        tg_send(report)
