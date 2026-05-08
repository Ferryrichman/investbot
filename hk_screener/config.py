"""
HK 財技股 Screener - 篩選條件配置
依用戶玩法 SOP 定義門檻,改參數只需修改呢個檔案
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "screener.db"
JSON_OUT = ROOT / "frontend" / "data.json"

# ============================================================
# 篩選門檻 (硬條件)
# ============================================================

# 主板 (代碼 1-3xxx)
MAIN_BOARD = {
    "ipo_raise_max_hkd": 150_000_000,   # 1.5 億下
    "market_cap_max_hkd": 400_000_000,  # 4 億下
}

# 創業板 / GEM (代碼 8xxx)
GEM = {
    "ipo_raise_max_hkd": 80_000_000,    # 0.8 億下
    "market_cap_max_hkd": 200_000_000,  # 2 億下
}

# 通用條件
TOP_HOLDER_MIN_PCT = 75.0          # 大莊 holding ≥ 75%
BROKER_COUNT_MAX = 120             # 券商 < 120 間
DAILY_TURNOVER_MAX_HKD = 1_000_000 # 平均日成交 < 100 萬
RANGE_DAYS = 300                   # 橫行檢測窗口
RANGE_MAX_PCT = 30.0               # (high-low)/avg < 30%
IPO_WITHIN_YEARS = 4               # 只睇近 4 年上市

# 保薦人炒作記錄: 5 年內 IPO 中 3x+ 比率
SPONSOR_LOOKBACK_YEARS = 5
SPONSOR_PUMP_MULTIPLE = 3.0
SPONSOR_HIT_RATE_MIN = 0.30        # ≥ 30% 命中率算「有炒作記錄」

# ============================================================
# 行業分類 (HKEX 11 大類 + 自定義 tag)
# ============================================================
# HKEX 行業 → 自定 tag 映射
INDUSTRY_TAGS = {
    "Construction": ["shell_friendly", "acct_friendly"],
    "Properties & Construction": ["shell_friendly", "acct_friendly"],
    "Financials": ["shell_friendly"],
    "Information Technology": ["acct_friendly"],
    # 夕陽產業 — 後續可加 manual 分類
}

# 排除類別
EXCLUDE_STOCK_TYPES = {"H Share", "Red Chip"}  # 內資股


# ============================================================
# 21章公司清單 (Manual list - 由 hkex_chapter21.py 維護)
# ============================================================
# Chapter 21 投資公司 stock codes (4-digit, no leading zero)
# 來源: HKEX List of Chapter 21 Investment Companies
CHAPTER_21_CODES = {
    "1141",  # CSOP Asset Management
    "1181",  # Tang Palace
    "1217",  # China Innovative Finance
    # 完整 list 由 scraper 維護 / 用戶可手動加
}
