# -*- coding: utf-8 -*-
"""recovery_score.py — 睡眠恢复分（个人参考，非医学指标）

分层口径（固定）：
  1) 固定安全/绝对项：时长达成、睡眠连续性、血氧（最低 3 点中位数）；
  2) 个人相对项：深睡、HR dip、阶段心率梯度、压力；
     基线满 14 晚后用个人百分位映射：5%→40、25%→60、50%→75、75%→90、95%→100；
     压力方向相反（越低越好）；基线不足退回固定参考。
  3) 合成：按时长 30 / 连续性 15 / 深睡 15 / HR dip 20 /
     阶段梯度 5 / 血氧 10 / 压力 5 加权；缺失项重归一。
置信度：权重覆盖率 ≥85%、含 HR dip、个人项已启用且基线满 14 晚 → 高。
样本门槛：睡眠心率 ≥10 条、阶段心率各 ≥3 条、SpO2 ≥3 点、压力 ≥3 点。

数据来源：
  - 睡眠账本 data/sleep/sleep.json（时长/目标/深睡/夜醒/效率）
  - 手表归档 data/health/watch/<date>.json（睡眠窗口 HR、分阶段 HR、血氧、压力）
  - 前一天归档的白天心率（算 HR dip 用的日间静息）

输出：data/health/recovery/<date>.json
CLI：
  python -X utf8 scripts/recovery_score.py --date 2026-10-31 [--json] [--save]
  python -X utf8 scripts/recovery_score.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path(__file__).resolve().parents[2])
WS = ROOT / "workspace"
sys.path.insert(0, str(WS / "scripts"))

WATCH_DIR = WS / "data" / "health" / "watch"
OUT_DIR = WS / "data" / "health" / "recovery"
BASELINE_DAYS = 28
PERSONAL_MIN_NIGHTS = 14
DEEP_REF_SHARE = 0.18
MIN_SLEEP_HR_SAMPLES = 10
MIN_STAGE_HR_SAMPLES = 3
MIN_SPO2_SAMPLES = 3
MIN_STRESS_SAMPLES = 3
PERCENTILE_ANCHORS = [
    (0.00, 20),
    (0.05, 40),
    (0.25, 60),
    (0.50, 75),
    (0.75, 90),
    (0.95, 100),
    (1.00, 100),
]

WEIGHTS = {
    "duration": 30,
    "continuity": 15,
    "deep": 15,
    "hr_dip": 20,
    "stage_gradient": 5,
    "spo2": 10,
    "stress": 5,
}


def _clamp(v: float, lo=0.0, hi=100.0) -> float:
    return max(lo, min(hi, v))


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _date_range(end: str, days: int) -> list[str]:
    d = datetime.strptime(end, "%Y-%m-%d")
    return [(d - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, days + 1)]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * q))))
    return vals[idx]


def _wake_window(day: dict) -> tuple[int, int] | None:
    sessions = day.get("sleep") or []
    if not sessions:
        return None
    main = max(sessions, key=lambda x: x["end"] - x["start"])
    return main["start"], main["end"]


def _daytime_resting_hr(prev_day: dict | None) -> float | None:
    """前一天白天（睡眠窗口外）心率低分位 ≈ 日间静息。"""
    if not prev_day:
        return None
    win = _wake_window(prev_day)
    vals = []
    for h in prev_day.get("heart_rate", []):
        if h.get("bpm") and (not win or not (win[0] <= h["ts"] <= win[1])):
            vals.append(h["bpm"])
    return round(_pct(vals, 0.10), 1) if len(vals) >= 10 else None


def _window_from_record(rec: dict) -> tuple[int, int] | None:
    try:
        d = datetime.strptime(rec["date"], "%Y-%m-%d")
        bh, bm = map(int, rec["bedtime"].split(":"))
        wh, wm = map(int, rec["wake"].split(":"))
        bed = d.replace(hour=bh, minute=bm, second=0)
        wake = d.replace(hour=wh, minute=wm, second=0)
        if bed >= wake:
            bed -= timedelta(days=1)
        return int(bed.timestamp()), int(wake.timestamp())
    except Exception:
        return None


def _sleep_window_stats(day: dict, rec: dict | None = None) -> dict:
    """睡眠窗口 HR 均值/分阶段均值/血氧/压力；无手表会话时退回睡眠账本的起止时间。"""
    out = {"sleep_hr_avg": None, "deep_hr": None, "light_hr": None,
           "spo2_min": None, "spo2_low": None, "stress_avg": None,
           "sleep_hr_n": 0, "deep_hr_n": 0, "light_hr_n": 0,
           "spo2_n": 0, "stress_n": 0}
    win = _wake_window(day) or (_window_from_record(rec) if rec else None)
    if not win:
        return out
    st, en = win
    hr = [h for h in day.get("heart_rate", []) if st <= h["ts"] <= en and h.get("bpm")]
    out["sleep_hr_n"] = len(hr)
    if len(hr) >= MIN_SLEEP_HR_SAMPLES:
        out["sleep_hr_avg"] = round(sum(h["bpm"] for h in hr) / len(hr), 1)
    stage_vals: dict[int, list[int]] = {}
    sessions = day.get("sleep") or []
    stages = (max(sessions, key=lambda s: s["end"] - s["start"]).get("stages") or []) if sessions else []
    for x in stages:
        w1 = x["ts"] + x.get("duration_s", 0)
        stage_vals.setdefault(x.get("stage"), []).extend(
            h["bpm"] for h in hr if x["ts"] <= h["ts"] < w1)
    out["deep_hr_n"] = len(stage_vals.get(1, []))
    out["light_hr_n"] = len(stage_vals.get(2, []))
    if len(stage_vals.get(1, [])) >= MIN_STAGE_HR_SAMPLES:
        out["deep_hr"] = round(sum(stage_vals[1]) / len(stage_vals[1]), 1)
    if len(stage_vals.get(2, [])) >= MIN_STAGE_HR_SAMPLES:
        out["light_hr"] = round(sum(stage_vals[2]) / len(stage_vals[2]), 1)
    spo2 = [x["percent"] for x in day.get("spo2", []) if st <= x["ts"] <= en and x.get("percent")]
    out["spo2_n"] = len(spo2)
    if spo2:
        out["spo2_min"] = min(spo2)
        if len(spo2) >= MIN_SPO2_SAMPLES:
            out["spo2_low"] = round(statistics.median(sorted(spo2)[:3]), 1)
    stress = [x["value"] for x in day.get("stress", []) if st <= x["ts"] <= en and x.get("value")]
    out["stress_n"] = len(stress)
    if len(stress) >= MIN_STRESS_SAMPLES:
        out["stress_avg"] = round(sum(stress) / len(stress), 1)
    return out


def _history(store: dict, date: str) -> dict:
    """个人基线：近 28 天（不含当天）的深睡占比、睡眠 HR、HR dip、梯度、压力、血氧。"""
    deep_shares, sleep_hrs, dips, gradients, stresses, spo2_mins = [], [], [], [], [], []
    for d in _date_range(date, BASELINE_DAYS):
        rec = store.get("records", {}).get(d)
        day = _load(WATCH_DIR / f"{d}.json")
        if rec and rec.get("duration_min") and rec.get("deep_min_est") is not None:
            deep_shares.append(rec["deep_min_est"] / rec["duration_min"])
        if not day:
            continue
        s = _sleep_window_stats(day, rec)
        if s["sleep_hr_avg"]:
            sleep_hrs.append(s["sleep_hr_avg"])
        if s["deep_hr"] and s["light_hr"]:
            gradients.append(s["light_hr"] - s["deep_hr"])
        if s["stress_avg"]:
            stresses.append(s["stress_avg"])
        if s["spo2_min"]:
            spo2_mins.append(s["spo2_min"])
        prev = _load(WATCH_DIR / f"{_date_range(d, 1)[0]}.json")
        day_hr = _daytime_resting_hr(prev)
        if day_hr and s["sleep_hr_avg"]:
            dips.append((day_hr - s["sleep_hr_avg"]) / day_hr)
    return {"deep_shares": deep_shares, "sleep_hrs": sleep_hrs, "dips": dips,
            "gradients": gradients, "stresses": stresses, "spo2_mins": spo2_mins,
            "nights": len(deep_shares)}


def _score_duration(rec: dict) -> float | None:
    dur, target = rec.get("duration_min"), rec.get("target_min")
    if not dur or not target:
        return None
    return _clamp(100.0 * dur / target)


def _score_continuity(rec: dict) -> float | None:
    dur, bed, wake = rec.get("duration_min"), rec.get("bedtime"), rec.get("wake")
    if not dur or not bed or not wake:
        return None
    h1, m1 = map(int, bed.split(":"))
    h2, m2 = map(int, wake.split(":"))
    in_bed = (h2 * 60 + m2 - h1 * 60 - m1) % 1440
    if in_bed <= 0:
        return None
    eff = min(1.0, dur / in_bed)
    if eff >= 0.95:
        base = 100.0
    elif eff >= 0.90:
        base = 85 + (eff - 0.90) / 0.05 * 15
    elif eff >= 0.85:
        base = 70 + (eff - 0.85) / 0.05 * 15
    elif eff >= 0.80:
        base = 50 + (eff - 0.80) / 0.05 * 20
    else:
        base = max(0.0, 50 * eff / 0.80)
    wakes = rec.get("night_wake_count")
    if wakes is not None:
        base -= min(20, max(0, int(wakes) - 1) * 8)
    return _clamp(base)


def _percentile_rank(value: float, hist: list[float]) -> float | None:
    if not hist:
        return None
    less = sum(1 for x in hist if x < value)
    equal = sum(1 for x in hist if x == value)
    return (less + 0.5 * equal) / len(hist)


def _map_percentile(p: float) -> float:
    p = max(0.0, min(1.0, p))
    for (p0, s0), (p1, s1) in zip(PERCENTILE_ANCHORS, PERCENTILE_ANCHORS[1:]):
        if p <= p1:
            if p1 <= p0:
                return float(s1)
            return _clamp(s0 + (s1 - s0) * (p - p0) / (p1 - p0))
    return float(PERCENTILE_ANCHORS[-1][1])


def _percentile_score(value: float, hist: list[float], higher_better: bool = True) -> float | None:
    p = _percentile_rank(value, hist)
    if p is None:
        return None
    if not higher_better:
        p = 1.0 - p
    return _map_percentile(p)


def _score_deep(rec: dict, hist: dict) -> tuple[float, bool] | None:
    dur, deep = rec.get("duration_min"), rec.get("deep_min_est")
    if not dur or deep is None:
        return None
    share = deep / dur
    if len(hist["deep_shares"]) >= PERSONAL_MIN_NIGHTS:
        score = _percentile_score(share, hist["deep_shares"], True)
        if score is not None:
            return score, True
    return _clamp(100 * share / DEEP_REF_SHARE), False


def _score_hr_dip(dip: float | None, hist: dict) -> tuple[float, bool] | None:
    if dip is None:
        return None
    if len(hist["dips"]) >= PERSONAL_MIN_NIGHTS:
        score = _percentile_score(dip, hist["dips"], True)
        if score is not None:
            return score, True
    return _clamp(100 * dip / 0.12), False


def _score_stage_gradient(grad: float | None, hist: dict) -> tuple[float, bool] | None:
    if grad is None:
        return None
    if len(hist["gradients"]) >= PERSONAL_MIN_NIGHTS:
        score = _percentile_score(grad, hist["gradients"], True)
        if score is not None:
            return score, True
    return _clamp(50 + 5 * grad), False


def _score_stress(avg: float | None, hist: dict) -> tuple[float, bool] | None:
    if avg is None:
        return None
    if len(hist["stresses"]) >= PERSONAL_MIN_NIGHTS:
        score = _percentile_score(avg, hist["stresses"], False)
        if score is not None:
            return score, True
    if avg <= 29:
        score = 100.0
    elif avg <= 59:
        score = 80 - (avg - 29) / 30 * 20
    elif avg <= 79:
        score = 60 - (avg - 59) / 20 * 40
    else:
        score = 20 - (avg - 79) * 2
    return _clamp(score), False


def compute(date: str) -> dict:
    import sleep_store

    store = sleep_store.load_store()
    rec = store.get("records", {}).get(date)
    day = _load(WATCH_DIR / f"{date}.json")
    hist = _history(store, date)

    parts: dict[str, dict] = {}
    if rec and day:
        s = _sleep_window_stats(day, rec)
        prev = _load(WATCH_DIR / f"{_date_range(date, 1)[0]}.json")
        day_hr = _daytime_resting_hr(prev)

        v = _score_duration(rec)
        if v is not None:
            parts["duration"] = {"score": round(v)}
        v = _score_continuity(rec)
        if v is not None:
            parts["continuity"] = {"score": round(v)}
        v = _score_deep(rec, hist)
        if v is not None:
            parts["deep"] = {"score": round(v[0]), "personal": v[1]}

        # HR dip：白天静息 vs 睡眠均值；缺白天数据时退化为睡眠心率相对个人基线
        dip = None
        if day_hr and s["sleep_hr_avg"]:
            dip = (day_hr - s["sleep_hr_avg"]) / day_hr
        v = _score_hr_dip(dip, hist)
        if v is not None:
            parts["hr_dip"] = {"score": round(v[0]), "dip": round(dip, 3),
                               "day_resting": day_hr, "sleep_avg": s["sleep_hr_avg"],
                               "personal": v[1]}
        elif s["sleep_hr_avg"] and len(hist["sleep_hrs"]) >= PERSONAL_MIN_NIGHTS:
            score = _percentile_score(s["sleep_hr_avg"], hist["sleep_hrs"], False)
            if score is not None:
                parts["hr_dip"] = {"score": round(score), "sleep_avg": s["sleep_hr_avg"],
                                   "fallback": "sleep_hr_vs_baseline", "personal": True}

        if s["deep_hr"] and s["light_hr"]:
            grad = s["light_hr"] - s["deep_hr"]
            v = _score_stage_gradient(grad, hist)
            if v is not None:
                parts["stage_gradient"] = {"score": round(v[0]), "gradient": round(grad, 1),
                                           "personal": v[1]}

        if s["spo2_low"] is not None and s["spo2_n"] >= MIN_SPO2_SAMPLES:
            low = s["spo2_low"]
            if low >= 95:
                score = 100.0
            elif low >= 90:
                score = 60 + (low - 90) * 8
            else:
                score = max(0.0, 60 - (90 - low) * 6)
            parts["spo2"] = {"score": round(score), "low": low,
                             "min": s["spo2_min"], "n": s["spo2_n"]}

        if s["stress_avg"] is not None:
            v = _score_stress(s["stress_avg"], hist)
            if v is not None:
                parts["stress"] = {"score": round(v[0]), "avg": s["stress_avg"],
                                   "n": s["stress_n"], "personal": v[1]}

    present = [k for k in WEIGHTS if k in parts]
    wsum = sum(WEIGHTS[k] for k in present)
    weight_cov = wsum / sum(WEIGHTS.values())
    score = round(sum(WEIGHTS[k] * parts[k]["score"] for k in present) / wsum) if wsum else None

    baseline_parts = [k for k in ("deep", "hr_dip", "stage_gradient", "stress") if k in parts]
    personal_ok = len(baseline_parts) >= 3 and all(parts[k].get("personal") for k in baseline_parts)
    hr_dip_ok = "hr_dip" in parts and "dip" in parts["hr_dip"]
    if weight_cov >= 0.85 and hr_dip_ok and personal_ok and hist["nights"] >= PERSONAL_MIN_NIGHTS:
        confidence = "高"
    elif weight_cov >= 0.55:
        confidence = "中"
    else:
        confidence = "低"

    unavailable = None
    if not rec:
        unavailable = "今日无睡眠记录"
    elif not day:
        unavailable = "缺手表归档"
    elif "hr_dip" not in parts:
        unavailable = "缺夜间心率"

    return {
        "date": date,
        "score": score if len(present) >= 4 else None,
        "confidence": confidence,
        "unavailable": unavailable if len(present) < 4 else None,
        "components": parts,
        "weights": {k: WEIGHTS[k] for k in present},
        "missing": [k for k in WEIGHTS if k not in parts],
        "weight_coverage": round(weight_cov, 2),
        "baseline_nights": hist["nights"],
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": "个人参考，非医学指标",
    }


def save(result: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{result['date']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def week_stats(end_date: str) -> dict | None:
    d = datetime.strptime(end_date, "%Y-%m-%d")
    start = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
    scores = []
    cur = datetime.strptime(start, "%Y-%m-%d")
    for _ in range(7):
        row = _load(OUT_DIR / f"{cur.strftime('%Y-%m-%d')}.json")
        if row and row.get("score") is not None:
            scores.append(row["score"])
        cur += timedelta(days=1)
    if not scores:
        return None
    return {"days": len(scores), "avg": round(sum(scores) / len(scores)),
            "max": max(scores), "min": min(scores)}


def selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    check("百分位中位数→75", abs(_map_percentile(0.50) - 75) < 0.01)
    check("百分位25→60", abs(_map_percentile(0.25) - 60) < 0.01)
    check("百分位75→90", abs(_map_percentile(0.75) - 90) < 0.01)
    hist5 = [1, 2, 3, 4, 5]
    check("并列中位数按50%", abs(_percentile_rank(3, hist5) - 0.5) < 1e-9)
    check("越高越好方向", _percentile_score(5, hist5, True) > _percentile_score(1, hist5, True))
    check("压力反向方向", _percentile_score(50, hist5, False) < _percentile_score(1, hist5, False))

    share = 86.4 / 480
    rec = {"duration_min": 480, "target_min": 480, "bedtime": "23:00",
           "wake": "07:00", "deep_min_est": 86.4}
    hist13 = {"deep_shares": [share] * 13, "dips": [0.08] * 13,
              "gradients": [2.0] * 13, "stresses": [40.0] * 13, "nights": 13}
    hist14 = {"deep_shares": [share] * 14, "dips": [0.08] * 14,
              "gradients": [2.0] * 14, "stresses": [40.0] * 14, "nights": 14}

    v = _score_deep(rec, hist13)
    check("深睡基线不足→绝对参考", v is not None and v[1] is False and v[0] == 100)
    v = _score_deep(rec, hist14)
    check("深睡基线足够→个人百分位", v is not None and v[1] is True and abs(v[0] - 75) < 0.01)
    v = _score_hr_dip(0.08, hist13)
    check("HR dip基线不足→绝对参考", v is not None and v[1] is False and abs(v[0] - 66.67) < 0.1)
    v = _score_hr_dip(0.08, hist14)
    check("HR dip基线足够→个人百分位", v is not None and v[1] is True and abs(v[0] - 75) < 0.01)
    v = _score_stage_gradient(2.0, hist14)
    check("阶段梯度基线足够→个人百分位", v is not None and v[1] is True and abs(v[0] - 75) < 0.01)
    v = _score_stress(40.0, hist14)
    check("压力基线足够→个人百分位", v is not None and v[1] is True and abs(v[0] - 75) < 0.01)

    st = datetime(2026, 1, 2, 23, 0).timestamp()
    en = datetime(2026, 1, 3, 7, 0).timestamp()
    day = {
        "sleep": [{"start": st, "end": en, "stages": [
            {"ts": st, "duration_s": 1800, "stage": 1},
            {"ts": st + 1800, "duration_s": 1800, "stage": 2},
        ]}],
        "heart_rate": [{"ts": st + i * 300, "bpm": 60 + i % 3} for i in range(96)],
        "spo2": [{"ts": st + i * 3600, "percent": p} for i, p in enumerate([88, 90, 96, 97])],
        "stress": [{"ts": st + i * 3600, "value": v} for i, v in enumerate([30, 40, 50])],
    }
    s = _sleep_window_stats(day)
    check("SpO2 用最低3点中位数", s["spo2_min"] == 88 and s["spo2_low"] == 90)
    check("SpO2 样本数记录", s["spo2_n"] == 4)
    check("压力样本数记录", s["stress_n"] == 3)
    check("睡眠心率样本数记录", s["sleep_hr_n"] == 96)
    check("深睡/浅睡心率样本数记录", s["deep_hr_n"] >= 3 and s["light_hr_n"] >= 3)

    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="睡眠恢复分（个人参考）")
    ap.add_argument("--date", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    result = compute(date)
    if args.save:
        save(result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["score"] is None:
            print(f"🔋 恢复分暂不可用（{result.get('unavailable') or '数据不足'}）")
        else:
            suffix = "" if result["confidence"] == "高" else f"（数据可信度：{result['confidence']}）"
            print(f"🔋 恢复分：{result['score']}{suffix}")
            for k, v in result["components"].items():
                print(f"  {k}: {v['score']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
