# -*- coding: utf-8 -*-
"""energy_score.py — 精力分（自评采集 + 校准中冷启动 + 个人模型，0–100；非医学指标）

用户输入：每天回复 1–5（1=很差，5=充沛）；也接受 0–100（自动折档并提示确认）。
存储：data/health/energy/YYYY-MM-DD.json（自评）、onboarding.json（5 题先验）、
      model.json（个人模型）、predictions.json（仅个人模型预测）
规则：
  - 同日重复以最后一次为准；不追问、不惩罚
  - 个人模型未验证前：cold_start 只用恢复分 + 近 7 天睡眠债变化 + 问卷先验，
    输出「校准中」分数与 1–2 个初步高能窗口；自评仅作训练标签，不外推为个人模型
  - 满 60 天配对数据才训练；时间外验证（后 20% 天）MAE ≤10 且优于「照抄昨天」才启用
  - 个人模型输出按 3 日平滑，显示整数 + 分档（≥80 充沛 / 60–79 一般 / <60 偏低）
CLI：
  python -X utf8 scripts/energy_score.py log --text "4"
  python -X utf8 scripts/energy_score.py event --text "现在有点困" [--date YYYY-MM-DD] [--time HH:MM]
  python -X utf8 scripts/energy_score.py events [--date YYYY-MM-DD] [--json]
  python -X utf8 scripts/energy_score.py progress [--json]
  python -X utf8 scripts/energy_score.py train [--json]
  python -X utf8 scripts/energy_score.py predict [--date YYYY-MM-DD] [--json]
  python -X utf8 scripts/energy_score.py selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path(__file__).resolve().parents[2])
WS = ROOT / "workspace"
sys.path.insert(0, str(WS / "scripts"))

ENERGY_DIR = Path(os.environ.get("OPENCLAW_ENERGY_DIR") or (WS / "data" / "health" / "energy"))
MODEL_PATH = ENERGY_DIR / "model.json"
PRED_PATH = ENERGY_DIR / "predictions.json"
EVENTS_PATH = ENERGY_DIR / "events.jsonl"
ONBOARD_PATH = ENERGY_DIR / "onboarding.json"
MIN_DAYS = 60
TRAIN_MIN_DAYS = 45
BANDS = [(80, "充沛"), (60, "一般"), (0, "偏低")]
LEVEL_SCORE = {1: 20, 2: 40, 3: 60, 4: 80, 5: 100}
WORDS = [
    (1, ("精疲力尽", "虚脱", "撑不住", "极差")),
    (2, ("很累", "疲惫", "挺累", "很差", "累")),
    (3, ("一般", "还行", "普通", "凑合", "正常", "困", "犯困", "瞌睡")),
    (4, ("不错", "挺好", "很好", "有精神", "可以")),
    (5, ("充沛", "满满", "超好", "特别好")),
]


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_rating(text: str) -> dict:
    """解析 1–5 或 0–100；返回 {level, mapped_from_100, raw}。"""
    s = (text or "").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if m:
        val = float(m.group(0))
        if 1 <= val <= 5 and val == int(val):
            return {"level": int(val), "mapped_from_100": False, "raw": s}
        if 10 <= val <= 100:
            # 百分制折档：40/60/80/100 附近 → 2/3/4/5，20 以下 → 1
            level = min(5, max(1, round(val / 20)))
            return {"level": level, "mapped_from_100": True, "raw": s, "value100": int(val)}
    for level, words in WORDS:
        if any(w in s for w in words):
            return {"level": level, "mapped_from_100": False, "raw": s}
    return {"error": "无法识别精力分：只接受 1–5 或 0–100，或「一般/不错/很累」这类描述", "raw": s}


def log_rating(text: str = "", level: int | None = None, date: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    if level is None:
        parsed = parse_rating(text)
        if "error" in parsed:
            return parsed
        level = parsed["level"]
        meta = parsed
    else:
        if not 1 <= int(level) <= 5:
            return {"error": "level 必须是 1–5"}
        level = int(level)
        meta = {"level": level, "mapped_from_100": False, "raw": str(level)}
    rec = {
        "date": date,
        "level": level,
        "score": LEVEL_SCORE[level],
        "raw": meta.get("raw"),
        "mapped_from_100": meta.get("mapped_from_100", False),
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    old = _load(ENERGY_DIR / f"{date}.json")
    _save(ENERGY_DIR / f"{date}.json", rec)
    return {"ok": True, "record": rec, "replaced": bool(old),
            "confirm": ("已按百分制折算为 %d 档（%d 分）；如果想改成别的档位直接再说一个数" % (level, LEVEL_SCORE[level]))
                       if meta.get("mapped_from_100") else None}


TAGS = ["刚下课", "上课", "考试", "咖啡", "午睡", "运动", "没睡好", "熬夜", "生病", "加班"]
LEVEL_LABEL = {1: "很差", 2: "有点累", 3: "一般", 4: "不错", 5: "充沛"}


def log_event(text: str, date: str | None = None, time_text: str | None = None) -> dict:
    """随时上报的精力事件：只追加、不覆盖当天整体自评；同日期/时间/原文去重。"""
    parsed = parse_rating(text)
    if "error" in parsed:
        return parsed
    now = datetime.now()
    target_date = date or now.strftime("%Y-%m-%d")
    try:
        target_day = datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "日期格式应为 YYYY-MM-DD", "raw": target_date}
    time_estimated = False
    if time_text:
        try:
            hh, mm = map(int, time_text.split(":"))
            event_dt = target_day.replace(hour=hh, minute=mm)
        except Exception:
            return {"error": "时间格式应为 HH:MM", "raw": time_text}
    elif target_date == now.strftime("%Y-%m-%d"):
        event_dt = now
    else:
        event_dt = target_day.replace(hour=12, minute=0)
        time_estimated = True
    tags = [x for x in TAGS if x in (text or "")]
    event = {
        "date": target_date,
        "ts": int(event_dt.timestamp()),
        "time": event_dt.strftime("%H:%M"),
        "level": parsed["level"],
        "score": LEVEL_SCORE[parsed["level"]],
        "raw": text,
        "tags": tags,
        "mapped_from_100": parsed.get("mapped_from_100", False),
        "source": "spontaneous",
        "time_provided": bool(time_text),
        "time_estimated": time_estimated,
        "recorded_at": now.strftime("%Y-%m-%d %H:%M"),
    }
    for prev in events(target_date):
        if (prev.get("time") == event["time"] and prev.get("raw") == text
                and prev.get("level") == event["level"]):
            return {"ok": True, "duplicate": True, "event": prev,
                    "confirm": f"这条已经记过了：{prev.get('time')} {LEVEL_LABEL[prev['level']]}"}
    ENERGY_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    suffix = "（未给具体时间，按中午记录）" if time_estimated else ""
    return {"ok": True, "event": event,
            "confirm": f"记下了：{event['time']} {LEVEL_LABEL[event['level']]}"
                       + (f"（{'、'.join(tags)}）" if tags else "") + suffix}


def events(date: str | None = None) -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    out = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if date is None or row.get("date") == date:
            out.append(row)
    return out


def event_stats(date: str | None = None) -> dict:
    all_rows = events()
    rows = events(date) if date else all_rows
    days = sorted({r.get("date") for r in all_rows if r.get("date")})
    hours = sorted({int(r["time"][:2]) for r in rows if r.get("time")})
    return {"date": date, "count": len(rows), "days": len(days),
            "hours": hours, "last": rows[-1] if rows else None}


def ratings() -> dict[str, dict]:
    out = {}
    if not ENERGY_DIR.exists():
        return out
    for p in ENERGY_DIR.glob("20*.json"):
        row = _load(p)
        if row and row.get("level"):
            out[row["date"]] = row
    return out


def progress(date: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    rows = ratings()
    today = _load(ENERGY_DIR / f"{date}.json")
    recent = [(datetime.strptime(date, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(7)]
    recent_has = any(d in rows for d in recent)
    model = _load(MODEL_PATH) or {}
    resume = _load(ENERGY_DIR / ".prompt-resume") or {}
    resumed = resume.get("date") == date
    ev_all = events()
    ev_today = events(date)
    return {
        "date": date,
        "days": len(rows),
        "days_to_go": max(0, MIN_DAYS - len(rows)),
        "rated_today": bool(today),
        "need_prompt": (today is None) and (recent_has or resumed or len(rows) == 0),
        "model_active": bool(model.get("active")),
        "model_mae": model.get("mae"),
        "event_count": len(ev_all),
        "event_days": len({r.get("date") for r in ev_all if r.get("date")}),
        "events_today": len(ev_today),
        "event_hours_today": sorted({int(r["time"][:2]) for r in ev_today if r.get("time")}),
    }


# ---------------------------------------------------------------- 特征 ----

def _reaction_features(date: str) -> dict:
    out = {"rt_median": None, "rt_lapses": None, "rt_false_starts": None,
           "rt_time_since_wake": None, "rt_missing": 1, "rt_error": None}
    try:
        import reaction_test as rt
        rep = rt.report(date)
        if not rep.get("available"):
            return out
        last = rep.get("last") or {}
        lapses = rep.get("lapses_total")
        if lapses is None:
            lapses = last.get("lapses")
        false_starts = rep.get("false_starts_total")
        if false_starts is None:
            false_starts = last.get("false_starts")
        wake = rep.get("time_since_wake_min")
        if wake is None:
            wake = last.get("time_since_wake_min")
        out.update({
            "rt_median": rep.get("median_ms"),
            "rt_lapses": lapses,
            "rt_false_starts": false_starts,
            "rt_time_since_wake": wake,
            "rt_missing": 0,
        })
    except Exception as e:
        out["rt_error"] = str(e)[:120]
    return out


def _features(date: str) -> dict | None:
    import recovery_score as rs
    import sleep_store

    rec = sleep_store.load_store().get("records", {}).get(date)
    day = rs._load(rs.WATCH_DIR / f"{date}.json")
    if not rec or not day:
        return None
    s = rs._sleep_window_stats(day)
    rs_result = rs.compute(date)
    dur, target = rec.get("duration_min"), rec.get("target_min") or 480
    feat = {
        "recovery": rs_result.get("score"),
        "dur_ratio": (dur / target) if dur else None,
        "deep_share": (rec["deep_min_est"] / dur) if rec.get("deep_min_est") and dur else None,
        "sleep_hr": s.get("sleep_hr_avg"),
        "deep_hr": s.get("deep_hr"),
        "wake_count": rec.get("night_wake_count"),
        "spo2_min": s.get("spo2_min"),
        "stress": s.get("stress_avg"),
        "debt_h": (rec.get("debt_after_min") or 0) / 60,
        "weekday": datetime.strptime(date, "%Y-%m-%d").weekday(),
    }
    feat.update(_reaction_features(date))
    return feat


FEATURES = ["recovery", "dur_ratio", "deep_share", "sleep_hr", "deep_hr", "wake_count",
            "spo2_min", "stress", "debt_h", "weekday", "rt_median", "rt_lapses",
            "rt_false_starts", "rt_time_since_wake", "rt_missing"]


def _rows() -> list[tuple[str, dict, float]]:
    out = []
    for date, row in sorted(ratings().items()):
        feat = _features(date)
        if feat and sum(1 for k in FEATURES if feat.get(k) is not None) >= 8:
            out.append((date, feat, float(row["score"])))
    return out


def _matrix(rows):
    import numpy as np

    cols = []
    for k in FEATURES:
        vals = [r[1].get(k) for r in rows]
        known = [v for v in vals if v is not None]
        med = sorted(known)[len(known) // 2] if known else 0.0
        cols.append([med if v is None else v for v in vals])
    X = np.array(cols, dtype=float).T
    y = np.array([r[2] for r in rows], dtype=float)
    return X, y


def train(verbose=False) -> dict:
    rows = _rows()
    if len(rows) < TRAIN_MIN_DAYS:
        return {"active": False, "reason": f"数据不足（{len(rows)}/{TRAIN_MIN_DAYS} 天）", "n": len(rows)}
    try:
        import numpy as np
        from sklearn.linear_model import Ridge
        from sklearn.metrics import mean_absolute_error
    except Exception as e:
        return {"active": False, "reason": f"缺建模库：{e}", "n": len(rows)}

    X, y = _matrix(rows)
    split = max(TRAIN_MIN_DAYS - 5, int(len(rows) * 0.8))
    split = min(split, len(rows) - 5)
    Xtr, ytr, Xte, yte = X[:split], y[:split], X[split:], y[split:]
    model = Ridge(alpha=1.0)
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)
    mae = float(mean_absolute_error(yte, pred))
    naive = y[split - 1]
    naive_mae = float(np.mean(np.abs(yte - naive)))
    active = (len(rows) >= MIN_DAYS) and mae <= 10.0 and mae <= naive_mae - 1.0
    out = {
        "active": bool(active),
        "n": len(rows),
        "train_n": int(split),
        "test_n": len(yte),
        "mae": round(mae, 3),
        "naive_mae": round(naive_mae, 3),
        "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "coef": {k: round(float(c), 4) for k, c in zip(FEATURES, model.coef_)},
        "intercept": round(float(model.intercept_), 3),
        "features": FEATURES,
    }
    _save(MODEL_PATH, out)
    if verbose:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def _onboarding() -> dict:
    if ONBOARD_PATH.exists():
        try:
            data = json.loads(ONBOARD_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"chronotype": 3, "post_lunch_sleepiness": 1,
            "weekday_wake": "07:30", "weekend_wake": "09:00",
            "natural_sleep_min": 480}


def _hm_to_min(text: str) -> int | None:
    try:
        h, m = text.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _cold_windows(date: str, rec: dict, debt_trend: dict, ob: dict) -> dict:
    wake = _hm_to_min(rec.get("wake", ""))
    if wake is None:
        weekend = datetime.strptime(date, "%Y-%m-%d").weekday() >= 5
        wake = _hm_to_min(ob.get("weekend_wake" if weekend else "weekday_wake", "07:30")) or 450
    try:
        ctype = int(ob.get("chronotype", 3))
    except Exception:
        ctype = 3
    shift = {1: -30, 2: -15, 3: 0, 4: 30, 5: 60}.get(ctype, 0)
    lunch = int(ob.get("post_lunch_sleepiness", 1) or 0)
    main_start = wake + 90 + shift
    main_end = main_start + 120
    aft_start = 15 * 60 + 30 + shift + (30 if lunch >= 2 else 0)
    aft_end = aft_start + 120
    delta = (debt_trend or {}).get("delta_min")
    if delta is not None and delta <= -120:
        main_end -= 30
        aft_start += 30
        aft_end += 30
    lo, hi = 6 * 60, 22 * 60
    windows = []
    for s, e in ((main_start, main_end), (aft_start, aft_end)):
        s, e = max(lo, min(s, hi)), max(lo, min(e, hi))
        if e - s >= 60:
            windows.append([s, e])
    windows.sort()
    merged = []
    for s, e in windows:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    merged.sort(key=lambda x: x[1] - x[0], reverse=True)
    merged = sorted(merged[:2])
    out = [{"start": f"{s // 60:02d}:{s % 60:02d}",
            "end": f"{e // 60:02d}:{e % 60:02d}",
            "minutes": e - s} for s, e in merged]
    return {"windows": out, "total_min": sum(x["minutes"] for x in out)}


def _template_high_energy(date: str, store: dict | None = None,
                         rec: dict | None = None, debt_trend: dict | None = None) -> dict | None:
    """高能模板：校准期主输出 + 长期 fallback；仅当时间标签足够且曲线通过时间外验证后才切为 curve。"""
    import sleep_store

    if store is None:
        store = sleep_store.load_store()
    if rec is None:
        rec = store.get("records", {}).get(date)
    if not rec:
        return None
    if debt_trend is None:
        debt_trend = _debt_trend_penalty(store, date)
    return _cold_windows(date, rec, debt_trend, _onboarding())


DEBT_TREND_DAYS = 7
DEBT_TREND_MAX_PENALTY = 4.0


def _debt_trend_penalty(store: dict, date: str) -> dict:
    """只惩罚最近 7 天新增的欠债，不按近两周存量重复扣分；最多 4 分。"""
    import sleep_store

    past = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=DEBT_TREND_DAYS)).strftime("%Y-%m-%d")
    if not any(d <= past for d in store.get("records", {})):
        return {"penalty": 0.0, "delta_min": None}
    debt_now = sleep_store._debt_2w(store, date)
    debt_past = sleep_store._debt_2w(store, past)
    delta = debt_now - debt_past
    penalty = min(DEBT_TREND_MAX_PENALTY, max(0, -delta) / 60.0 * 0.5)
    return {"penalty": round(penalty, 2), "delta_min": int(delta)}


def cold_start(date: str | None = None) -> dict:
    """模型验证前的校准中精力分（恢复分 + 近 7 天债变化 + 问卷先验；自评只作训练标签）。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    import sleep_store
    import recovery_score

    store = sleep_store.load_store()
    rec = store.get("records", {}).get(date)
    if not rec:
        return {"available": False, "reason": "缺当日睡眠记录", "date": date,
                "provisional": True}
    base = None
    try:
        rr = recovery_score.compute(date)
        base = rr.get("score")
    except Exception:
        base = None
    if base is None:
        dur = rec.get("duration_min") or 0
        target = rec.get("target_min") or 480
        base = 55 + 25 * min(1.0, dur / target)
    try:
        debt_2w = sleep_store._debt_2w(store, date)
    except Exception:
        debt_2w = 0
    trend = _debt_trend_penalty(store, date)
    score = base - trend["penalty"]
    score = int(round(max(0.0, min(100.0, score))))
    return {"available": True, "provisional": True, "date": date,
            "score": score, "band": _band(score),
            "high_energy": _template_high_energy(date, store, rec, trend),
            "high_energy_source": "template",
            "calibration_days": len(ratings()),
            "onboarding": ONBOARD_PATH.exists(),
            "debt_2w_min": debt_2w,
            "debt_penalty": trend["penalty"],
            "debt_trend_min": trend["delta_min"]}


