# -*- coding: utf-8 -*-
"""watch_health.py — CMF Watch 3 Pro 夜间健康数据接入（bridge 唯一入口）

子命令：
  status            检查 bridge 端口 / 手表广播 / 上次同步时间（--json）
  sync [--duration] 确保 bridge 在跑 → 确认手表在广播 → POST /sync → 归档当天数据
  report [--date]   读当天归档（无归档则从 latest_sync 现算），输出 Markdown / --json

数据语义（见 skills/watch-health/SKILL.md）：
  - activity 是当日累计快照：只留最新一条，不能求和
  - sleep 同 start 多条是累积快照：只留 end 最大的一条；header_totals 为权威汇总
  - 心率/血氧/压力按 ts 去重；血氧/压力是稀疏采样，不是连续曲线
  - 睡眠按醒来那天归属日期（与睡眠管理功能一致）

安全：tools/.watch_state.json 含 secret/authkey，禁止打印/外传；bridge 只绑 127.0.0.1。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path(__file__).resolve().parents[2])
WATCH_DIR = ROOT / "cmf-watch"
WS = ROOT / "workspace"
ARCHIVE_DIR = WS / "data" / "health" / "watch"
LATEST = WATCH_DIR / "data" / "latest_sync.json"
BRIDGE_LOG = WATCH_DIR / "data" / "bridge.log"
LOCK = WATCH_DIR / "data" / ".sync.lock"
WATCH_ADDRESS = os.environ.get("CMF_WATCH_ADDRESS") or None
WATCH_NAME = os.environ.get("CMF_WATCH_NAME", "CMF Watch 3 Pro")
HOST, PORT = "127.0.0.1", 8765
STAGES = {1: "deep", 2: "light", 3: "rem", 4: "awake"}
STAGE_CN = {"deep": "深睡", "light": "浅睡", "rem": "REM", "awake": "清醒"}


# ---------------------------------------------------------------- 基础 ----

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def local_date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def hm(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def port_open(timeout=0.6) -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_bridge(wait=15.0) -> tuple[bool, str]:
    """bridge 没在跑就后台拉起；返回 (ok, 说明)。"""
    if port_open():
        return True, "bridge 已在运行"
    bridge = WATCH_DIR / "bridge.py"
    if not bridge.exists():
        return False, f"找不到 {bridge}"
    try:
        log = open(BRIDGE_LOG, "ab")
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable, str(bridge)],
            cwd=str(WATCH_DIR), stdout=log, stderr=log,
            creationflags=flags, close_fds=True,
        )
    except Exception as e:
        return False, f"启动 bridge 失败：{e}"
    deadline = time.time() + wait
    while time.time() < deadline:
        if port_open():
            return True, "bridge 已启动"
        time.sleep(0.5)
    return False, f"bridge 启动超时（{wait:.0f}s），看 {BRIDGE_LOG}"


def http_json(method: str, path: str, payload: dict | None = None, timeout=60):
    import urllib.error
    import urllib.request

    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"bridge HTTP {e.code}: {body[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上 bridge（{e.reason}），请先运行 sync/status") from e


def watch_advertising(timeout=10.0) -> bool:
    """手表是否在广播（手机连着时通常不广播）。"""
    import asyncio

    from bleak import BleakScanner

    async def go():
        found = await BleakScanner.find_device_by_filter(
            lambda d, ad: (d.address or "").upper() == (WATCH_ADDRESS or "").upper()
            or WATCH_NAME in ((ad.local_name or d.name or "")),
            timeout=timeout,
        )
        return found is not None

    try:
        return asyncio.run(go())
    except Exception:
        return False


def friendly_sync_error(msg: str) -> str:
    low = msg.lower()
    if "watch not found" in low or "no device" in low:
        return ("手表不在广播：可能手机 Nothing X 仍连着表、手表离电脑太远或蓝牙关闭。"
                "请先关闭手机蓝牙（或断开 Nothing X），靠近电脑后重试；不要反复强行连接。")
    if "timed out" in low or "timeout" in low:
        return "连接超时：手表可能被手机占用或未响应。关闭手机蓝牙后重试。"
    if "address already in use" in low or "10048" in low:
        return "端口 8765 被占用：已有另一个 bridge/同步进程，避免并发同步。"
    return msg


def try_enable_bluetooth() -> bool:
    """蓝牙关着时用仓库自带 enable-bt.ps1 打开（WinRT Radio）。"""
    ps = WATCH_DIR / "enable-bt.ps1"
    if os.name != "nt" or not ps.exists():
        return False
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                       capture_output=True, timeout=40, creationflags=flags)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 归档 ----

def load_latest() -> dict:
    if not LATEST.exists():
        return {}
    try:
        return json.loads(LATEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _day_heart_rate(activity: dict, date: str) -> list[dict]:
    return [x for x in activity.get("heart_rate", []) if local_date(x["ts"]) == date]


def _day_sparse(activity: dict, name: str, date: str) -> list[dict]:
    return [x for x in activity.get(name, []) if local_date(x["ts"]) == date]


def _day_sleep(activity: dict, date: str) -> list[dict]:
    """按醒来日归属；同 start 留 end 最大。"""
    best = {}
    for s in activity.get("sleep", []):
        if local_date(s["end"]) != date:
            continue
        span = s["end"] - s["start"]
        if not (300 <= span <= 129600):  # 5 分钟~36 小时，否则视为坏帧
            continue
        cur = best.get(s["start"])
        if cur is None or s["end"] > cur["end"]:
            best[s["start"]] = s
    return sorted(best.values(), key=lambda x: x["start"])


def _day_activity_snapshot(activity: dict, date: str) -> dict | None:
    rows = [x for x in activity.get("activity", []) if local_date(x["ts"]) == date]
    return max(rows, key=lambda x: x["ts"]) if rows else None


def archive_day(activity: dict, date: str) -> dict:
    """把当天数据并入 data/health/watch/YYYY-MM-DD.json（幂等、去重）。"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{date}.json"
    cur = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cur = {}

    sleep = {s["start"]: s for s in cur.get("sleep", [])}
    day_sessions = _day_sleep(activity, date)
    for s in day_sessions:
        old = sleep.get(s["start"])
        if old is None or s["end"] > old["end"]:
            sleep[s["start"]] = s
    windows = [(s["start"], s["end"]) for s in sleep.values()]

    def in_day(ts: int) -> bool:
        # 自然日样本 + 当晚睡眠窗口内的跨午夜样本（23:5x 的入睡初期数据不能丢）
        return local_date(ts) == date or any(a <= ts <= b for a, b in windows)

    def merge_ts(name: str):
        rows = {x["ts"]: x for x in cur.get(name, [])}
        for x in activity.get(name, []):
            if in_day(x["ts"]):
                rows[x["ts"]] = x
        return [rows[k] for k in sorted(rows)]

    snap = _day_activity_snapshot(activity, date)
    old_snap = cur.get("activity_today")
    if snap and (not old_snap or snap["ts"] >= old_snap.get("ts", 0)):
        snap_row = snap
    else:
        snap_row = old_snap

    out = {
        "date": date,
        "updated_at": now_str(),
        "sleep": sorted(sleep.values(), key=lambda x: x["start"]),
        "heart_rate": merge_ts("heart_rate"),
        "resting_heart_rate": merge_ts("resting_heart_rate"),
        "spo2": merge_ts("spo2"),
        "stress": merge_ts("stress"),
        "activity_today": snap_row,
    }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_day(date: str) -> dict:
    path = ARCHIVE_DIR / f"{date}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    latest = load_latest()
    if latest:
        return archive_day(latest, date)
    return {"date": date, "sleep": [], "heart_rate": [], "spo2": [], "stress": [],
            "resting_heart_rate": [], "activity_today": None}


