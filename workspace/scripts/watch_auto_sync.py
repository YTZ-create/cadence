# -*- coding: utf-8 -*-
"""watch_auto_sync.py — 开机自动同步手表夜间数据（失败间隔重试）

用法：
  python -X utf8 scripts/watch_auto_sync.py --spawn      # 开机流程用：脱离父进程后台跑
  python -X utf8 scripts/watch_auto_sync.py              # 前台试跑（调试）
  python -X utf8 scripts/watch_auto_sync.py --post-sync   # 手动同步成功后处理早报/补推（不重新扫表）

行为：
  1. 四类数据（睡眠/睡眠心率/血氧/压力）全齐才算成功；缺一继续同步
  2. 今天四类已齐 → 直接退出（不重复同步）
  3. 调 watch_health.sync 同步 + 归档 + 写睡眠账本；失败或不齐则每 --interval 分钟重试，最多 --max 次
  4. 全齐且 08:15–09:30、早报未发 → 触发早报 cron（报告直接带睡眠板块）
  5. 全齐且早报已发 → 私聊补推「😴 昨晚睡眠」板块（每天仅一次，marker 防重）
  6. 全齐但不在触发时段 → 私聊短通知「可以开手机蓝牙」
  7. 09:30 后仍不齐 → 私聊推送一次缺项+失败原因+排查（每天一次）；09:30 前不推送
  8. 同步入库出现 debt_alert/debt_cleared（近两周累计债阈值）→ 私聊推送固定睡眠债文案（每种每天一次）
  9. --post-sync：手动同步成功后走同一套早报/补推处理，不重新扫表
  10. 全程写 cmf-watch/data/auto_status.json

约束：手机必须先断表（Nothing X 连着表时 PC 连不上）；单实例锁防并发。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch_health as wh  # noqa: E402

STATUS = wh.WATCH_DIR / "data" / "auto_status.json"
AUTO_LOCK = wh.WATCH_DIR / "data" / ".auto.lock"
SUPPLEMENT_MARK = wh.WATCH_DIR / "data" / ".supplement"
FAIL_MARK = wh.WATCH_DIR / "data" / ".sync_fail"
DEBT_MARK = wh.WATCH_DIR / "data" / ".debt"
DEBT_MESSAGES = {
    "debt_alert": "近两周累计比目标少睡 8小时，相当于一整晚没睡。周末多睡 1小时补回来，别一次补过头。",
    "debt_cleared": "之前欠的觉都补回来了，最近睡得不错。",
}
SENTINEL = wh.WS / "data" / "morning-report" / ".last-sent"
CHAT_ID = os.environ.get("CMF_FEISHU_CHAT_ID", "")
MORNING_JOB_ID = os.environ.get("CMF_MORNING_JOB_ID", "")


def log(msg: str):
    line = f"[{wh.now_str()}] {msg}"
    print(line, flush=True)
    try:
        with open(wh.WATCH_DIR / "data" / "auto_sync.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_status(**kw):
    try:
        STATUS.write_text(json.dumps(kw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def today_completeness() -> dict:
    date = datetime.now().strftime("%Y-%m-%d")
    try:
        return wh.completeness(date)
    except Exception as e:
        return {"date": date, "complete": False, "missing": ["检测失败"], "error": str(e)[:200]}


def auto_lock_alive() -> bool:
    if not AUTO_LOCK.exists():
        return False
    try:
        pid = int(AUTO_LOCK.read_text(encoding="utf-8").strip())
        age = time.time() - AUTO_LOCK.stat().st_mtime
    except Exception:
        return False
    if age > 3600 * 3:
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


# 通过环境变量或 PATH 查找 openclaw CLI，避免绑定本机安装路径
def _openclaw_cmd():
    cli = os.environ.get("OPENCLAW_CLI") or shutil.which("openclaw")
    return [cli] if cli else ["openclaw"]


def _feishu_api_send(text: str) -> bool:
    """直发飞书 API：不经过 openclaw CLI，避开会话文件锁与 cmd.exe 多行截断。"""
    import urllib.request
    try:
        cfg_path = Path(os.environ.get("OPENCLAW_CONFIG") or (Path(__file__).resolve().parents[2] / "openclaw.json"))
        if not cfg_path.is_file():
            log("feishu api: OPENCLAW_CONFIG missing; notification disabled")
            return False
        if not CHAT_ID:
            log("feishu api: CMF_FEISHU_CHAT_ID missing; notification disabled")
            return False
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        acc = cfg["channels"]["feishu"]["accounts"]["default"]
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": acc["appId"], "app_secret": acc["appSecret"]}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        token = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("tenant_access_token")
        if not token:
            log("feishu api: no tenant_access_token")
            return False
        req2 = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            data=json.dumps({"receive_id": CHAT_ID, "msg_type": "text",
                             "content": json.dumps({"text": text}, ensure_ascii=False)}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": "Bearer " + token}, method="POST")
        resp = json.loads(urllib.request.urlopen(req2, timeout=20).read())
        if resp.get("code") == 0:
            return True
        log(f"feishu api send failed: code={resp.get('code')} msg={resp.get('msg')}")
        return False
    except Exception as e:
        log(f"feishu api send exception: {e}")
        return False


def send_feishu(text: str) -> bool:
    if not CHAT_ID:
        log("未配置 CMF_FEISHU_CHAT_ID，跳过飞书通知")
        return False
    for attempt in (1, 2):
        if _feishu_api_send(text):
            return True
        if attempt == 1:
            time.sleep(5)
    return False


def notify_debt_events(results):
    """同步入库出现 debt_alert/debt_cleared 时私聊推送固定文案；每种每天一次。"""
    for item in results or []:
        date = item.get("date") or datetime.now().strftime("%Y-%m-%d")
        for event in item.get("events") or []:
            msg = DEBT_MESSAGES.get(event)
            if not msg:
                continue
            mark = Path(f"{DEBT_MARK}-{event}-{date}")
            if mark.exists():
                continue
            if send_feishu(msg):
                mark.write_text(wh.now_str(), encoding="utf-8")
                log(f"已推送 {event}（{date}）")
            else:
                log(f"{event} 推送失败（{date}）")


def morning_sent_today() -> bool:
    try:
        return SENTINEL.exists() and SENTINEL.read_text(encoding="utf-8").strip() == datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return False


def trigger_morning_report(allow_late: bool = False) -> bool:
    """08:15–09:30 且早报未发时触发；手动同步可延到 13:00，自动同步 09:30 后交给定时兜底。"""
    now = datetime.now()
    ready = now.replace(hour=8, minute=15, second=0, microsecond=0)
    end = now.replace(hour=13, minute=0, second=0, microsecond=0) if allow_late else \
        now.replace(hour=9, minute=30, second=0, microsecond=0)
    if not (ready <= now < end):
        return False
    if not MORNING_JOB_ID:
        log("未配置 CMF_MORNING_JOB_ID，跳过早报触发")
        return False
    try:
        r = subprocess.run(
            _openclaw_cmd() + ["cron", "run", MORNING_JOB_ID],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        ok = r.returncode == 0
        log(f"触发早报 cron rc={r.returncode} out={(r.stdout or '')[-200:]} err={(r.stderr or '')[-200:]}")
        return ok
    except Exception as e:
        log(f"触发早报 cron 异常：{e}")
        return False


def diagnose_failure() -> list[str]:
    try:
        bridge = "可连接" if wh.port_open() else "未启动/不可达"
    except Exception:
        bridge = "检测失败"
    try:
        adv = "检测到" if wh.watch_advertising(timeout=6.0) else "未检测到"
    except Exception:
        adv = "检测失败"
    try:
        last_sync = wh.load_latest().get("synced_at") or "无记录"
    except Exception:
        last_sync = "未知"
    return [f"桥接端口 8765：{bridge}",
            f"手表广播：{adv}",
            f"上次成功同步：{last_sync}"]


def notify_failure(last_err: str, attempts: int = 0, missing: list[str] | None = None):
    """09:30 后数据仍未齐全时推送一次缺项+失败原因+排查（每天一次）。"""
    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    cutoff = now.replace(hour=9, minute=30, second=0, microsecond=0)
    late = now.replace(hour=13, minute=0, second=0, microsecond=0)
    if now < cutoff:
        log("未到 09:30 超时，暂不推送失败通知")
        return
    mark = Path(f"{FAIL_MARK}-{date}")
    if mark.exists():
        log("今天已发过同步失败通知，跳过")
        return
    if missing is None:
        try:
            comp = wh.completeness(date)
            if comp.get("complete"):
                log("四类数据已齐，无需发送失败通知")
                return
            missing = comp.get("missing") or []
        except Exception:
            missing = []
    elif not missing:
        log("缺项为空，无需发送失败通知")
        return
    missing_text = "、".join(missing) if missing else "未知"
    lines = [f"手表数据不全（当前仍缺：{missing_text}）", ""]
    if attempts:
        lines.append(f"同步尝试：{attempts} 次")
    lines += [f"最后一次错误：{(last_err or '数据不齐')[:400]}", "", "排查："]
    lines += [f"• {x}" for x in diagnose_failure()]
    lines += ["", "请先关掉手机蓝牙、退出 Nothing X，手表靠近电脑后回我一句「同步手表」或「再同步一次」即可（自然语言都行）。",
              "如果昨晚没戴表、或手表本身没测到缺的那几类（如未连接手机），本条可忽略。"]
    if morning_sent_today():
        lines.append("今天早报已发（数据不全会在板块标注）；补齐后会补推完整睡眠板块。")
    elif now >= late:
        lines.append("今天早报窗口已过（13:00 后不再发早报）；补齐后补推昨晚睡眠。")
    else:
        lines.append("09:30 起早报按兜底规则发出并标注数据不全；补齐后会补推完整睡眠板块。")
    lines += ["", "日志：cmf-watch/data/auto_sync.log、cmf-watch/data/bridge.log"]
    if send_feishu("\n".join(lines)):
        mark.write_text(wh.now_str(), encoding="utf-8")
        log("已推送数据不全/同步失败通知到飞书")
    else:
        log("同步失败通知发送失败，未写标记，下次再发")


def _hm_text(minutes: float) -> str:
    m = int(round(minutes))
    h, mi = divmod(m, 60)
    return f"{h}小时{mi}分钟" if h else f"{mi}分钟"


def supplement_block(footer: str | None = None) -> str | None:
    """按早报板块模板渲染（数字逐字取 sleep_store，不圆整）。"""
    sys.path.insert(0, str(wh.WS / "scripts"))
    import sleep_store

    date = datetime.now().strftime("%Y-%m-%d")
    store = sleep_store.load_store()
    m = sleep_store.morning_payload(store, date)
    if not m:
        return None
    r = m["record"]
    d = m.get("deviations") or {}
    h, mi = divmod(int(r["duration_min"]), 60)
    dur = f"{h}小时{mi}分钟" if h else f"{mi}分钟"
    lines = [
        f"😴 昨晚睡眠（{int(date[5:7])}月{int(date[8:10])}日）", "",
    ]
    if m.get("quality") is not None:
        lines.append(f"📊 质量分：{m['quality']}")
    try:
        sys.path.insert(0, str(wh.WS / "scripts"))
        import recovery_score as rs
        rr = rs.compute(date)
        rs.save(rr)
        if rr.get("score") is not None:
            suffix = "" if rr["confidence"] == "高" else f"（数据可信度：{rr['confidence']}）"
            lines.append(f"🔋 恢复分：{rr['score']}{suffix}")
        try:
            import energy_score as es
            ee = es.predict(date)
            if ee.get("available"):
                prefix = "（校准中）" if ee.get("provisional") else ""
                lines.append(f"⚡ 精力分{prefix}：{ee['score']} · {ee['band']}")
                he = ee.get("high_energy") or {}
                wins = (he.get("windows") or [])[:2]
                if wins:
                    lines.append(f"　  高能时段：{wins[0]['start']}–{wins[0]['end']}")
                    if len(wins) > 1:
                        lines.append(f"　  　　　　　{wins[1]['start']}–{wins[1]['end']}")
                    total = int(he.get("total_min") or 0)
                    if total:
                        th, tm = divmod(total, 60)
                        ttext = f"{th}小时{tm}分钟" if th else f"{tm}分钟"
                        lines.append(f"　  高能时间：共 {ttext}")
        except Exception:
            pass
    except Exception:
        pass
    eff = m.get("efficiency")
    if eff is not None:
        lines.append(f"🛌 睡眠效率：{int(round(eff))}%")
    rest = m.get("restorative_min")
    if rest is not None:
        rh, rmi = divmod(int(round(rest)), 60)
        lines.append(f"🧠 恢复性睡眠：{rh}小时{rmi}分钟" if rh else f"🧠 恢复性睡眠：{rmi}分钟")
    lines.append(f"🌙 睡眠时长：{dur}")
    target_min = int(r.get("target_min") or 0)
    if target_min:
        th, tm = divmod(target_min, 60)
        target_text = f"{th}小时{tm}分钟" if tm else f"{th}小时"
        lines.append(f"🎯 目标睡眠时长：{target_text}")
    debt = m.get("debt_2w_min")
    if debt is not None:
        dval = int(round(abs(debt)))
        dh, dm = divmod(dval, 60)
        dtext = f"{dh}小时{dm}分钟" if dh and dm else (f"{dh}小时" if dh else f"{dm}分钟")
        lines.append("⚖️ 睡眠债（近两周）")
        if debt < 0:
            lines.append(f"　  比目标少睡 {dtext}")
        elif debt > 0:
            lines.append(f"　  比目标多睡 {dtext}")
        else:
            lines.append("　  已补齐")
    lines.append(f"🕒 {r['bedtime']} 入睡 → {r['wake']} 起床")
    if r.get("deep_segments") is not None:
        est = r.get("deep_min_est")
        lines.append(f"💤 深睡 {r['deep_segments']}段" + (f" 约{_hm_text(est)}" if est else ""))
    elif r.get("deep_min_est") is not None:
        lines.append(f"💤 深睡 {_hm_text(r['deep_min_est'])}")
    if r.get("night_wake_count") is not None:
        lines.append(f"👁 夜醒 {r['night_wake_count']} 次")
    elif r.get("night_wake_min_est"):
        lines.append(f"👁 夜醒 {round(r['night_wake_min_est'])} 分钟")
    if d:
        lines.append("🕐 比近7天均值：")
        for key, label in (("bed_min", "晚睡"), ("wake_min", "晚起"), ("dur_min", "多睡")):
            if d.get(key) is not None:
                v = int(d[key])
                word = label if (label != "多睡" or v >= 0) else "少睡"
                lines.append(f"　  {word} {_hm_text(abs(v))}")
    if m.get("streak") is not None:
        lines.append(f"🔥 连续达标 {m['streak']} 天")
    if m.get("week_days"):
        lines.append(f"📅 本周 {m['week_days']} 天中 {m.get('week_meet', 0)} 天达标")
    anomalies = []
    if r["duration_min"] > 570:
        anomalies.append("睡超 9.5 小时，偏多")
    if r["duration_min"] < 360:
        anomalies.append("不足 6 小时，偏少")
    bed_min = int(r["bedtime"][:2]) * 60 + int(r["bedtime"][3:])
    if 30 <= bed_min < 720:  # 00:30–11:59 入睡才算偏晚（23:5x 正常）
        anomalies.append("入睡晚于 00:30，偏晚")
    if anomalies:
        lines.append("⚠️ " + "；".join(anomalies))
    if footer is None:
        footer = "（此条为手表自动同步补推，早报已发过）"
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


def post_sync(date: str | None = None, manual: bool = False) -> dict:
    """同步成功后的统一处理：触发早报 / 补推睡眠板块 / 定时提示。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    sys.path.insert(0, str(wh.WS / "scripts"))
    import sleep_store
    m = sleep_store.morning_payload(sleep_store.load_store(), date)
    if not m:
        return {"ok": False, "action": "no_sleep_record", "date": date}
    try:
        comp = wh.completeness(date)
    except Exception as e:
        return {"ok": False, "action": "completeness_failed", "error": str(e)[:200], "date": date}
    if not comp.get("complete"):
        return {"ok": False, "action": "incomplete", "missing": comp.get("missing", []), "date": date}

    now = datetime.now()
    ready = now.replace(hour=8, minute=15, second=0, microsecond=0)
    cutoff = now.replace(hour=9, minute=30, second=0, microsecond=0)
    late = now.replace(hour=13, minute=0, second=0, microsecond=0)
    sent = morning_sent_today()
    mark = Path(f"{SUPPLEMENT_MARK}-{date}")

    if not sent and trigger_morning_report(allow_late=manual):
        log("已触发早报 cron；本次睡眠数据将进入早报")
        return {"ok": True, "action": "triggered_report", "date": date}

    if sent:
        if mark.exists():
            return {"ok": True, "action": "already_supplemented", "date": date}
        footer = "（此条为手动同步补推，早报已发过）" if manual else None
        block = supplement_block(footer)
        if block and send_feishu(block):
            mark.write_text(wh.now_str(), encoding="utf-8")
            return {"ok": True, "action": "supplemented", "date": date}
        return {"ok": False, "action": "supplement_failed", "date": date}

    if now >= late:
        if mark.exists():
            return {"ok": True, "action": "already_supplemented", "date": date}
        block = supplement_block("（早报窗口已过，补推睡眠数据）")
        if block and send_feishu(block):
            mark.write_text(wh.now_str(), encoding="utf-8")
            return {"ok": True, "action": "late_supplemented", "date": date}
        return {"ok": False, "action": "late_supplement_failed", "date": date}

    if manual:
        return {"ok": True, "action": "no_report_action", "date": date}

    h, mi = divmod(int(m["record"]["duration_min"]), 60)
    tail = f"（昨夜 {h}小时{mi}分钟）"
    if now >= cutoff:
        send_feishu(f"✅ 手表睡眠数据已同步{tail}；已过 9:30，早报按兜底规则处理。")
        return {"ok": True, "action": "notified_cutoff", "date": date}
    if now >= ready:
        send_feishu(f"✅ 手表睡眠数据已同步{tail}；早报触发失败，9:30 会兜底发送。")
        return {"ok": True, "action": "notified_failure", "date": date}
    send_feishu(f"✅ 手表睡眠数据已同步{tail}，可以开手机蓝牙了；8:15 后的早报会带上睡眠板块。")
    return {"ok": True, "action": "notified_early", "date": date}