def _band(score: int | float) -> str:
    for lo, name in BANDS:
        if score >= lo:
            return name
    return "偏低"


def predict(date: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    prog = progress(date)
    model = _load(MODEL_PATH) or {}
    if not model.get("active"):
        res = cold_start(date)
        res["days"] = prog["days"]
        res["days_to_go"] = prog["days_to_go"]
        return res
    feat = _features(date)
    if not feat:
        return {"available": False, "reason": "缺当日睡眠/手表数据"}
    vals = []
    for k in model["features"]:
        v = feat.get(k)
        vals.append(0.0 if v is None else float(v))
    raw = model["intercept"] + sum(c * v for c, v in zip((model["coef"][k] for k in model["features"]), vals))
    raw = max(0.0, min(100.0, raw))
    preds = _load(PRED_PATH) or {}
    prev = preds.get((datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"))
    score = round(0.6 * raw + 0.4 * prev["score"]) if prev else round(raw)
    score = max(0, min(100, score))
    high_energy = _template_high_energy(date)
    out = {"available": True, "date": date, "score": score, "band": _band(score),
           "raw": round(raw, 1), "model_mae": model.get("mae"),
           "high_energy": high_energy,
           "high_energy_source": "template" if high_energy else None}
    preds[date] = out
    _save(PRED_PATH, preds)
    return out


def week_stats(end_date: str) -> dict | None:
    d = datetime.strptime(end_date, "%Y-%m-%d")
    start = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
    rows = ratings()
    preds = _load(PRED_PATH) or {}
    days, preds_v, actual_v, errors = 0, [], [], []
    cur = datetime.strptime(start, "%Y-%m-%d")
    for _ in range(7):
        key = cur.strftime("%Y-%m-%d")
        if key in preds and preds[key].get("available") and key in rows:
            days += 1
            preds_v.append(preds[key]["score"])
            actual_v.append(rows[key]["score"])
            errors.append(abs(preds[key]["score"] - rows[key]["score"]))
        cur += timedelta(days=1)
    if days == 0:
        return None
    return {"days": days, "mae": round(sum(errors) / len(errors)),
            "pred_avg": round(sum(preds_v) / days), "actual_avg": round(sum(actual_v) / days),
            "pred_min": min(preds_v), "pred_max": max(preds_v)}


# ---------------------------------------------------------------- 自测 ----

def selftest() -> int:
    global ENERGY_DIR, MODEL_PATH, PRED_PATH, EVENTS_PATH
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    with tempfile.TemporaryDirectory() as td:
        ENERGY_DIR = Path(td)
        MODEL_PATH = ENERGY_DIR / "model.json"
        PRED_PATH = ENERGY_DIR / "predictions.json"
        EVENTS_PATH = ENERGY_DIR / "events.jsonl"

        check("解析 4", parse_rating("4")["level"] == 4)
        check("解析 80 → 4 档", parse_rating("80")["level"] == 4 and parse_rating("80")["mapped_from_100"])
        check("解析 一般", parse_rating("今天一般")["level"] == 3)
        check("解析 很累", parse_rating("很累")["level"] == 2)
        check("脏输入报错", "error" in parse_rating("abc"))

        r = log_rating("4", date="2026-10-01")
        check("入库", r["ok"] and r["record"]["score"] == 80)
        r2 = log_rating("2", date="2026-10-01")
        check("同日覆盖", r2["replaced"] and r2["record"]["level"] == 2)
        check("百分制折档提示", "已按百分制折算" in (log_rating("80", date="2026-10-02").get("confirm") or ""))

        prog = progress("2026-10-02")
        check("进度 days=2", prog["days"] == 2 and prog["days_to_go"] == MIN_DAYS - 2)
        check("当天已评不再提醒", prog["rated_today"] is True)

        m = train()
        check("样本不足不训练", m["active"] is False and "数据不足" in m["reason"])
        check("个人模型不使用自评特征", "prev_rating" not in FEATURES)
        p = predict("2026-10-02")
        check("无睡眠记录不出分", p["available"] is False)

        ev = log_event("现在精神很好，刚下课", date="2026-10-02")
        check("事件记录", ev.get("ok") and ev["event"]["tags"] == ["刚下课"])
        check("事件只追加不覆盖日评", len(events("2026-10-02")) == 1 and ratings()["2026-10-02"]["level"] == 4)

        ev2 = log_event("下午有点困", date="2026-10-03", time_text="15:30")
        check("事件可指定时间", ev2.get("ok") and ev2["event"]["time"] == "15:30")
        ev3 = log_event("下午有点困", date="2026-10-03", time_text="15:30")
        check("同一事件不重复写入", ev3.get("duplicate") and len(events("2026-10-03")) == 1)
        check("事件日期格式错误报错", "error" in log_event("有点困", date="2026/10/03"))
        st = event_stats("2026-10-03")
        check("事件覆盖统计", st["count"] == 1 and st["hours"] == [15] and st["days"] == 2)
        prog2 = progress("2026-10-03")
        check("progress 含事件覆盖", prog2["event_count"] == 2 and prog2["events_today"] == 1)

        import reaction_test as rt
        old_rt_dir = rt.OUT_DIR
        rt.OUT_DIR = ENERGY_DIR / "reaction"
        rt.OUT_DIR.mkdir(parents=True, exist_ok=True)
        (rt.OUT_DIR / "2026-10-05.json").write_text(json.dumps({
            "date": "2026-10-05",
            "tests": [
                {"median_ms": 250, "min_ms": 200, "lapses": 1,
                 "false_starts": 0, "time_since_wake_min": 120},
                {"median_ms": 300, "min_ms": 240, "lapses": 2,
                 "false_starts": 3, "time_since_wake_min": 180},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        try:
            rf = _reaction_features("2026-10-05")
            rf_missing = _reaction_features("2026-10-06")
        finally:
            rt.OUT_DIR = old_rt_dir
        check("反应力中位数入特征", rf["rt_median"] == 275 and rf["rt_missing"] == 0)
        check("反应力漏拍按日报聚合", rf["rt_lapses"] == 3)
        check("反应力误点按日报聚合", rf["rt_false_starts"] == 3)
        check("反应力距起床时间入特征", rf["rt_time_since_wake"] == 150)
        check("反应力缺失标记", rf_missing["rt_missing"] == 1 and rf_missing["rt_median"] is None)
        check("反应力特征已注册",
              all(k in FEATURES for k in ("rt_median", "rt_lapses", "rt_false_starts",
                                          "rt_time_since_wake", "rt_missing")))
        X, y = _matrix([("d1", {k: 1.0 for k in FEATURES}, 60.0),
                        ("d2", {k: 2.0 for k in FEATURES}, 70.0)])
        check("训练矩阵含反应力特征", X.shape[1] == len(FEATURES) and len(y) == 2)

        sentinel = {"windows": [{"start": "09:00", "end": "11:00", "minutes": 120}], "total_min": 120}
        orig_features = globals()["_features"]
        orig_template = globals()["_template_high_energy"]
        _save(MODEL_PATH, {"active": True, "features": FEATURES,
                           "coef": {k: 0.0 for k in FEATURES},
                           "intercept": 70.0, "mae": 5.0})
        try:
            globals()["_features"] = lambda _d: {k: 0.0 for k in FEATURES}
            globals()["_template_high_energy"] = lambda _d: sentinel
            p_active = predict("2026-10-04")
        finally:
            globals()["_features"] = orig_features
            globals()["_template_high_energy"] = orig_template
        check("active 分支保留高能窗口", p_active.get("high_energy") == sentinel)
        check("active 分支标记模板来源", p_active.get("high_energy_source") == "template")

        win_rec = {"wake": "07:30"}
        win_ob = {"chronotype": 3, "post_lunch_sleepiness": 1}
        w_stable = _cold_windows("2026-10-05", win_rec, {"delta_min": 0}, win_ob)
        w_worse = _cold_windows("2026-10-05", win_rec, {"delta_min": -180}, win_ob)
        w_edge = _cold_windows("2026-10-05", win_rec, {"delta_min": -120}, win_ob)
        w_light = _cold_windows("2026-10-05", win_rec, {"delta_min": -119}, win_ob)
        check("高能窗口债稳定→4小时", w_stable["total_min"] == 240)
        check("高能窗口债恶化≥2h→3小时30分钟", w_worse["total_min"] == 210)
        check("高能窗口债恶化2h为边界", w_edge["total_min"] == 210)
        check("高能窗口债恶化<2h不缩", w_light["total_min"] == 240)

        s_trend = {"records": {
            "2026-09-20": {"duration_min": 480, "target_min": 480},
            "2026-09-24": {"duration_min": 480, "target_min": 480},
            "2026-10-01": {"duration_min": 360, "target_min": 480},
        }}
        tr = _debt_trend_penalty(s_trend, "2026-10-01")
        check("近7天债变差才扣分", tr["penalty"] > 0)
        check("债趋势扣分≤4", tr["penalty"] <= DEBT_TREND_MAX_PENALTY)
        s_no_base = {"records": {"2026-10-01": {"duration_min": 360, "target_min": 480}}}
        check("无7天债基线不扣分", _debt_trend_penalty(s_no_base, "2026-10-01")["penalty"] == 0)
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="精力分（自评 + 预测）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("log", help="记录今日精力 1–5（或 0–100）")
    p.add_argument("--text", default="")
    p.add_argument("--level", type=int, default=None)
    p.add_argument("--date", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("event", help="随时上报精力事件（带时间戳，不覆盖当天整体自评）")
    p.add_argument("--text", default="")
    p.add_argument("--date", default=None)
    p.add_argument("--time", default=None, help="事件时间 HH:MM；缺省为当前时间")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("events", help="查看已采集的精力事件覆盖")
    p.add_argument("--date", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("progress", help="采集进度与是否需要提醒")
    p.add_argument("--date", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("train", help="训练并验证模型")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("predict", help="预测某天精力分")
    p.add_argument("--date", default=None)
    p.add_argument("--json", action="store_true")

    sub.add_parser("selftest", help="自测（临时目录，不碰真实数据）")

    args = ap.parse_args(argv)
    if args.cmd == "log":
        res = log_rating(args.text, args.level, args.date)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              (res.get("error") or res.get("confirm") or
               f"已记录 {res['record']['date']} 精力 {res['record']['level']} 档（{res['record']['score']} 分）"))
        return 0 if "error" not in res else 1
    if args.cmd == "event":
        res = log_event(args.text, args.date, args.time)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              (res.get("error") or res.get("confirm")))
        return 0 if "error" not in res else 1
    if args.cmd == "events":
        res = event_stats(args.date)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              f"精力事件：{res['count']} 条，覆盖 {res['days']} 天；小时："
              + ("、".join(f"{h:02d}" for h in res["hours"]) or "无"))
        return 0
    if args.cmd == "progress":
        res = progress(args.date)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              f"精力自评：{res['days']} 天已记录，还差 {res['days_to_go']} 天；今日{'已' if res['rated_today'] else '未'}评；"
              f"事件 {res['event_count']} 条/{res['event_days']} 天")
        return 0
    if args.cmd == "train":
        res = train(verbose=not args.json)
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("active") else 1
    if args.cmd == "predict":
        res = predict(args.date)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              (f"⚡ 精力分{'（校准中）' if res.get('provisional') else ''}：{res['score']} · {res['band']}"
               if res.get("available") else f"精力分不可用（{res.get('reason')}）"))
        return 0
    if args.cmd == "selftest":
        return selftest()
    return 1


if __name__ == "__main__":
    sys.exit(main())
