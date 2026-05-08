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
  python hk_watchlist_monitor.py alert     # 只推買入 / 止賺訊號
  python hk_watchlist_monitor.py plan      # 打印資金計劃表
  python hk_watchlist_monitor.py lotsize   # 更新每手股數快取
"""

import os, sys, json, time, requests, math, sqlite3
from datetime import datetime
from pathlib import Path
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# ★ 配置區 ★
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8784672432:AAFx5xQoF0oWSFQcbRm62ucVgssgMgDzqtc")
CHAT_ID        = os.environ.get("CHAT_ID", "577581404")

# ── Watchlist ──────────────────────────────────────────────
# 格式: "股票號碼": {"board": "main"/"gem", "shell_m": 殼價百萬(可選), "lot": 每手股數(可選)}
# board: main=主板, gem=創業板
# shell_m: 可個別override殼價（唔填就用預設）
# lot: 每手股數（唔填就從HKEX自動抓取）
WATCHLIST: dict[str, dict] = {
    # 格式: "CODE": {"board": "main"/"gem", "shell_m": 殼價百萬(可選), "lot": 每手(可選)}
    "0368": {"board": "main"},
    "1147": {"board": "main"},
    "1156": {"board": "main"},
    "1284": {"board": "main"},
    "1346": {"board": "main"},
    "1407": {"board": "main"},
    "1408": {"board": "main"},
    "1429": {"board": "main"},
    "1433": {"board": "main"},
    "1449": {"board": "main"},
    "1455": {"board": "main"},
    "1473": {"board": "main"},
    "1489": {"board": "main"},
    "1545": {"board": "main"},
    "1553": {"board": "main"},
    "1582": {"board": "main"},
    "1615": {"board": "main"},
    "1620": {"board": "main"},
    "1643": {"board": "main"},
    "1650": {"board": "main"},
    "1740": {"board": "main"},
    "1767": {"board": "main"},
    "1793": {"board": "main"},
    "1795": {"board": "main"},
    "1832": {"board": "main"},
    "1843": {"board": "main"},
    "1871": {"board": "main"},
    "1891": {"board": "main"},
    "1927": {"board": "main"},
    "1941": {"board": "main"},
    "1947": {"board": "main"},
    "1955": {"board": "main"},
    "1960": {"board": "main"},
    "1971": {"board": "main"},
    "2108": {"board": "main"},
    "2129": {"board": "main"},
    "2159": {"board": "main"},
    "2205": {"board": "main"},
    "2230": {"board": "main"},
    "2263": {"board": "main"},
    "2347": {"board": "main"},
    "2350": {"board": "main"},
    "2370": {"board": "main"},
    "2372": {"board": "main"},
    "2381": {"board": "main"},
    "2455": {"board": "main"},
    "2457": {"board": "main"},
    "2497": {"board": "main"},
    "2501": {"board": "main"},
    "2505": {"board": "main"},
    "2528": {"board": "main"},
    "2529": {"board": "main"},
    "2545": {"board": "main"},
    "2885": {"board": "main"},
    "3348": {"board": "main"},
    "3830": {"board": "main"},
    "6939": {"board": "main"},
    "9929": {"board": "main"},
    "8103": {"board": "gem"},
    "8241": {"board": "gem"},
    "8400": {"board": "gem"},
    "8447": {"board": "gem"},
    "8495": {"board": "gem"},
    "8532": {"board": "gem"},
    "8620": {"board": "gem"},
}

# ── 每注固定金額 (HKD) ────────────────────────────────────
TRANCHE_SIZE = 10_000   # 每次買入 $10,000

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
    {"mcap_m": 400,  "mcap_gain_pct": 100.0, "gain_pct": 200.0, "sell_frac": None, "label": "M1 市值4億+浮盈100% / 浮盈200%"},
    {"mcap_m": 600,  "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M2 市值6億"},
    {"mcap_m": 1000, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M3 市值10億"},
    {"mcap_m": 1500, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M4 市值15億"},
    {"mcap_m": 2000, "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M5 市值20億"},
]

# 創業板（主板門檻 × 0.4，與殼價比例一致）
POST_ZERO_GEM = [
    {"mcap_m": 150,  "mcap_gain_pct": 100.0, "gain_pct": 200.0, "sell_frac": None, "label": "M1 市值1.5億+浮盈100% / 浮盈200%"},
    {"mcap_m": 250,  "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M2 市值2.5億"},
    {"mcap_m": 400,  "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M3 市值4億"},
    {"mcap_m": 600,  "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M4 市值6億"},
    {"mcap_m": 800,  "mcap_gain_pct": None,  "gain_pct": None,  "sell_frac": 0.25, "label": "M5 市值8億"},
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
    s.get("https://finance.yahoo.com", timeout=15)
    r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
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
        sell_pct  = sell_shares / total_shares * 100 if total_shares > 0 else 0
        sell_lots = sell_shares // lot_size
        recv_hkd  = sell_shares * current_price

        # 0成本觸發條件 = 與 M1 完全一致，觸發後 M1 自動視為已完成
        # (市值 ≥ M1門檻  AND  浮盈 ≥ 100%)  OR  (浮盈 ≥ 200%)
        m1 = (POST_ZERO_MAIN if board == "main" else POST_ZERO_GEM)[0]
        m1_mcap_ok = mcap_m >= m1["mcap_m"] and gain_pct >= (m1.get("mcap_gain_pct") or 100.0)
        m1_gain_ok = m1["gain_pct"] is not None and gain_pct >= m1["gain_pct"]

        if m1_mcap_ok or m1_gain_ok:
            triggers = []
            if m1_mcap_ok:
                triggers.append(f"市值{mcap_m:.0f}M≥{m1['mcap_m']}M + 浮盈{gain_pct:.0f}%≥{m1.get('mcap_gain_pct',100):.0f}%")
            if m1_gain_ok:
                triggers.append(f"浮盈{gain_pct:.0f}%≥{m1['gain_pct']:.0f}%")
            trigger_desc = " / ".join(triggers)
            signals.append({
                "type": "ZERO_COST",
                "msg": (
                    f"[0成本+M1] {trigger_desc}  "
                    f"賣 {sell_lots}手 ({sell_shares:,}股, {sell_pct:.0f}%)  "
                    f"≈ HKD {recv_hkd:,.0f}  →  {remain:,}股 0成本持倉  (完成後M1自動done)"
                ),
                "shares_to_sell": sell_shares,
                "lots_to_sell":   sell_lots,
                "hkd_to_receive": recv_hkd,
                "auto_m1_done":   True,   # zerocost 指令記錄時自動 mark post_zero_done=[0]
            })

    # ── 階段二：已達0成本，追蹤後市目標 ──────────────────
    else:
        zero_shares  = int(stock_st.get("zero_cost_shares") or 0)
        done_indices = stock_st.get("post_zero_done", [])
        milestones   = POST_ZERO_MAIN if board == "main" else POST_ZERO_GEM

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

            # 計算賣出股數
            sell_frac = ms["sell_frac"]
            if sell_frac is None:
                # M1：鏡像建議（與0成本賣出量相同），唔計入 shares_to_sell
                mirror = round_to_lots(zero_sold, lot_size, "down") or lot_size
                mirror = min(mirror, zero_shares)  # 唔能超過剩餘
                mirror_lots = mirror // lot_size
                signals.append({
                    "type": f"POST_ZERO_{i}",
                    "msg": (
                        f"[後市止賺] {ms['label']}  {trigger_desc}  "
                        f"建議賣 {mirror_lots}手 ({mirror:,}股) ≈ HKD {mirror*current_price:,.0f}  "
                        f"(鏡像0成本賣出量，可自行調整)  剩餘{zero_shares-mirror:,}股免費持有"
                    ),
                    "shares_to_sell": mirror,
                    "lots_to_sell": mirror_lots,
                    "hkd_to_receive": mirror * current_price,
                    "milestone_idx": i,
                    "manual_adjust": True,
                })
            else:
                raw_sell    = zero_shares * sell_frac
                shares_sell = round_to_lots(raw_sell, lot_size, "down")
                lots_sell   = shares_sell // lot_size
                recv_hkd    = shares_sell * current_price
                signals.append({
                    "type": f"POST_ZERO_{i}",
                    "msg": (
                        f"[後市止賺] {ms['label']}  {trigger_desc}  "
                        f"賣 {lots_sell}手 ({shares_sell:,}股, {sell_frac*100:.0f}%剩餘)  "
                        f"≈ HKD {recv_hkd:,.0f}  餘下{zero_shares-shares_sell:,}股免費持有"
                    ),
                    "shares_to_sell": shares_sell,
                    "lots_to_sell": lots_sell,
                    "hkd_to_receive": recv_hkd,
                    "milestone_idx": i,
                    "manual_adjust": False,
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


def build_stock_block(
    code: str, board: str, quote: dict,
    stock_st: dict, new_tiers: list[int],
    tp_signals: list[dict],
    ccass_alerts: list[str] | None = None,
) -> str:
    mcap_m    = quote["mcap"] / 1e6
    lot_size  = stock_st.get("lot_size", 1)
    price     = quote["price"]
    chg       = quote.get("chg") or 0
    name      = quote["name"]
    tranches  = stock_st.get("tranches", [])
    zero_done = stock_st.get("zero_cost_achieved", False)

    sign = "+" if chg >= 0 else ""
    total_invested = sum(t["hkd"] for t in tranches)
    avg_cost = calc_avg_cost(tranches) if tranches else None
    gain_pct = calc_gain_pct(avg_cost, price) if avg_cost else None

    # ── 頭部一行 ──
    prefix = ""
    if new_tiers:   prefix = "[買入] "
    if tp_signals:  prefix = "[止賺] "
    if new_tiers and tp_signals: prefix = "[買入+止賺] "

    zero_tag = " [0成本]" if zero_done else ""
    gain_tag = f"  浮盈{gain_pct:+.0f}%" if gain_pct is not None else ""
    inv_tag  = f"  已投${total_invested:,.0f}" if tranches and not zero_done else ""
    z_shares = stock_st.get("zero_cost_shares", 0) or 0
    z_tag    = f"  免費{z_shares:,}股" if zero_done and z_shares else ""

    lines = [
        f"{prefix}[{int(code):04d}] {name}{zero_tag}  "
        f"${price:.3f} {sign}{chg:.1f}%  {fmt_mcap(mcap_m)}{gain_tag}{inv_tag}{z_tag}"
    ]

    # ── 買入訊號 ──
    for nt in new_tiers:
        est_shares = round_to_lots(TRANCHE_SIZE / price, lot_size, "down")
        est_lots   = est_shares // lot_size if lot_size > 0 else "?"
        lines.append(
            f"  → 買入 市值跌穿{fmt_mcap(nt)} "
            f"{est_lots}手({est_shares:,}股) ≈ ${est_shares*price:,.0f}"
        )

    # ── 止賺訊號 ──
    for sig in tp_signals:
        lines.append(f"  → {sig['msg']}")

    # ── 0成本後市目標進度 ──
    if zero_done and z_shares:
        milestones = POST_ZERO_MAIN if board == "main" else POST_ZERO_GEM
        done_idx   = stock_st.get("post_zero_done", [])
        for i, ms in enumerate(milestones):
            if i in done_idx:
                continue
            mcap_req = ms["mcap_m"]
            label    = ms["label"]
            if ms["sell_frac"]:
                sell_q = round_to_lots(z_shares * ms["sell_frac"], lot_size, "down")
                action = f"賣{sell_q:,}股({sell_q*price:,.0f}$)"
            else:
                action = "按持倉決定賣出量"
            curr = " ← 已達到!" if mcap_m >= mcap_req else ""
            lines.append(f"  [後市{i+1}] {label}: {action}{curr}")

    # ── CCASS 警示 + 備注 ──
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
    查詢 screener.db ccass_history，若 top10_pct 在最近 lookback_rows 條記錄內
    升幅 >= threshold_pct，返回格式化警示字串；否則返回 None。
    DB 不存在或表格不找到時靜默跳過。
    """
    if not SCREENER_DB.exists():
        return None

    # DB code 格式：去掉前導零（e.g. "0001" → "1"）
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
            # Table doesn't exist yet
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
    if recent_pct is None or old_pct is None:
        return None

    delta = recent_pct - old_pct
    if delta >= threshold_pct:
        broker_count = recent["broker_count"]
        return (
            f"[CCASS IN] top10集中度 +{delta:.1f}% "
            f"({old_pct:.1f}%→{recent_pct:.1f}%, "
            f"{oldest['snapshot_date']}→{recent['snapshot_date']}, "
            f"broker數={broker_count})"
        )
    return None