# ---------------------------------------------------------------- 报表 ----

def header_totals(s: dict) -> dict:
    h = s.get("header_totals", {})
    if "field_8" in h:  # 兼容旧解析器字段
        vals = [h.get("field_8", 0), h.get("field_10", 0), h.get("field_12", 0), h.get("field_14", 0)]
    else:
        vals = [h.get("deep_s", 0), h.get("light_s", 0), h.get("rem_s", 0), h.get("awake_s", 0)]
    return dict(zip(("deep", "light", "rem", "awake"), (int(v / 60) for v in vals)))


def _stats(vals: list[float]) -> dict | None:
    if not vals:
        return None
    return {"n": len(vals), "min": min(vals), "avg": round(sum(vals) / len(vals), 1), "max": max(vals)}


MIN_SLEEP_FOR_RECORD = 90  # 分钟；低于此为小睡/碎片，不入睡眠账本


def _watch_record(day: dict) -> dict | None:
    """把当天最长一段手表睡眠转成 sleep_store 记录（时长=深+浅+REM，不含清醒）。"""
    sessions = day.get("sleep", [])
    if not sessions:
        return None
    main = max(sessions, key=lambda x: x["end"] - x["start"])
    tot = header_totals(main)
    dur = tot["deep"] + tot["light"] + tot["rem"]
    if dur < MIN_SLEEP_FOR_RECORD:
        return None
    seg = {"deep": 0, "rem": 0, "awake": 0}
    for x in main.get("stages", []):
        name = STAGES.get(x["stage"])
        if name in seg:
            seg[name] += 1
    return {
        "date": day["date"],
        "bedtime": hm(main["start"]),
        "wake": hm(main["end"]),
        "duration_min": dur,
        "source": "watch",
        "recorded_at": now_str(),
        "deep_min_est": round(tot["deep"], 1),
        "rem_min_est": round(tot["rem"], 1),
        "deep_segments": seg["deep"],
        "rem_segments": seg["rem"],
        "night_wake_count": seg["awake"],
        "night_wake_min_est": tot["awake"],
    }


