"""
CCASS 時序分析 — 計算每隻股嘅 7d/30d/90d 集中度變化 + 個別 broker 累積
偵測異常事件並寫入 ccass_anomaly 表
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from ..db import init_db, get_conn, upsert_anomaly

# Anomaly thresholds
TOP10_RISE_30D = 15.0       # top10 % 過去 30 日上升 ≥ 15% → 累積信號
TOP10_DROP_30D = 10.0       # top10 % 過去 30 日下降 ≥ 10% → 派貨信號
BROKER_RISE_30D = 5.0       # 個別 broker 倉位過去 30 日 +≥ 5% → 莊家進場
NEW_BROKER_PCT = 5.0        # 之前 ≤ 0.5% 而而家 ≥ 5% → 新莊上場


def parse_date(s):
    """'YYYY/MM/DD' → datetime.date"""
    return datetime.strptime(s, "%Y/%m/%d").date()


def get_history(code):
    """拎一隻股嘅全部 history,以日期排好(舊 → 新)"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT snapshot_date, top1_pct, top10_pct, broker_count, top20_json
            FROM ccass_history WHERE code = ?
            ORDER BY snapshot_date ASC
        """, (code,)).fetchall()
    return [dict(r) for r in rows]


def find_nearest(history, target_date, max_days=7):
    """搵 target_date 或之前最近嘅 snapshot,within max_days"""
    target = parse_date(target_date) if isinstance(target_date, str) else target_date
    best = None
    best_delta = None
    for h in history:
        d = parse_date(h["snapshot_date"])
        delta_days = (target - d).days
        if 0 <= delta_days <= max_days:
            if best_delta is None or delta_days < best_delta:
                best = h
                best_delta = delta_days
    return best


def compute_deltas(history):
    """
    返回 dict:
      latest, top10_d7, top10_d30, top10_d90, broker_d30, ...
      Plus broker_changes: list of (broker_id, name, before_pct, after_pct, delta)
    """
    if not history:
        return None
    latest = history[-1]
    latest_date = parse_date(latest["snapshot_date"])
    out = {
        "latest_date": latest["snapshot_date"],
        "latest_top10": latest["top10_pct"],
        "latest_top1": latest["top1_pct"],
        "latest_brokers": latest["broker_count"],
    }

    # Time-window deltas
    from datetime import timedelta as td
    for label, days in [("7d", 7), ("30d", 30), ("90d", 90)]:
        ref_date = latest_date - td(days=days)
        ref = find_nearest(history, ref_date, max_days=10)
        if ref:
            out[f"top10_d{label}"] = (latest["top10_pct"] or 0) - (ref["top10_pct"] or 0)
            out[f"top1_d{label}"] = (latest["top1_pct"] or 0) - (ref["top1_pct"] or 0)
            out[f"brokers_d{label}"] = (latest["broker_count"] or 0) - (ref["broker_count"] or 0)
            out[f"ref_{label}"] = ref["snapshot_date"]

    # Broker-level changes (30d)
    ref30 = find_nearest(history, latest_date - td(days=30), max_days=10)
    broker_changes = []
    if ref30:
        try:
            old_top20 = {p["id"]: p for p in json.loads(ref30["top20_json"] or "[]")}
            new_top20 = {p["id"]: p for p in json.loads(latest["top20_json"] or "[]")}
            all_ids = set(old_top20) | set(new_top20)
            for bid in all_ids:
                old_pct = (old_top20.get(bid) or {}).get("pct", 0)
                new_pct = (new_top20.get(bid) or {}).get("pct", 0)
                delta = new_pct - old_pct
                name = (new_top20.get(bid) or old_top20.get(bid))["name"]
                if abs(delta) >= 1.0:  # only material changes
                    broker_changes.append({
                        "id": bid, "name": name,
                        "before_pct": old_pct, "after_pct": new_pct,
                        "delta": delta,
                    })
            broker_changes.sort(key=lambda x: -abs(x["delta"]))
        except Exception:
            pass
    out["broker_changes_30d"] = broker_changes[:10]
    return out


def detect_anomalies(code, deltas):
    """根據 deltas 偵測異常,返回 list of anomaly dicts"""
    anomalies = []
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    # 1. Top10 大幅上升 (累積食貨信號)
    d30 = deltas.get("top10_d30d")
    if d30 is not None and d30 >= TOP10_RISE_30D:
        anomalies.append({
            "code": code, "detected_date": today,
            "anomaly_type": "top10_rise_30d",
            "severity": min(d30 / 30.0, 1.0),  # +30% = max severity
            "detail_json": json.dumps({
                "delta_pct": d30,
                "from": deltas.get("ref_30d"),
                "to": deltas.get("latest_date"),
                "before": deltas["latest_top10"] - d30,
                "after": deltas["latest_top10"],
            }),
            "last_updated": now,
        })

    # 2. Top10 大幅下跌 (派貨信號)
    if d30 is not None and d30 <= -TOP10_DROP_30D:
        anomalies.append({
            "code": code, "detected_date": today,
            "anomaly_type": "top10_drop_30d",
            "severity": min(abs(d30) / 25.0, 1.0),
            "detail_json": json.dumps({
                "delta_pct": d30,
                "from": deltas.get("ref_30d"),
                "to": deltas.get("latest_date"),
            }),
            "last_updated": now,
        })

    # 3. 個別 broker 大幅累積 (新莊 / 加碼)
    for ch in deltas.get("broker_changes_30d", []):
        if ch["delta"] >= BROKER_RISE_30D:
            new_broker = ch["before_pct"] < 0.5 and ch["after_pct"] >= NEW_BROKER_PCT
            anomalies.append({
                "code": code, "detected_date": today,
                "anomaly_type": "broker_new" if new_broker else "broker_accumulation",
                "severity": min(ch["delta"] / 15.0, 1.0),
                "detail_json": json.dumps(ch),
                "last_updated": now,
            })

    return anomalies


def run():
    """跑分析 — 對每隻有 history ≥ 2 行嘅股計 deltas + anomaly"""
    init_db()
    with get_conn() as conn:
        codes = [r[0] for r in conn.execute("""
            SELECT code FROM ccass_history GROUP BY code HAVING COUNT(*) >= 2
        """).fetchall()]

    print(f"[ccass_trend] Analyzing {len(codes)} stocks with history")
    n_anom = 0
    for code in codes:
        history = get_history(code)
        deltas = compute_deltas(history)
        if not deltas:
            continue
        anomalies = detect_anomalies(code, deltas)
        for a in anomalies:
            upsert_anomaly(a)
            n_anom += 1
    print(f"[ccass_trend] Detected {n_anom} anomalies")
    return n_anom


def get_trend_for_export(code):
    """畀 pipeline export 用 — 返回 dict 包括 sparkline + anomalies"""
    history = get_history(code)
    if not history:
        return None
    deltas = compute_deltas(history)
    # Sparkline data (≤ 90 most recent points)
    sparkline = [
        {"d": h["snapshot_date"], "top10": h["top10_pct"], "top1": h["top1_pct"], "brokers": h["broker_count"]}
        for h in history[-90:]
    ]
    with get_conn() as conn:
        anomalies = [dict(r) for r in conn.execute("""
            SELECT detected_date, anomaly_type, severity, detail_json
            FROM ccass_anomaly WHERE code = ?
            ORDER BY detected_date DESC LIMIT 20
        """, (code,)).fetchall()]
    return {
        "sparkline": sparkline,
        "deltas": {k: v for k, v in deltas.items() if k != "broker_changes_30d"} if deltas else {},
        "broker_changes_30d": deltas.get("broker_changes_30d", []) if deltas else [],
        "anomalies": anomalies,
    }


if __name__ == "__main__":
    run()