# ============================================================
# 主報告
# ============================================================

def monitor_report(alert_only: bool = False) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = load_state()
    new_state = dict(state)

    all_blocks    = []
    signal_blocks = []
    no_data       = []

    load_lot_cache()  # 預先載入每手快取

    for code, entry in WATCHLIST.items():
        board    = get_board(entry)
        lot_size = get_lot_size(code, entry if isinstance(entry, dict) else None)

        quote = fetch_hk_quote(code)
        time.sleep(0.2)

        if not quote or not quote["mcap"] or not quote["price"]:
            no_data.append(code)
            continue

        mcap_m   = quote["mcap"] / 1e6
        price    = quote["price"]
        tiers    = get_tiers(board)
        shell_m  = get_shell_m(board, entry if isinstance(entry, dict) else None)
        stock_st = get_stock_state(state, code)

        prev_tier_reached = stock_st.get("tier_reached", 0)
        new_tier_reached  = current_tier_reached(mcap_m, board)

        # 找出新觸發的層，記錄每注（股數對齊整手）
        new_tiers = []
        if new_tier_reached > prev_tier_reached:
            newly     = tiers[prev_tier_reached:new_tier_reached]
            new_tiers = newly
            for nt in newly:
                bought_shares = round_to_lots(TRANCHE_SIZE / price, lot_size, "down")
                actual_hkd    = bought_shares * price
                stock_st.setdefault("tranches", []).append({
                    "tier_m": nt,
                    "price":  price,
                    "hkd":    actual_hkd,
                    "shares": bought_shares,
                    "lots":   bought_shares // lot_size,
                    "date":   now,
                })

        tranches = stock_st.get("tranches", [])
        avg_cost = calc_avg_cost(tranches) if tranches else None
        gain_pct = calc_gain_pct(avg_cost, price) if avg_cost else 0.0

        tp_signals = check_take_profit(
            price, mcap_m, board, avg_cost, gain_pct,
            tranches, stock_st, lot_size,
            entry if isinstance(entry, dict) else None,
        )

        # CCASS 集中度自動警示（唔寫入 state，每次動態查詢）
        ccass_alert = check_ccass_concentration(code)
        ccass_alerts = [ccass_alert] if ccass_alert else []

        # 更新狀態（tier_reached 只升不降——高水位，防止市值反彈後重複觸發同層）
        stock_st["tier_reached"] = max(prev_tier_reached, new_tier_reached)
        stock_st["lot_size"]     = lot_size
        stock_st["last_mcap_m"]  = round(mcap_m, 2)
        stock_st["last_price"]   = price
        stock_st["last_check"]   = now
        new_state[code] = stock_st

        block = build_stock_block(code, board, quote, stock_st, new_tiers, tp_signals, ccass_alerts)
        all_blocks.append(block)

        if new_tiers or tp_signals or ccass_alerts:
            signal_blocks.append(block)

    save_state(new_state)

    # ── 持倉總覽 ──
    total_inv = total_val = total_gain = 0.0
    n_holdings = n_zero = 0
    for code2, st2 in new_state.items():
        tr = st2.get("tranches", [])
        if not tr:
            continue
        n_holdings += 1
        inv = sum(t["hkd"] for t in tr)
        total_inv += inv
        p2 = st2.get("last_price") or 0
        ls = st2.get("lot_size", 1)
        shares2 = sum(
            round_to_lots(t["hkd"] / t["price"], ls, "down")
            for t in tr if t.get("price", 0) > 0
        )
        if st2.get("zero_cost_achieved"):
            n_zero += 1
            val2 = (st2.get("zero_cost_shares") or 0) * p2
        else:
            val2 = shares2 * p2
        total_val  += val2
        total_gain += val2 - inv

    gain_pct_total = total_gain / total_inv * 100 if total_inv > 0 else 0
    gain_sign      = "+" if total_gain >= 0 else ""

    # ── 買入層表（一次性）──
    main_tiers_str = "/".join(fmt_mcap(t) for t in MAIN_TIERS_M)
    gem_tiers_str  = "/".join(fmt_mcap(t) for t in GEM_TIERS_M)
    max_main = TRANCHE_SIZE * len(MAIN_TIERS_M)
    max_gem  = TRANCHE_SIZE * len(GEM_TIERS_M)

    sep  = "\n" + "-" * 40 + "\n"
    sep2 = "\n" + "=" * 40 + "\n"

    summary = (
        f"Watchlist | {now} HKT\n"
        f"{'='*40}\n"
        f"持倉: {n_holdings}隻 | 已0成本: {n_zero}隻\n"
        f"已投: ${total_inv:,.0f}  現值: ${total_val:,.0f}  浮盈: {gain_sign}${total_gain:,.0f} ({gain_sign}{gain_pct_total:.1f}%)\n"
        f"{'='*40}\n"
        f"買入計劃 (每注${TRANCHE_SIZE:,})\n"
        f"  主板 {len(MAIN_TIERS_M)}層 ≤ {main_tiers_str} (最多${max_main:,})\n"
        f"  創業板 {len(GEM_TIERS_M)}層 ≤ {gem_tiers_str} (最多${max_gem:,})\n"
        f"  0成本: 主板市值≥4億+浮盈≥100% / 浮盈≥200% | 創業板市值≥1.5億+浮盈≥100% / 浮盈≥200%\n"
        f"{'='*40}"
    )

    if alert_only:
        if not signal_blocks:
            return f"{summary}\n\n[OK] 暫無新訊號"
        return summary + "\n\n" + sep.join(signal_blocks)

    body = sep.join(all_blocks) if all_blocks else "(WATCHLIST 為空)"
    if no_data:
        body += f"\n\n[無數據] {', '.join(no_data)}"
    return summary + "\n\n" + body


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
    stock_st["zero_cost_price"]    = sell_price
    stock_st["zero_cost_date"]     = datetime.now().strftime("%Y-%m-%d")
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

if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else "send"

    if mode == "plan":
        print_allocation_plan()

    elif mode == "check":
        print(monitor_report(alert_only=False))

    elif mode == "alert":
        report = monitor_report(alert_only=True)
        print(report)
        tg_send(report)

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
            if WATCHLIST:
                print("\nWatchlist 每手股數:")
                for code, entry in WATCHLIST.items():
                    lot = get_lot_size(code, entry if isinstance(entry, dict) else None)
                    print(f"  {int(code):04d}.HK  每手 {lot:,} 股")

    elif not WATCHLIST:
        print("WATCHLIST is empty. Add stock codes to hk_watchlist_monitor.py")
        print_allocation_plan()

    else:
        # 預設：全部報告 + 發 TG
        report = monitor_report(alert_only=False)
        print(report)
        tg_send(report)