def ingest_watch(activity: dict) -> list[dict]:
    """把同步拿到的所有夜晚（含补到的历史天）归档并写入睡眠账本。"""
    sys.path.insert(0, str(WS / "scripts"))
    import sleep_store  # 延迟导入，避免循环

    dates = sorted({local_date(s["end"]) for s in activity.get("sleep", [])})
    out = []
    for date in dates:
        day = archive_day(activity, date)
        rec = _watch_record(day)
        if not rec:
            out.append({"date": date, "ingested": False, "reason": "无≥90分钟睡眠"})
            continue
        res = sleep_store.ingest(rec)
        out.append({"date": date, "ingested": "higher_source_wins_rejected" not in res["events"],
                    "duration_min": rec["duration_min"], "events": res["events"]})
    return out


def _cadence(ts_list: list[int]) -> dict | None:
    """采样间隔统计（用于判断心率监测频率是否够做事件级分析）。"""
    ts_list = sorted(ts_list)
    if len(ts_list) < 3:
        return None
    import statistics

    gaps = [b - a for a, b in zip(ts_list, ts_list[1:]) if 0 < b - a < 6 * 3600]
    if not gaps:
        return None
    return {
        "n": len(ts_list),
        "median_s": int(statistics.median(gaps)),
        "mean_s": int(statistics.mean(gaps)),
        "p90_s": int(sorted(gaps)[int(len(gaps) * 0.9)]),
        "max_gap_s": int(max(gaps)),
    }


def build_report(day: dict) -> dict:
    date = day["date"]
    sleep = sorted(day.get("sleep", []), key=lambda x: x["start"])
    hr = day.get("heart_rate", [])
    main = max(sleep, key=lambda x: x["end"] - x["start"]) if sleep else None

    rep = {"date": date, "synced": bool(sleep or hr), "sessions": [], "main": None,
           "sleep_hr": None, "stage_hr": {}, "hourly_hr": [], "spo2": None, "stress": None,
           "resting_hr": None, "activity": day.get("activity_today")}

    for s in sleep:
        st, en = s["start"], s["end"]
        vals = [h["bpm"] for h in hr if st <= h["ts"] <= en]
        tot = header_totals(s)
        # 分段记录（可能不覆盖整段，只用于时间线和分段心率）
        rec = {}
        stage_vals: dict[str, list[int]] = {}
        for x in s.get("stages", []):
            name = STAGES.get(x["stage"], str(x["stage"]))
            rec[name] = rec.get(name, 0) + int(x.get("duration_s", 0) / 60)
            w1 = x["ts"] + x.get("duration_s", 0)
            stage_vals.setdefault(name, []).extend(h["bpm"] for h in hr if x["ts"] <= h["ts"] < w1)
        rep["sessions"].append({
            "start": s["start_time"], "end": s["end_time"],
            "duration_min": int(round(s.get("duration_min") or (en - st) / 60)),
            "totals_min": tot, "record_min": rec, "hr": _stats(vals),
        })
        if main is s:
            rep["main"] = rep["sessions"][-1]
            rep["sleep_hr"] = _stats(vals)
            rep["hr_cadence"] = _cadence([h["ts"] for h in hr if st <= h["ts"] <= en])
            rep["stage_hr"] = {k: _stats(v) for k, v in stage_vals.items() if v}
            buckets: dict[str, list[int]] = {}
            for h in hr:
                if st <= h["ts"] <= en:
                    hh = time.strftime("%H:00", time.localtime(h["ts"]))
                    buckets.setdefault(hh, []).append(h["bpm"])
            rep["hourly_hr"] = [{"hour": k, **_stats(v)} for k, v in sorted(buckets.items())]

    if main:
        st, en = main["start"], main["end"]
        rep["spo2"] = _stats([x["percent"] for x in day.get("spo2", []) if st <= x["ts"] <= en])
        rep["stress"] = _stats([x["value"] for x in day.get("stress", []) if st <= x["ts"] <= en])
    rep["resting_hr"] = _stats([x["bpm"] for x in day.get("resting_heart_rate", [])])
    return rep