def one_attempt(duration: float) -> tuple[bool, str]:
    """跑一次 sync，返回 (ok, 说明)。"""
    try:
        ok, why = wh.ensure_bridge()
        if not ok:
            return False, why
        if not wh.watch_advertising(timeout=8.0):
            if wh.try_enable_bluetooth():
                time.sleep(4)
            if not wh.watch_advertising(timeout=8.0):
                return False, "手表不在广播（手机可能连着表/距离远/蓝牙关闭）"
        res = wh.http_json("POST", "/sync", {"duration": duration}, timeout=duration + 120)
        date = datetime.now().strftime("%Y-%m-%d")
        wh.archive_day(res, date)
        notify_debt_events(wh.ingest_watch(res))
        # 教训：首轮可能只推到睡眠帧、心率批次没传完 → 缺睡眠窗口心率时自动补一次同步
        try:
            sys.path.insert(0, str(wh.WS / "scripts"))
            import sleep_store
            rec = sleep_store.load_store().get("records", {}).get(date)
            if rec and rec.get("bedtime") and rec.get("wake"):
                d = datetime.strptime(date, "%Y-%m-%d")
                bh, bm = map(int, rec["bedtime"].split(":"))
                wh_, wm = map(int, rec["wake"].split(":"))
                bed = d.replace(hour=bh, minute=bm)
                wake = d.replace(hour=wh_, minute=wm)
                if bed >= wake:
                    bed -= timedelta(days=1)
                st, en = int(bed.timestamp()), int(wake.timestamp())
                hr_in_window = [h for h in res.get("heart_rate", []) if st <= h.get("ts", 0) <= en]
                if not hr_in_window:
                    log("首轮缺睡眠窗口心率，补做第二次同步…")
                    res2 = wh.http_json("POST", "/sync", {"duration": 90}, timeout=210)
                    wh.archive_day(res2, date)
                    notify_debt_events(wh.ingest_watch(res2))
                    res = res2
                    hr_in_window = [h for h in res.get("heart_rate", []) if st <= h.get("ts", 0) <= en]
                    log(f"补同步后睡眠窗口心率 {len(hr_in_window)} 条")
        except Exception as e:
            log(f"补同步检查失败（不影响已同步数据）：{e}")
        try:
            comp = wh.completeness(date)
        except Exception as e:
            return False, f"完整性检查失败：{e}"
        if not comp.get("complete"):
            return False, f"数据不全（缺：{'、'.join(comp.get('missing') or [])}）"
        return True, f"同步成功 synced_at={res.get('synced_at')} sleep={len(res.get('sleep', []))}"
    except Exception as e:
        return False, wh.friendly_sync_error(str(e))


