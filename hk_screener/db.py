"""
SQLite schema + helpers for HK 財技股 screener
"""
import sqlite3
from contextlib import contextmanager
from .config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code            TEXT PRIMARY KEY,         -- 4-digit, no leading zero (e.g. "0001" → "1")
    code_padded     TEXT NOT NULL,            -- "00001" 5-digit form
    name            TEXT,
    name_zh         TEXT,
    board           TEXT,                     -- "Main" | "GEM"
    stock_type      TEXT,                     -- "Equity" | "H Share" | "Red Chip" | etc
    industry        TEXT,                     -- HKEX classification
    industry_tags   TEXT,                     -- JSON array: ["shell_friendly", ...]
    market_cap_hkd  REAL,
    is_chapter21    INTEGER DEFAULT 0,
    last_updated    TEXT
);

CREATE TABLE IF NOT EXISTS ipo_info (
    code            TEXT PRIMARY KEY,
    listing_date    TEXT,                     -- ISO YYYY-MM-DD
    ipo_price_hkd   REAL,
    raise_amount_hkd REAL,                    -- 集資總額
    sponsor         TEXT,                     -- 主保薦人 (可能多個,逗號分隔)
    sponsors_all    TEXT,                     -- 所有保薦人 JSON
    last_updated    TEXT,
    FOREIGN KEY (code) REFERENCES stocks(code)
);

CREATE TABLE IF NOT EXISTS sponsor_record (
    sponsor         TEXT PRIMARY KEY,
    total_ipos      INTEGER,                  -- 5 年內保薦過嘅 IPO 數
    pumped_count    INTEGER,                  -- 5 年內見過 3x 嘅
    hit_rate        REAL,                     -- pumped / total
    last_computed   TEXT
);

CREATE TABLE IF NOT EXISTS price_metrics (
    code            TEXT PRIMARY KEY,
    last_close      REAL,
    avg_turnover    REAL,                     -- 過去 60 日平均日成交額 HKD
    range_pct       REAL,                     -- (max-min)/avg over RANGE_DAYS
    range_high      REAL,
    range_low       REAL,
    last_updated    TEXT,
    FOREIGN KEY (code) REFERENCES stocks(code)
);

CREATE TABLE IF NOT EXISTS ccass_snapshot (
    code            TEXT PRIMARY KEY,
    snapshot_date   TEXT,
    top10_pct       REAL,                     -- top 10 participants 持股 %
    top1_pct        REAL,
    broker_count    INTEGER,                  -- 持有股份嘅 participant 數
    raw_json        TEXT,                     -- 完整數據備份
    last_updated    TEXT,
    FOREIGN KEY (code) REFERENCES stocks(code)
);