def completeness(date: str) -> dict:
    """夜间四类数据是否齐全：睡眠/睡眠心率/血氧/压力，缺一即不全。"""
    try:
        import sleep_store
        rec = sleep_store.load_store().get("records", {}).get(date)
    except Exception:
        rec = None
    path = ARCHIVE_DIR / f"{date}.json"
    if not path.exists():
        return {"date": date, "complete": False,
                "missing": ["睡眠", "睡眠心率", "血氧", "压力"], "sleep": False,
                "sleep_hr": False, "spo2": False, "stress": False}
    try:
        day = json.loads(path.read_text(encoding="utf-8"))
        rep = build_report(day)
    except Exception as e:
        return {"date": date, "complete": False, "missing": ["睡眠", "睡眠心率", "血氧", "压力"],
                "error": str(e)[:200]}
    missing = []
    if not rec or rec.get("source") != "watch" or not rep.get("main"):
        missing.append("睡眠")
    if not rep.get("sleep_hr"):
        missing.append("睡眠心率")
    if not rep.get("spo2"):
        missing.append("血氧")
    if not rep.get("stress"):
        missing.append("压力")
    return {"date": date, "complete": not missing, "missing": missing,
            "sleep": bool(rep.get("main")), "sleep_hr": bool(rep.get("sleep_hr")),
            "spo2": bool(rep.get("spo2")), "stress": bool(rep.get("stress"))}


def render_markdown(rep: dict) -> str:
    lines = [f"# 手表夜间健康 · {rep['date']}", ""]
    if not rep["sessions"]:
        lines.append("无夜间数据：手表未记录睡眠，或手机先同步把数据拿走了（游标全局，PC 无法补回）。")
        hr = rep.get("sleep_hr")
        if not hr and rep.get("resting_hr"):
            lines.append(f"\n静息心率：{rep['resting_hr']['avg']} 次/分"
                         f"（{rep['resting_hr']['min']}–{rep['resting_hr']['max']}，{rep['resting_hr']['n']} 条）")
        return "\n".join(lines)

    m = rep["main"]
    t = m["totals_min"]
    lines += [
        f"## 主睡眠 {m['start'][11:16]}–{m['end'][11:16]}（{m['duration_min']} 分钟）", "",
        f"- 深睡 {t['deep']} 分钟 / 浅睡 {t['light']} 分钟 / REM {t['rem']} 分钟 / 清醒 {t['awake']} 分钟",
    ]
    if m["record_min"]:
        rec = " / ".join(f"{STAGE_CN.get(k, k)} {v} 分钟" for k, v in m["record_min"].items())
        lines.append(f"- 分段记录：{rec}（分段可能不覆盖整段，总和少于时长属正常）")
    hr = rep.get("sleep_hr")
    if hr:
        lines.append(f"- 睡眠心率：{hr['n']} 条，均值 {hr['avg']}，最低 {hr['min']}，最高 {hr['max']} BPM")
    cad = rep.get("hr_cadence")
    if cad:
        lines.append(f"- 心率采样：中位间隔 {cad['median_s']} 秒，平均 {cad['mean_s']} 秒，"
                     f"90% 间隔 ≤{cad['p90_s']} 秒，最长空档 {cad['max_gap_s'] // 60} 分钟")
    if rep.get("stage_hr"):
        parts = [f"{STAGE_CN.get(k, k)} {v['avg']}（{v['n']} 条）" for k, v in rep["stage_hr"].items() if v]
        lines.append(f"- 分阶段心率均值：{' / '.join(parts)}")
    if rep.get("spo2"):
        s = rep["spo2"]
        lines.append(f"- 睡眠血氧：{s['n']} 条，均值 {s['avg']}%，范围 {s['min']}–{s['max']}%")
    if rep.get("stress"):
        s = rep["stress"]
        lines.append(f"- 睡眠压力：{s['n']} 条，均值 {s['avg']}，范围 {s['min']}–{s['max']}")
    others = [x for x in rep["sessions"] if x is not rep["main"]]
    if others:
        lines.append("")
        lines.append("## 其他睡眠片段")
        for x in others:
            lines.append(f"- {x['start'][11:16]}–{x['end'][11:16]}（{x['duration_min']} 分钟）")
    if rep.get("hourly_hr"):
        lines.append("")
        lines.append("## 逐小时心率")
        lines.append(" / ".join(f"{h['hour']} 均值 {h['avg']}" for h in rep["hourly_hr"]))
    if rep.get("resting_hr"):
        r = rep["resting_hr"]
        lines.append("")
        lines.append(f"全天静息心率：{r['avg']} 次/分（{r['min']}–{r['max']}，{r['n']} 条）")
    return "\n".join(lines)


