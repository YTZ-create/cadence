# -*- coding: utf-8 -*-
"""sleep_store.py — 睡眠数据层：入库、去重、派生指标全量重算

数据文件: data/sleep/sleep.json
记录归属: 每条记录按「醒来那天」归属日期（date=起床日）
来源: screenshot | oral（口头仅限补当天，截图可凭图上日期补历史）

派生指标（任何入库后全量重算，保证乱序补录一致）:
  - 动态目标 target(date): <14 天历史=App 固定目标；满 14 天=个人基线±睡眠债修正，
    每日变动 ≤±30min，锁 [420, 540] 分钟
  - 睡眠债 debt(date): Σ clamp(时长-当日目标, ±240min)，滚动累计
  - 质量分 quality(date): 时长达标 .5 + 深睡估算占比 .25 + 规律性 .25（缺因子归一化）
  - 规律性 regularity(date): 近 7 条记录入睡/起床标准差 → 0-100
  - streak(date): 连续达标天数（缺记录日断链）

命令:
  python -X utf8 scripts/sleep_store.py --ingest <record.json>
  python -X utf8 scripts/sleep_store.py --show [date]
  python -X utf8 scripts/sleep_store.py --week <YYYY-MM-DD>  # 该日期所在自然周
  python -X utf8 scripts/sleep_store.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date as D, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path(__file__).resolve().parents[2])
STORE_PATH = ROOT / "workspace" / "data" / "sleep" / "sleep.json"

TZ8 = timezone(timedelta(hours=8))
MIN_PER_DAY = 1440
TARGET_MIN, TARGET_MAX = 420, 540      # 动态目标锁 7~9h
TARGET_DRIFT = 30                      # 每日最大变动 ±30min
DEBT_DAILY_CAP = 240                   # 单日计债 ±4h
DEBT_ALERT = -480                      # 近两周少睡 8h 触发提醒
DEBT_SWITCH_DAYS = 14                  # 动态目标切换所需历史天数
WARMUP_FALLBACK_TARGET = 480           # 无 App 目标时的过渡值
DEEP_REF_SHARE = 0.18                  # 深睡占比参考值
STREAK_MILESTONES = (7, 14, 30)

# ---------------------------------------------------------------- 基础 ----

def load_store() -> dict:
    if STORE_PATH.is_file():
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    return {"records": {}, "app_goal_min": None, "weekly_sent": {}}


def save_store(store: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _hm_to_min(hm: str) -> int | None:
    try:
        h, m = hm.split(":")
        v = int(h) * 60 + int(m)
        if 0 <= v < MIN_PER_DAY:
            return v
    except Exception:
        pass
    return None


def _bed_shifted(hm: str) -> int | None:
    """入睡分钟平移半天（23:30→690、00:40→760），跨午夜均值/标准差专用。"""
    v = _hm_to_min(hm)
    return None if v is None else (v - 720) % MIN_PER_DAY


def _shift_back(v: float) -> float:
    """把平移域的差值折回 [-720, 720]，保证偏差可读。"""
    while v > 720:
        v -= 1440
    while v < -720:
        v += 1440
    return v


def in_bed_minutes(rec: dict) -> int | None:
    """入睡(前一日或当日凌晨)到起床的在床时长。"""
    bed, wake = _hm_to_min(rec.get("bedtime", "")), _hm_to_min(rec.get("wake", ""))
    if bed is None or wake is None:
        return None
    return (wake - bed) % MIN_PER_DAY or MIN_PER_DAY


def sleep_efficiency(rec: dict) -> float | None:
    dur, bed = rec.get("duration_min"), in_bed_minutes(rec)
    if not dur or not bed or bed <= 0:
        return None
    return round(min(100.0, 100.0 * dur / bed), 1)


# ------------------------------------------------------- 派生指标重算 ----

def _baseline(hist: list[dict]) -> int | None:
    """个人基线 = 无约束日(自然醒, 起床≥09:00)时长中位数；无则全体中位数。"""
    if not hist:
        return None
    natural = [r["duration_min"] for r in hist
               if r.get("duration_min") and _hm_to_min(r.get("wake", "")) is not None
               and _hm_to_min(r["wake"]) >= 540]
    pool = natural or [r["duration_min"] for r in hist if r.get("duration_min")]
    return int(median(pool)) if pool else None


def recompute(store: dict) -> dict:
    """按日期顺序递推动态目标与睡眠债；返回 store 本身（已写入派生字段）。"""
    recs = store["records"]
    dates = sorted(recs)
    app_goal = store.get("app_goal_min") or WARMUP_FALLBACK_TARGET
    prev_target = app_goal
    debt = 0
    for d in dates:
        rec = recs[d]
        hist = [recs[x] for x in dates if x < d]
        if len(hist) >= DEBT_SWITCH_DAYS:
            base = _baseline(hist)
            if base:
                adjust = min(max(-debt, 0), 60)          # 欠债越多目标越高，最多 +1h
                desired = min(max(base + adjust, TARGET_MIN), TARGET_MAX)
                prev_target = min(max(prev_target + min(max(desired - prev_target, -TARGET_DRIFT), TARGET_DRIFT),
                                      TARGET_MIN), TARGET_MAX)
        else:
            prev_target = app_goal
        rec["target_min"] = prev_target
        rec["debt_after_min"] = debt = debt + min(max(rec.get("duration_min", 0) - prev_target,
                                                      -DEBT_DAILY_CAP), DEBT_DAILY_CAP)
    store["latest_debt_min"] = debt
    return store


def _std_min(values: list[int]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


def regularity(store: dict, upto: str, window: int = 7) -> int | None:
    """近 window 条记录的入睡/起床稳定度 0-100；记录 <3 条返回 None。"""
    rows = [r for d, r in sorted(store["records"].items()) if d <= upto][-window:]
    if len(rows) < 3:
        return None
    # 入睡时间跨午夜（23:xx 与 00:xx 混合），统一平移半天再算，避免 std 爆炸出负分
    beds = [(m - 720) % MIN_PER_DAY for r in rows
            if (m := _hm_to_min(r.get("bedtime", ""))) is not None]
    wakes = [_hm_to_min(r["wake"]) for r in rows if _hm_to_min(r.get("wake", "")) is not None]
    sb, sw = _std_min(beds), _std_min(wakes)
    if sb is None and sw is None:
        return None
    parts = [max(0.0, 100.0 - v) for v in (sb, sw) if v is not None]
    return int(round(max(0.0, min(100.0, sum(parts) / len(parts)))))


def quality(store: dict, d: str) -> int | None:
    """当日质量分 0-100；无记录返回 None。"""
    rec = store["records"].get(d)
    if not rec:
        return None
    factors = []
    dur, target = rec.get("duration_min"), rec.get("target_min")
    if dur and target:
        factors.append((0.5, min(1.0, dur / target)))
    deep = rec.get("deep_min_est")
    if deep and dur:
        factors.append((0.25, min(1.0, (deep / dur) / DEEP_REF_SHARE)))
    reg = regularity(store, d)
    if reg is not None:
        factors.append((0.25, reg / 100))
    if not factors:
        return None
    wsum = sum(w for w, _ in factors)
    return int(round(max(0.0, min(100.0, 100 * sum(w * v for w, v in factors) / wsum))))


def streak(store: dict, upto: str | None = None) -> int:
    """截至 upto 的连续达标天数；缺记录日断链。"""
    dates = sorted(d for d in store["records"] if upto is None or d <= upto)
    n = 0
    for d in reversed(dates):
        rec = store["records"][d]
        dur, target = rec.get("duration_min"), rec.get("target_min")
        if not dur or not target or dur < target:
            break
        n += 1
        if len(dates) - dates.index(d) >= 2:      # 前一天是否存在（断链检查）
            prev = dates[dates.index(d) - 1]
            if (D.fromisoformat(d) - D.fromisoformat(prev)).days > 1:
                break
    return n


def longest_streak(store: dict) -> int:
    dates = sorted(store["records"])
    best = run = 0
    for i, d in enumerate(dates):
        rec = store["records"][d]
        ok = rec.get("duration_min") and rec.get("duration_min") >= rec.get("target_min", 10 ** 9)
        contiguous = i > 0 and (D.fromisoformat(d) - D.fromisoformat(dates[i - 1])).days == 1
        run = run + 1 if (ok and (i == 0 or contiguous)) else (1 if ok else 0)
        best = max(best, run)
    return best


# ------------------------------------------------------------ 查询 ----

def week_start(d: str) -> str:
    dt = D.fromisoformat(d)
    return (dt - timedelta(days=dt.weekday())).isoformat()   # 周一


def deviations(store: dict, d: str, window: int = 7) -> dict | None:
    """当日入睡/起床/时长相对近 window 天均值的偏差（分钟）。"""
    rows = [r for x, r in sorted(store["records"].items()) if x < d][-window:]
    beds = [v for r in rows if (v := _bed_shifted(r.get("bedtime", ""))) is not None]
    wakes = [_hm_to_min(r["wake"]) for r in rows if _hm_to_min(r.get("wake", "")) is not None]
    durs = [r["duration_min"] for r in rows if r.get("duration_min")]
    rec = store["records"].get(d)
    if not rec or (not beds and not wakes and not durs):
        return None
    out = {}
    sbed = _bed_shifted(rec.get("bedtime", ""))
    wake = _hm_to_min(rec.get("wake", ""))
    if sbed is not None and beds:
        out["bed_min"] = round(_shift_back(sbed - sum(beds) / len(beds)))
    if wake is not None and wakes:
        out["wake_min"] = round(_shift_back(wake - sum(wakes) / len(wakes)))
    if rec.get("duration_min") and durs:
        out["dur_min"] = round(rec["duration_min"] - sum(durs) / len(durs))
    return out or None


def week_payload(store: dict, any_day: str) -> dict:
    """自然周（周一~周日）聚合。"""
    ws = D.fromisoformat(week_start(any_day))
    days = [(ws + timedelta(days=i)).isoformat() for i in range(7)]
    recs = [(d, store["records"][d]) for d in days if d in store["records"]]
    durs = [r["duration_min"] for _, r in recs if r.get("duration_min")]
    beds = [v for _, r in recs if (v := _bed_shifted(r.get("bedtime", ""))) is not None]
    wakes = [_hm_to_min(r["wake"]) for _, r in recs if _hm_to_min(r.get("wake", "")) is not None]
    meets = [d for d, r in recs if r.get("duration_min") and r.get("duration_min") >= r.get("target_min", 10 ** 9)]
    effs = [sleep_efficiency(r) for _, r in recs if sleep_efficiency(r) is not None]
    deep_shares = [r["deep_min_est"] / r["duration_min"] for _, r in recs
                   if r.get("deep_min_est") and r.get("duration_min")]
    wk = [r["duration_min"] for d, r in recs if D.fromisoformat(d).weekday() < 5]
    we = [r["duration_min"] for d, r in recs if D.fromisoformat(d).weekday() >= 5]
    prev_week = [(ws - timedelta(days=i)).isoformat() for i in range(7, 0, -1)]
    prev_recs = [store["records"][d] for d in prev_week if d in store["records"]]
    prev_durs = [r["duration_min"] for r in prev_recs if r.get("duration_min")]
    prev_deeps = [r["deep_min_est"] / r["duration_min"] for r in prev_recs
                  if r.get("deep_min_est") and r.get("duration_min")]
    # 综合分 = 周内日质量均值 .5 + 规律性 .3 + 达标率 .2
    qs = [quality(store, d) for d, _ in recs]
    qs = [q for q in qs if q is not None]
    reg = regularity(store, days[-1])
    comp_parts = []
    if qs:
        comp_parts.append((0.5, sum(qs) / len(qs)))
    if reg is not None:
        comp_parts.append((0.3, reg))
    if recs:
        comp_parts.append((0.2, 100.0 * len(meets) / len(recs)))
    composite = int(round(sum(w * v for w, v in comp_parts) / sum(w for w, _ in comp_parts))) if comp_parts else None
    return {
        "week_start": ws.isoformat(),
        "week_end": days[-1],
        "label": f"第{ws.isocalendar()[1]}周（{ws.month}月{ws.day}日-{(ws + timedelta(days=6)).month}月{(ws + timedelta(days=6)).day}日）",
        "days_recorded": len(recs),
        "days_missing": 7 - len(recs),
        "complete": len(recs) == 7,
        "avg_dur_min": int(round(sum(durs) / len(durs))) if durs else None,
        "meet_days": len(meets),
        "composite": composite,
        "bed_stats": {"mean": int(round((sum(beds) / len(beds) + 720) % MIN_PER_DAY)) if beds else None,
                      "std": int(round(_std_min(beds))) if _std_min(beds) is not None else None,
                      "min": f"{(min(beds) + 720) % MIN_PER_DAY // 60:02d}:{(min(beds) + 720) % MIN_PER_DAY % 60:02d}" if beds else None,
                      "max": f"{(max(beds) + 720) % MIN_PER_DAY // 60:02d}:{(max(beds) + 720) % MIN_PER_DAY % 60:02d}" if beds else None},
        "wake_stats": {"mean": int(round(sum(wakes) / len(wakes))) if wakes else None,
                       "std": int(round(_std_min(wakes))) if _std_min(wakes) is not None else None},
        "deep_share": round(sum(deep_shares) / len(deep_shares), 3) if deep_shares else None,
        "prev_deep_share": round(sum(prev_deeps) / len(prev_deeps), 3) if prev_deeps else None,
        "efficiency": round(sum(effs) / len(effs), 1) if effs else None,
        "social_jetlag_min": int(round(sum(we) / len(we) - sum(wk) / len(wk))) if wk and we else None,
        "streak_best": longest_streak(store),
        "streak_now": streak(store, days[-1]),
        "debt_2w_min": _debt_2w(store, days[-1]),
        "debt_repaid_1w_min": _debt_repaid(store, ws),
        "prev_avg_dur_min": int(round(sum(prev_durs) / len(prev_durs))) if prev_durs else None,
    }


def _debt_2w(store: dict, upto: str) -> int:
    """近 14 天累计计债（滚动窗口口径）。"""
    start = (D.fromisoformat(upto) - timedelta(days=13)).isoformat()
    return sum(min(max(r.get("duration_min", 0) - r.get("target_min", 0), -DEBT_DAILY_CAP), DEBT_DAILY_CAP)
               for d, r in store["records"].items() if start <= d <= upto)


def _debt_repaid(store: dict, week_start_d: D) -> int:
    """本周还债量 = 上周末债 - 本周末债 的正向部分。"""
    prev_end = (week_start_d - timedelta(days=1)).isoformat()
    this_end = (week_start_d + timedelta(days=6)).isoformat()
    def debt_at(d: str) -> int:
        rows = [r for x, r in sorted(store["records"].items()) if x <= d]
        return rows[-1].get("debt_after_min", 0) if rows else 0
    return max(0, debt_at(prev_end) - debt_at(this_end))


# ------------------------------------------------------------ 入库 ----

SRC_RANK = {"oral": 1, "screenshot": 2, "watch_manual": 2, "watch": 3}  # 手表直连最高；watch_manual=手动录入手表显示，等同截图级


def ingest(rec: dict, override: bool = True) -> dict:
    """入库一条记录并全量重算。返回 {"store", "events", "replaced"}。
    rec 需含: date, duration_min；可选 bedtime/wake/deep_*/night_wake_*/source/image
    来源优先级：watch > screenshot > oral；低优先级写入被拒收，同级覆盖。
    """
    d = rec.get("date")
    if not d or not rec.get("duration_min"):
        raise ValueError("record 需要 date 与 duration_min")
    store = load_store()
    events = []
    old = store["records"].get(d)
    if old and not override:
        return {"store": store, "events": ["exists_not_overridden"], "replaced": False}
    old_rank = SRC_RANK.get(old.get("source"), 0) if old else 0
    new_rank = SRC_RANK.get(rec.get("source"), 0)
    if old and new_rank < old_rank:
        event = ("screenshot_wins_oral_rejected"
                 if old.get("source") == "screenshot" and rec.get("source") == "oral"
                 else "higher_source_wins_rejected")
        return {"store": store, "events": [event], "replaced": False}
    store["records"][d] = rec
    if rec.get("goal_app_min"):
        store["app_goal_min"] = rec["goal_app_min"]
    recompute(store)
    latest_date = max(store["records"])
    debt_2w = _debt_2w(store, latest_date)
    alerted = bool(store.get("debt_2w_alerted"))
    if debt_2w <= DEBT_ALERT and not alerted:
        events.append("debt_alert")
        store["debt_2w_alerted"] = True
    elif debt_2w >= 0 and alerted:
        events.append("debt_cleared")
        store["debt_2w_alerted"] = False
    streak_now = streak(store)
    if streak_now in STREAK_MILESTONES and (old is None or streak(store, d) - (1 if old else 0) < streak_now):
        events.append(f"streak_{streak_now}")
    ws = week_start(d)
    if D.fromisoformat(d).weekday() == 6 and all(
            (D.fromisoformat(ws) + timedelta(days=i)).isoformat() in store["records"] for i in range(7)):
        events.append("week_complete")
        if not store["weekly_sent"].get(ws):
            events.append("weekly_due")
    save_store(store)
    return {"store": store, "events": events, "replaced": old is not None}


# ------------------------------------------------------------ CLI ----

def _fmt_hm(m: int) -> str:
    m = int(round(m))
    return f"{m // 60}小时{m % 60:02d}分" if m // 60 else f"{m}分钟"


def _show(store: dict, d: str | None) -> dict:
    d = d or sorted(store["records"])[-1]
    rec = store["records"].get(d)
    if not rec:
        print(json.dumps({"error": f"no record {d}"}, ensure_ascii=False))
        return {}
    eff = sleep_efficiency(rec)
    deep, rem = rec.get("deep_min_est"), rec.get("rem_min_est")
    restorative = None
    restorative_pct = None
    if deep is not None and rem is not None:
        restorative = round(float(deep) + float(rem), 1)
        if rec.get("duration_min"):
            restorative = min(restorative, float(rec["duration_min"]))
            if rec["duration_min"] > 0:
                restorative_pct = round(100.0 * restorative / rec["duration_min"], 1)
    return {"date": d, "record": rec,
            "deviations": deviations(store, d),
            "quality": quality(store, d),
            "regularity": regularity(store, d),
            "streak": streak(store, d),
            "efficiency": eff,
            "restorative_min": restorative,
            "restorative_pct": restorative_pct,
            "debt_min": rec.get("debt_after_min")}


def morning_payload(store: dict, d: str) -> dict | None:
    """早报「😴 昨晚睡眠」取数（记录按醒来那天归属，d=今天）。"""
    if d not in store["records"]:
        return None
    m = _show(store, d)
    ws = week_start(d)
    week_recs = [(dd, r) for dd, r in sorted(store["records"].items())
                 if ws <= dd <= d]
    m["week_days"] = len(week_recs)
    m["week_meet"] = sum(1 for dd, r in week_recs
                         if r.get("duration_min")
                         and r.get("duration_min") >= r.get("target_min", 10**9))
    m["debt_2w_min"] = _debt_2w(store, d)
    return m


def _selftest() -> int:
    import copy
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    # 隔离：ingest 测试不得读写真实库（2026-10-09 曾把测试记录落盘污染真实数据）
    _mem = {"records": {}, "app_goal_min": 480, "weekly_sent": {}}
    globals()["load_store"] = lambda: _mem
    globals()["save_store"] = lambda s: None

    def mk(dur_h, bed="23:00", wake="07:00", **kw):
        r = {"bedtime": bed, "wake": wake, "duration_min": int(dur_h * 60), "source": "screenshot"}
        r.update(kw)
        return r

    s = {"records": {}, "app_goal_min": 480, "weekly_sent": {}}
    # 14 天 8h 达标 → 动态目标 = App 目标
    d0 = D(2026, 9, 1)
    for i in range(14):
        s["records"][(d0 + timedelta(days=i)).isoformat()] = mk(8.0)
    recompute(s)
    last = sorted(s["records"])[-1]
    check("14天全8h → 目标=App 480", s["records"][last]["target_min"] == 480)
    check("全达标 → 债=0", s["records"][last]["debt_after_min"] == 0)
    check("streak=14", streak(s, last) == 14)

    # 欠债两晚 → 目标上浮但 ≤+30min/天、≤540
    s["records"][(d0 + timedelta(days=14)).isoformat()] = mk(6.0)
    s["records"][(d0 + timedelta(days=15)).isoformat()] = mk(6.0)
    recompute(s)
    t15 = s["records"][(d0 + timedelta(days=15)).isoformat()]
    check("欠债后目标上浮 ≤+30", 480 < t15["target_min"] <= 510)
    check("债为负", t15["debt_after_min"] < 0)

    # 单日计债 ±240 封顶
    s["records"][(d0 + timedelta(days=16)).isoformat()] = mk(3.0)
    recompute(s)
    d16 = s["records"][(d0 + timedelta(days=16)).isoformat()]
    check("单日计债封顶240", t15["debt_after_min"] - d16["debt_after_min"] == DEBT_DAILY_CAP)

    # 缺记录日断链：9/1~9/14 全达标，挖掉 9/8 → streak 截至 9/14 = 6
    s_gap = {"records": {}, "app_goal_min": 480, "weekly_sent": {}}
    for i in range(14):
        s_gap["records"][(d0 + timedelta(days=i)).isoformat()] = mk(8.0)
    del s_gap["records"][(d0 + timedelta(days=7)).isoformat()]
    recompute(s_gap)
    check("缺日断链 → streak=6", streak(s_gap, (d0 + timedelta(days=13)).isoformat()) == 6)

    # 效率：总时长不含清醒
    eff_rec = mk(9.4167, bed="00:09", wake="09:50", duration_min=569)
    check("效率=569/581≈97.9", abs(sleep_efficiency(eff_rec) - 97.9) < 0.2)

    # 规律性：入睡时间跨午夜（23:xx 与 00:xx 混合）不得出负分（2026-10-11 事故回归）
    s_mid = {"records": {}, "app_goal_min": 480, "weekly_sent": {}}
    for dd, bb, ww in (("2026-10-09", "00:01", "07:14"), ("2026-10-10", "23:30", "09:08"), ("2026-10-11", "00:40", "08:21")):
        s_mid["records"][dd] = mk(7.7, bed=bb, wake=ww)
    rg = regularity(s_mid, "2026-10-11")
    check("跨午夜规律性 0-100", rg is not None and 0 <= rg <= 100)
    q11 = quality(s_mid, "2026-10-11")
    check("跨午夜质量分 ≥60", q11 is not None and q11 >= 60)

    # 周聚合（构造完整自然周：2026-10-05 是周一）
    s2 = {"records": {}, "app_goal_min": 480, "weekly_sent": {}}
    ws = D(2026, 10, 5)
    durs = [8.1, 6.8, 7.5, 8.2, 7.3, 9.7, 8.0]
    for i, du in enumerate(durs):
        s2["records"][(ws + timedelta(days=i)).isoformat()] = mk(du, **({"deep_min_est": 110} if i % 2 else {}))
    recompute(s2)
    wp = week_payload(s2, ws.isoformat())
    check("周完整 7 天", wp["complete"] and wp["days_recorded"] == 7)
    check("达标 4 天", wp["meet_days"] == 4)
    check("社交时差为正(周末多睡)", wp["social_jetlag_min"] is not None and wp["social_jetlag_min"] > 0)
    check("综合分 0-100", wp["composite"] is not None and 0 <= wp["composite"] <= 100)
    check("周标签含月日", "10月5日" in wp["label"])

    # ingest 事件与覆盖规则
    r1 = ingest({**mk(7.0), "date": "2026-10-07", "source": "oral"})
    check("口头入库", "exists_not_overridden" not in r1["events"] and r1["replaced"] is False)
    r2 = ingest({**mk(8.5), "date": "2026-10-07", "source": "screenshot"})
    check("截图覆盖口头", r2["store"]["records"]["2026-10-07"]["source"] == "screenshot")
    r3 = ingest({**mk(7.5), "date": "2026-10-07", "source": "oral"})
    check("口头被截图拒收", "screenshot_wins_oral_rejected" in r3["events"])
    r4a = ingest({**mk(8.0), "date": "2026-10-08", "source": "oral"})
    r4 = ingest({**mk(7.0), "date": "2026-10-08", "source": "oral"}, override=False)
    check("override=False 已存在则跳过", "exists_not_overridden" in r4["events"]
          and r4a["store"]["records"]["2026-10-08"]["duration_min"] == r4["store"]["records"]["2026-10-08"]["duration_min"])

    # 来源优先级：watch > screenshot > oral（2026-10-31 手表直连接入）
    r5 = ingest({**mk(8.2), "date": "2026-10-09", "source": "watch"})
    check("手表入库", r5["store"]["records"]["2026-10-09"]["source"] == "watch")
    r6 = ingest({**mk(7.8), "date": "2026-10-09", "source": "screenshot"})
    check("截图不能压过手表", "higher_source_wins_rejected" in r6["events"]
          and r6["store"]["records"]["2026-10-09"]["source"] == "watch")
    r7 = ingest({**mk(7.9), "date": "2026-10-09", "source": "watch"})
    check("手表同级覆盖", r7["replaced"] and r7["store"]["records"]["2026-10-09"]["duration_min"] == 474)

    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="睡眠数据层")
    ap.add_argument("--ingest", default=None, help="record.json 路径（'-'=stdin JSON）")
    ap.add_argument("--show", nargs="?", const="__latest__", default=None)
    ap.add_argument("--week", default=None, help="该日期所在自然周聚合")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.ingest:
        raw = sys.stdin.read() if args.ingest == "-" else Path(args.ingest).read_text(encoding="utf-8")
        out = ingest(json.loads(raw))
        print(json.dumps({"events": out["events"], "replaced": out["replaced"],
                          "latest_debt_min": out["store"].get("latest_debt_min")}, ensure_ascii=False))
        return 0
    store = load_store()
    if args.week:
        print(json.dumps(week_payload(store, args.week), ensure_ascii=False, indent=2))
        return 0
    if args.show:
        d = None if args.show == "__latest__" else args.show
        print(json.dumps(_show(store, d), ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