CREATE TABLE IF NOT EXISTS ccass_history (
    code            TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,            -- 'YYYY/MM/DD' (HKEX format)
    top1_pct        REAL,
    top10_pct       REAL,
    broker_count    INTEGER,
    top20_json      TEXT,                     -- 序列化 top 20 participants 用嚟追蹤個別 broker 變化
    last_updated    TEXT,
    PRIMARY KEY (code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS ccass_anomaly (
    code            TEXT NOT NULL,
    detected_date   TEXT NOT NULL,
    anomaly_type    TEXT NOT NULL,            -- 'top10_rise_30d' | 'broker_accumulation' | 'top10_drop' etc.
    severity        REAL,                     -- 0..1 score
    detail_json     TEXT,                     -- structured details (broker IDs, deltas)
    last_updated    TEXT,
    PRIMARY KEY (code, detected_date, anomaly_type)
);

CREATE INDEX IF NOT EXISTS idx_stocks_board ON stocks(board);
CREATE INDEX IF NOT EXISTS idx_stocks_industry ON stocks(industry);
CREATE INDEX IF NOT EXISTS idx_stocks_mcap ON stocks(market_cap_hkd);
CREATE INDEX IF NOT EXISTS idx_ccass_history_code_date ON ccass_history(code, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_ccass_anomaly_date ON ccass_anomaly(detected_date);
"""

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def upsert_stocks(rows):
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO stocks (code, code_padded, name, name_zh, board, stock_type,
                                industry, industry_tags, market_cap_hkd, is_chapter21, last_updated)
            VALUES (:code, :code_padded, :name, :name_zh, :board, :stock_type,
                    :industry, :industry_tags, :market_cap_hkd, :is_chapter21, :last_updated)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                name_zh = excluded.name_zh,
                board = excluded.board,
                stock_type = excluded.stock_type,
                industry = excluded.industry,
                industry_tags = excluded.industry_tags,
                market_cap_hkd = excluded.market_cap_hkd,
                is_chapter21 = excluded.is_chapter21,
                last_updated = excluded.last_updated
        """, rows)

def upsert_price_metrics(row):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO price_metrics (code, last_close, avg_turnover, range_pct,
                                       range_high, range_low, last_updated)
            VALUES (:code, :last_close, :avg_turnover, :range_pct,
                    :range_high, :range_low, :last_updated)
            ON CONFLICT(code) DO UPDATE SET
                last_close = excluded.last_close,
                avg_turnover = excluded.avg_turnover,
                range_pct = excluded.range_pct,
                range_high = excluded.range_high,
                range_low = excluded.range_low,
                last_updated = excluded.last_updated
        """, row)

def upsert_ipo(row):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ipo_info (code, listing_date, ipo_price_hkd, raise_amount_hkd,
                                  sponsor, sponsors_all, last_updated)
            VALUES (:code, :listing_date, :ipo_price_hkd, :raise_amount_hkd,
                    :sponsor, :sponsors_all, :last_updated)
            ON CONFLICT(code) DO UPDATE SET
                listing_date = excluded.listing_date,
                ipo_price_hkd = excluded.ipo_price_hkd,
                raise_amount_hkd = excluded.raise_amount_hkd,
                sponsor = excluded.sponsor,
                sponsors_all = excluded.sponsors_all,
                last_updated = excluded.last_updated
        """, row)

def upsert_ccass(row):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ccass_snapshot (code, snapshot_date, top10_pct, top1_pct,
                                        broker_count, raw_json, last_updated)
            VALUES (:code, :snapshot_date, :top10_pct, :top1_pct,
                    :broker_count, :raw_json, :last_updated)
            ON CONFLICT(code) DO UPDATE SET
                snapshot_date = excluded.snapshot_date,
                top10_pct = excluded.top10_pct,
                top1_pct = excluded.top1_pct,
                broker_count = excluded.broker_count,
                raw_json = excluded.raw_json,
                last_updated = excluded.last_updated
        """, row)

def upsert_ccass_history(row):
    """Append/replace a CCASS history row (1 per stock per date)"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ccass_history (code, snapshot_date, top1_pct, top10_pct,
                                       broker_count, top20_json, last_updated)
            VALUES (:code, :snapshot_date, :top1_pct, :top10_pct,
                    :broker_count, :top20_json, :last_updated)
            ON CONFLICT(code, snapshot_date) DO UPDATE SET
                top1_pct = excluded.top1_pct,
                top10_pct = excluded.top10_pct,
                broker_count = excluded.broker_count,
                top20_json = excluded.top20_json,
                last_updated = excluded.last_updated
        """, row)

def upsert_anomaly(row):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ccass_anomaly (code, detected_date, anomaly_type, severity,
                                       detail_json, last_updated)
            VALUES (:code, :detected_date, :anomaly_type, :severity,
                    :detail_json, :last_updated)
            ON CONFLICT(code, detected_date, anomaly_type) DO UPDATE SET
                severity = excluded.severity,
                detail_json = excluded.detail_json,
                last_updated = excluded.last_updated
        """, row)

def upsert_sponsor_record(row):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sponsor_record (sponsor, total_ipos, pumped_count, hit_rate, last_computed)
            VALUES (:sponsor, :total_ipos, :pumped_count, :hit_rate, :last_computed)
            ON CONFLICT(sponsor) DO UPDATE SET
                total_ipos = excluded.total_ipos,
                pumped_count = excluded.pumped_count,
                hit_rate = excluded.hit_rate,
                last_computed = excluded.last_computed
        """, row)