def write_markdown(rep: dict) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{rep['date']}.md"
    path.write_text(render_markdown(rep) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- 命令 ----

def cmd_status(args) -> int:
    running = port_open()
    adv = watch_advertising(timeout=args.scan_timeout) if not args.no_scan else None
    latest = load_latest()
    battery = None
    if running and not args.no_scan:
        try:
            battery = http_json("GET", "/health", timeout=45).get("battery")
        except Exception:
            battery = None
    out = {
        "bridge_running": running,
        "watch_advertising": adv,
        "battery": battery,
        "last_sync": latest.get("synced_at"),
        "last_counts": latest.get("counts"),
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"bridge：{'运行中' if running else '未运行（sync 会自动拉起）'}")
        print(f"手表广播：{'是' if adv else '否' if adv is False else '未检测'}"
              f"{'（空闲，可同步）' if adv else '（手机可能连着表/距离远/蓝牙关闭）' if adv is False else ''}")
        if battery is not None:
            print(f"电量：{battery}%")
        print(f"上次同步：{out['last_sync'] or '从未同步'}")
    return 0


def _lock_alive() -> bool:
    if not LOCK.exists():
        return False
    try:
        pid = int(LOCK.read_text(encoding="utf-8").strip())
        age = time.time() - LOCK.stat().st_mtime
    except Exception:
        return False
    if age > 600:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_sync(args) -> int:
    if _lock_alive():
        print(json.dumps({"ok": False, "error": "已有同步进程在跑，请等待完成（避免 BLE 冲突）"},
                         ensure_ascii=False))
        return 2
    ok, why = ensure_bridge()
    if not ok:
        print(json.dumps({"ok": False, "error": why}, ensure_ascii=False))
        return 2
    if not watch_advertising(timeout=args.scan_timeout):
        # 蓝牙关着时自动开一次，再扫一遍（爬坡记录：BT 关闭是最常见原因之一）
        if try_enable_bluetooth():
            time.sleep(4)
        if not watch_advertising(timeout=args.scan_timeout):
            msg = ("手表不在广播：请关闭手机蓝牙（或断开 Nothing X）、把手表靠近电脑后重试；"
                   "不要反复强行连接。")
            print(json.dumps({"ok": False, "error": msg, "stage": "scan"}, ensure_ascii=False))
            return 3
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    try:
        t0 = time.time()
        try:
            res = http_json("POST", "/sync", {"duration": args.duration},
                            timeout=args.duration + 120)
        except RuntimeError as e:
            print(json.dumps({"ok": False, "error": friendly_sync_error(str(e)), "stage": "sync"},
                             ensure_ascii=False))
            return 4
        took = round(time.time() - t0, 1)
        date = args.date or datetime.now().strftime("%Y-%m-%d")
        day = archive_day(res, date)
        rep = build_report(day)
        md_path = write_markdown(rep)
        ingested = ingest_watch(res) if not args.no_ingest else []
        debt_events = [e for item in ingested for e in (item.get("events") or [])
                       if e in ("debt_alert", "debt_cleared")]
        try:
            import watch_auto_sync as _was
            _was.notify_debt_events(ingested)
        except Exception:
            pass
        from_summary = {
            "ok": True,
            "synced_at": res.get("synced_at"),
            "took_s": took,
            "counts": res.get("counts"),
            "date": date,
            "sleep_sessions": len(rep["sessions"]),
            "archive": str(md_path),
            "sleep_ingested": ingested,
            "debt_events": debt_events,
        }
        print(json.dumps(from_summary, ensure_ascii=False))
        return 0
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


def cmd_report(args) -> int:
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    day = load_day(date)
    rep = build_report(day)
    md = render_markdown(rep)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(md)
    if args.save:
        write_markdown(rep)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CMF Watch 夜间健康数据接入")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="bridge/广播/上次同步状态")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-scan", action="store_true", help="不扫手表广播（快）")
    p.add_argument("--scan-timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", help="同步手表并归档当天数据")
    p.add_argument("--duration", type=float, default=180.0, help="采集窗口秒数（20–300）")
    p.add_argument("--date", default=None, help="归档日期（默认今天=醒来日）")
    p.add_argument("--scan-timeout", type=float, default=10.0)
    p.add_argument("--no-ingest", action="store_true", help="只归档，不写睡眠账本")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("report", help="当天夜间健康报告")
    p.add_argument("--date", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", action="store_true", help="同时写 data/health/watch/<date>.md")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
