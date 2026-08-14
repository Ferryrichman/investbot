#!/usr/bin/env python3
"""
MJ 半新股系統 — 每日功課 PDF 匯入
=====================================
把課程每日發嘅功課 PDF 表格 parse 入 data/mj_state.json。
系統動能係課程專有指標 (要 MarketSmith + DT) — 冇得自己計，只可以由 PDF 匯入。

使用方式:
  python mj_import.py                     # 自動搵 Downloads 最新一份 MJ 每日功課 PDF
  python mj_import.py <path-to-pdf>       # 匯入指定 PDF

輸出: data/mj_state.json
  _meta.prev_codes = 上一份功課嘅股票代號 (用嚟偵測 🆕 新入選)
  stocks[CODE] = 該股全部 MJ 欄位 (動能 / 放棄位 / 位置0,2,3 ...)

注意: monitor 會寫 last_price / last_check 落每隻股，重新匯入時會保留。
"""

import re
import sys
import json
import glob
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz  # PyMuPDF

MJ_STATE_FILE = Path(__file__).parent / "data" / "mj_state.json"
DOWNLOADS_DIR = Path(r"C:\Users\JCKW\Downloads")
PDF_GLOB      = "MJ*每日功課*.pdf"
MOM_FLOOR     = 55      # 動能下限，低過就係「要放棄」

DATE_RE     = re.compile(r"^\d{2}-\d{2}-\d{4}$")
PDF_DATE_RE = re.compile(r"Date\s+(\d{2}-\d{2}-\d{4})")


def _f(tok: str) -> float:
    """轉 float，容許尾隨 % 同千分位逗號"""
    return float(str(tok).replace(",", "").rstrip("%"))


def parse_pdf(pdf_path: Path) -> tuple[str, dict]:
    """
    parse MJ 每日功課 PDF (表格喺第 1 頁)。
    返回 (pdf_date, {code: {...欄位...}})
    """
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[0]

        # PDF 自己嘅功課日期: 第 1 頁有一行 "Date 06-08-2026"
        m = PDF_DATE_RE.search(page.get_text())
        pdf_date = m.group(1) if m else ""

        # 按 y 座標分組成行 — 容差 clustering (唔用 round(y/3): 固定 bin 會喺
        # baseline 跨越 bin 邊界時將一行切開兩份, 令該行漏 import — 2026-08 review)
        words = page.get_text("words")   # (x0, y0, x1, y1, word, ...)
        groups: list[dict] = []          # [{y, words:[(x0,word)]}]
        for w in sorted(words, key=lambda w: w[1]):   # 按 y0 排
            y0 = w[1]
            if groups and abs(y0 - groups[-1]["y"]) <= 2.5:
                groups[-1]["words"].append((w[0], w[4]))
            else:
                groups.append({"y": y0, "words": [(w[0], w[4])]})

        stocks: dict[str, dict] = {}
        skipped_rows = []
        for g in groups:
            toks = [t[1] for t in sorted(g["words"], key=lambda x: x[0])]
            # 資料行 = >=15 個 token，第 1 個係序號(全數字)，
            # 第 2 個係股票代號 (1-5 位數字 — 支持 <1000 嘅細 code)
            if len(toks) < 15:
                continue
            if not toks[0].isdigit():
                continue
            if not (1 <= len(toks[1]) <= 5 and toks[1].isdigit()):
                continue

            try:
                rec = _parse_row(toks)
            except (ValueError, IndexError, StopIteration) as e:
                print(f"  [skip] {toks[:4]} … parse 失敗: {e}")
                skipped_rows.append(toks[:4])
                continue
            stocks[rec.pop("_code")] = rec

        if skipped_rows:
            print(f"  ⚠️ {len(skipped_rows)} 行 parse 失敗, 請 check PDF 格式")
        return pdf_date, stocks
    finally:
        doc.close()


def _parse_row(r: list[str]) -> dict:
    """parse 一條 x-sorted 資料行 → 欄位 dict (連 _code)"""
    code = f"{int(r[1]):04d}"

    # 股票名可以係多個 token 嘅英文名 (e.g. "TRUE PARTNER" / "AI X TECH")，
    # 亦有可能完全冇名 — 以第一個日期 token 做終點。
    di = next(i for i, t in enumerate(r) if DATE_RE.match(t))
    name = " ".join(r[2:di]) or "(無名)"

    # 留意日期有時會出現兩次 — 全部食完
    i = di
    dates: list[str] = []
    while i < len(r) and DATE_RE.match(r[i]):
        dates.append(r[i])
        i += 1

    # 9 個數值: 留意日收市價, 系統動能, 動能變化, 現價, 放棄位置, 位置0, 位置2, 位置3, 現時狀態%
    f = [_f(x) for x in r[i:i + 9]]
    j = i + 9

    return {
        "_code":       code,
        "name":        name,
        "watch_date":  dates[0] if dates else "",
        "watch_close": f[0],
        "mom":         f[1],
        "mom_chg":     f[2],
        "pdf_price":   f[3],
        "abandon":     f[4],
        "p0":          f[5],
        "p2":          f[6],
        "p3":          f[7],
        "status_pct":  f[8],
        "lot_hkd":     _f(r[j]),          # 每手價錢
        "mcap_y":      _f(r[j + 1]),      # 市值 (億)
        "ccass5":      _f(r[j + 2]),      # CCASS Top5 %
        "brokers":     int(_f(r[j + 3])), # 券商數
        "ipo":         r[j + 4],          # IPO 日期
        "dchg":        _f(r[j + 5]),      # Daily Change
        # 系統信息 / 每日Notes tag；最後一個 token 係內部 ID — 唔要
        "info":        list(r[j + 6:-1]),
    }