def run_loop(interval_min: float, max_tries: int, duration: float) -> int:
    if auto_lock_alive():
        log("已有自动同步循环在跑，退出")
        return 0
    AUTO_LOCK.write_text(str(os.getpid()), encoding="utf-8")
    started = wh.now_str()
    try:
        comp0 = today_completeness()
        if comp0.get("complete"):
            log("今天四类数据已齐，跳过同步")
            write_status(ok=True, skipped="already_complete", started_at=started,
                         finished_at=wh.now_str(), attempts=0, complete=True)
            return 0
        log(f"今天数据未齐（缺：{'、'.join(comp0.get('missing') or [])}），开始同步")
        attempts = 0
        last_err = ""
        while attempts < max_tries:
            attempts += 1
            log(f"第 {attempts}/{max_tries} 次尝试同步…")
            ok, why = one_attempt(duration)
            write_status(ok=ok, attempts=attempts, last_error=None if ok else why,
                         complete=ok, started_at=started,
                         finished_at=None if not ok else wh.now_str())
            if ok:
                log(why)
                date = datetime.now().strftime("%Y-%m-%d")
                res = post_sync(date, manual=False)
                if res.get("action") == "no_sleep_record":
                    log("同步完成但今天无有效睡眠记录（疑似解析异常），原始帧已存 last_frames.json")
                    write_status(ok=True, warning="sleep_parse_corrupted", attempts=attempts,
                                 started_at=started, finished_at=wh.now_str())
                return 0
            last_err = why
            log(f"失败：{why}")
            if attempts < max_tries:
                time.sleep(interval_min * 60)
        log(f"重试用尽（{max_tries} 次），最后一次错误：{last_err}")
        comp_end = today_completeness()
        write_status(ok=False, attempts=attempts, last_error=last_err,
                     complete=comp_end.get("complete"), missing=comp_end.get("missing", []),
                     started_at=started, finished_at=wh.now_str())
        notify_failure(last_err, attempts)
        return 1
    finally:
        try:
            AUTO_LOCK.unlink()
        except OSError:
            pass


def spawn() -> int:
    log_path = wh.WATCH_DIR / "data" / "auto_sync.log"
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_path, "ab") as logf:
        subprocess.Popen(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve())],
            cwd=str(wh.WS), stdout=logf, stderr=logf,
            creationflags=flags, close_fds=True,
        )
    print(json.dumps({"ok": True, "spawned": "watch_auto_sync.py"}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="开机自动同步手表夜间数据")
    ap.add_argument("--spawn", action="store_true", help="后台脱离运行（开机流程用）")
    ap.add_argument("--post-sync", action="store_true", help="手动同步成功后处理早报/补推（不重新扫表）")
    ap.add_argument("--date", default=None, help="配合 --post-sync 指定日期")
    ap.add_argument("--interval", type=float, default=10.0, help="重试间隔分钟（默认 10）")
    ap.add_argument("--max", type=int, default=6, help="最多尝试次数（默认 6）")
    ap.add_argument("--duration", type=float, default=180.0, help="单次采集窗口秒数")
    args = ap.parse_args(argv)
    if args.post_sync:
        res = post_sync(args.date, manual=True)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if args.spawn:
        return spawn()
    return run_loop(args.interval, args.max, args.duration)


if __name__ == "__main__":
    sys.exit(main())