def pick_latest_pdf() -> Path | None:
    """自動搵 Downloads 最新一份 MJ 每日功課 PDF"""
    hits = glob.glob(str(DOWNLOADS_DIR / PDF_GLOB))
    if not hits:
        return None
    return Path(max(hits, key=lambda p: Path(p).stat().st_mtime))


def load_existing() -> dict:
    if MJ_STATE_FILE.exists():
        try:
            return json.loads(MJ_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠ 舊 mj_state.json 讀唔到 ({e}) — 當第一次匯入")
    return {}


def do_import(pdf_path: Path) -> dict:
    pdf_date, stocks = parse_pdf(pdf_path)
    if not stocks:
        print(f"❌ {pdf_path.name} 一行都 parse 唔到 — 格式可能改咗")
        sys.exit(1)

    old = load_existing()
    old_stocks = old.get("stocks", {}) if isinstance(old, dict) else {}
    prev_codes = sorted(old_stocks.keys())

    # 保留 monitor 寫落嘅 last_price / last_check + 手動標記嘅 suspended (停牌)
    for code, rec in stocks.items():
        prev = old_stocks.get(code) or {}
        for k in ("last_price", "last_check", "suspended", "featured"):
            if k in prev:
                rec[k] = prev[k]

    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M HKT")
    out = {
        "_meta": {
            "pdf_date":    pdf_date,
            "source_pdf":  pdf_path.name,
            "imported_at": now,
            "count":       len(stocks),
            "prev_codes":  prev_codes,
        },
        "stocks": dict(sorted(stocks.items())),
    }
    # 保留 monitor 嘅 alert suppression 記錄
    if isinstance(old.get("_meta"), dict) and old["_meta"].get("alert_seen"):
        out["_meta"]["alert_seen"] = old["_meta"]["alert_seen"]

    MJ_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MJ_STATE_FILE.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


def print_summary(out: dict):
    meta   = out["_meta"]
    stocks = out["stocks"]
    prev   = set(meta.get("prev_codes") or [])

    n_new     = len([c for c in stocks if c not in prev]) if prev else 0
    n_lowmom  = len([s for s in stocks.values() if s["mom"] < MOM_FLOOR])
    n_abandon = len([s for s in stocks.values() if s["pdf_price"] <= s["abandon"]])

    print(f"\n✅ 匯入完成 — {meta['source_pdf']}")
    print(f"   功課日期  : {meta['pdf_date'] or '(讀唔到)'}")
    print(f"   匯入時間  : {meta['imported_at']}")
    print(f"   股票總數  : {len(stocks)}")
    if prev:
        gone = sorted(prev - set(stocks))
        print(f"   新入選    : {n_new} 隻 (上一份 {len(prev)} 隻)")
        if gone:
            print(f"   已消失    : {len(gone)} 隻 — {', '.join(gone[:12])}"
                  + (" …" if len(gone) > 12 else ""))
    else:
        print("   新入選    : — (第一次匯入，唔標 New)")
    print(f"   低動能<{MOM_FLOOR} : {n_lowmom} 隻")
    print(f"   到放棄位  : {n_abandon} 隻")
    print(f"   寫入      : {MJ_STATE_FILE}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        pdf_path = Path(args[0])
        if not pdf_path.exists():
            print(f"❌ 搵唔到檔案: {pdf_path}")
            sys.exit(1)
    else:
        pdf_path = pick_latest_pdf()
        if pdf_path is None:
            print(f"❌ 搵唔到任何 MJ 每日功課 PDF")
            print(f"   搜尋位置: {DOWNLOADS_DIR}")
            print(f"   搜尋格式: {PDF_GLOB}")
            print(f"   用法: python mj_import.py <path-to-pdf>")
            sys.exit(1)
        print(f"📂 自動選擇最新 PDF: {pdf_path.name}")

    out = do_import(pdf_path)
    print_summary(out)


if __name__ == "__main__":
    main()
