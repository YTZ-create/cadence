# -*- coding: utf-8 -*-
"""reaction_test.py — 桌面反应力小测（不定时，自己做）

用法：
  双击桌面「反应力测试」图标（或 pythonw scripts/reaction_test.py）
  看到白色圆点立刻按空格（鼠标点击不计）；共 8 次，约 60 秒。
  做完自动保存：data/health/reaction/YYYY-MM-DD.json + log.jsonl

命令：
  python -X utf8 scripts/reaction_test.py            # 打开测试窗口
  python -X utf8 scripts/reaction_test.py report --date 2026-10-31 [--json]
  python -X utf8 scripts/reaction_test.py selftest   # 自测（临时目录）
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path(__file__).resolve().parents[2])
WS = ROOT / "workspace"
sys.path.insert(0, str(WS / "scripts"))
OUT_DIR = Path(os.environ.get("OPENCLAW_REACTION_DIR") or (WS / "data" / "health" / "reaction"))
TRIALS = 8
RESPONSE_LIMIT_MS = 1500
LAPSE_MS = 500


def _block(hour: int) -> str:
    if hour < 6:
        return "凌晨"
    if hour < 12:
        return "上午"
    if hour < 18:
        return "下午"
    return "晚上"


def _time_since_wake(date: str, at: datetime | None = None) -> int | None:
    rec = None
    try:
        import sleep_store
        rec = sleep_store.load_store().get("records", {}).get(date)
    except Exception:
        try:
            store = json.loads((WS / "data" / "sleep" / "sleep.json").read_text(encoding="utf-8"))
            rec = store.get("records", {}).get(date)
        except Exception:
            rec = None
    if not rec or not rec.get("wake"):
        return None
    try:
        h, m = map(int, rec["wake"].split(":"))
        now = at or datetime.now()
        wake = now.replace(hour=h, minute=m, second=0, microsecond=0)
        delta = int((now - wake).total_seconds() // 60)
        return delta if delta >= 0 else None
    except Exception:
        return None


def summarize(rts: list[int], false_starts: int, trials: int) -> dict:
    valid = [x for x in rts if x is not None]
    if not valid:
        return {"error": "no valid trials"}
    return {
        "trials": trials,
        "valid": len(valid),
        "median_ms": int(statistics.median(valid)),
        "mean_ms": int(sum(valid) / len(valid)),
        "min_ms": min(valid),
        "max_ms": max(valid),
        "lapses": sum(1 for x in valid if x > LAPSE_MS),
        "false_starts": false_starts,
    }


def save_result(result: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day_path = OUT_DIR / f"{result['date']}.json"
    day = {"date": result["date"], "tests": []}
    if day_path.exists():
        try:
            day = json.loads(day_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    day.setdefault("tests", []).append(result)
    tmp = day_path.with_name(day_path.name + ".tmp")
    tmp.write_text(json.dumps(day, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, day_path)
    with open(OUT_DIR / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return day_path


def record(rts: list[int], false_starts: int, inputs: list[str] | None = None,
           started_at: datetime | None = None) -> dict:
    ended = datetime.now()
    started = started_at or ended
    date = started.strftime("%Y-%m-%d")
    s = summarize(rts, false_starts, TRIALS)
    result = {
        "date": date,
        "ts": int(started.timestamp()),
        "time": started.strftime("%H:%M"),
        "block": _block(started.hour),
        "time_since_wake_min": _time_since_wake(date, at=started),
        "source": "desktop",
        "version": 4,
        "space_trials": (inputs or []).count("space"),
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "ended_at": ended.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": max(0, int((ended - started).total_seconds())),
        **s,
    }
    save_result(result)
    return result


def report(date: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    path = OUT_DIR / f"{date}.json"
    if not path.exists():
        return {"date": date, "available": False, "reason": "当天没有反应力测试"}
    try:
        day = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"date": date, "available": False, "reason": str(e)}
    tests = day.get("tests") or []
    if not tests:
        return {"date": date, "available": False, "reason": "当天没有反应力测试"}
    valid = [t for t in tests
             if isinstance(t.get("median_ms"), (int, float))
             and isinstance(t.get("min_ms"), (int, float))]
    if not valid:
        return {"date": date, "available": False, "reason": "测试记录不完整"}
    med = [t["median_ms"] for t in valid]
    wake_vals = [t.get("time_since_wake_min") for t in valid
                 if isinstance(t.get("time_since_wake_min"), (int, float))
                 and t["time_since_wake_min"] >= 0]
    return {
        "date": date,
        "available": True,
        "tests": len(valid),
        "skipped": len(tests) - len(valid),
        "median_ms": int(statistics.median(med)),
        "best_ms": min(t["min_ms"] for t in valid),
        "lapses_total": sum(int(t.get("lapses") or 0) for t in valid),
        "false_starts_total": sum(int(t.get("false_starts") or 0) for t in valid),
        "time_since_wake_min": int(statistics.median(wake_vals)) if wake_vals else None,
        "last": tests[-1],
    }


# ---------------------------------------------------------------- GUI ----

class App:
    BG = "#101114"
    FG = "#e8e8ea"
    DIM = "#8a8a90"
    DOT = "#f5f5f7"

    def __init__(self):
        import tkinter as tk

        self.tk = tk
        self.root = tk.Tk()
        self.root.title("反应力测试")
        self.root.geometry("560x420")
        self.root.configure(bg=self.BG)
        self.root.resizable(False, False)
        self.root.bind("<space>", lambda e: self.on_press(True))  # 仅空格计入，鼠标点击不响应
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.canvas = tk.Canvas(self.root, bg=self.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.texts = []
        self.dot = None
        self.dot_t0 = None
        self.rts: list[int] = []
        self.inputs: list[str] = []
        self.false_starts = 0
        self.started_at = None
        self.trial = 0
        self.state = "idle"
        self.after_ids = []
        self.show_intro()

    def clear(self):
        for i in self.after_ids:
            try:
                self.root.after_cancel(i)
            except Exception:
                pass
        self.after_ids.clear()
        self.canvas.delete("all")
        self.texts.clear()
        self.dot = None
        self.dot_t0 = None

    def text(self, x, y, s, size=18, color=None, anchor="center"):
        t = self.canvas.create_text(x, y, text=s, fill=color or self.FG,
                                    font=("Microsoft YaHei UI", size), anchor=anchor,
                                    width=500, justify="center")
        self.texts.append(t)
        return t

    def show_intro(self):
        self.state = "idle"
        self.clear()
        self.text(280, 90, "反应力测试", 24)
        self.text(280, 170,
                  "看到白色圆点，立刻按空格\n（鼠标点击不计成绩，不用移到圆点上）\n\n共 8 次，约 60 秒\n"
                  "漏拍/抢拍会记录，但不用紧张", 15, self.DIM)
        self.text(280, 345, "按空格开始 · Esc 退出", 14, self.DIM)

    def start(self):
        self.clear()
        self.rts = []
        self.inputs = []
        self.false_starts = 0
        self.started_at = datetime.now()
        self.trial = 0
        self.state = "ready"
        self.text(280, 210, "准备好…", 20)
        self.after(900, self.next_trial)

    def after(self, ms, fn):
        self.after_ids.append(self.root.after(ms, fn))

    def next_trial(self):
        if self.trial >= TRIALS:
            self.finish()
            return
        self.trial += 1
        self.clear()
        self.text(280, 60, f"第 {self.trial} / {TRIALS} 次", 14, self.DIM)
        self.state = "wait"
        self.after(random.randint(1000, 2500), self.show_dot)

    def show_dot(self):
        if self.state != "wait":
            return
        self.state = "go"
        x = random.randint(120, 440)
        y = random.randint(140, 300)
        r = 34
        self.dot = self.canvas.create_oval(x - r, y - r, x + r, y + r,
                                           fill=self.DOT, outline="")
        self.dot_t0 = time.perf_counter()
        self.after(RESPONSE_LIMIT_MS, self.on_timeout)

    def on_timeout(self):
        if self.state != "go":
            return
        self.state = "wait"
        self.rts.append(None)
        self.canvas.delete(self.dot) if self.dot else None
        self.dot = None
        self.text(280, 350, "这次没接住", 14, self.DIM)
        self.after(500, self.next_trial)

    def on_press(self, space=False):
        if self.state == "idle":
            self.start()
        elif self.state == "wait":
            if self.trial > 0:
                self.false_starts += 1
                self.text(280, 350, "太早了，等圆点出现再按", 14, "#e0a030")
        elif self.state == "go":
            rt = int((time.perf_counter() - self.dot_t0) * 1000)
            self.state = "wait"
            self.rts.append(rt)
            self.inputs.append("space" if space else "click")
            if self.dot:
                self.canvas.delete(self.dot)
                self.dot = None
            self.text(280, 350, f"{rt} 毫秒", 16,
                      "#5ad09a" if rt <= LAPSE_MS else "#e0a030")
            self.after(350, self.next_trial)

    def finish(self):
        self.state = "done"
        self.clear()
        s = summarize(self.rts, self.false_starts, TRIALS)
        if "error" in s:
            self.text(280, 160, "本次没有有效数据", 18)
            self.text(280, 300, "按空格重做 · Esc 退出", 14, self.DIM)
            self.state = "idle"
            return
        try:
            result = record(self.rts, self.false_starts, self.inputs, self.started_at)
            saved = result["time"]
        except Exception as e:
            saved = f"保存失败：{e}"
        self.text(280, 80, "完成", 24)
        self.text(280, 175,
                  f"中位 {s['median_ms']} 毫秒\n最快 {s['min_ms']} · 最慢 {s['max_ms']}"
                  f"\n漏拍 {s['lapses']} · 误点 {s['false_starts']}", 17)
        self.text(280, 300, f"已保存 {saved}", 13, self.DIM)
        self.text(280, 345, "按空格再做一次 · Esc 退出", 14, self.DIM)
        self.state = "idle"

    def run(self):
        self.root.mainloop()


def selftest() -> int:
    global OUT_DIR
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    s = summarize([280, 320, 250, 510, 300, 270, 290, 265], 1, 8)
    check("中位数", s["median_ms"] == 285)
    check("漏拍统计 >500ms", s["lapses"] == 1)
    check("误点计数", s["false_starts"] == 1)
    s2 = summarize([None, None], 0, 2)
    check("无有效样本报错", "error" in s2)

    with tempfile.TemporaryDirectory() as td:
        OUT_DIR = Path(td)
        r = record([280, 320, 250], 0)
        check("保存日期文件", (Path(td) / f"{r['date']}.json").exists())
        check("记录字段齐全", all(k in r for k in ("time", "ts", "block", "median_ms", "source",
                                                    "started_at", "ended_at", "duration_s")))
        check("无死字段 click_trials", "click_trials" not in r)
        check("临时文件已清理", not (Path(td) / f"{r['date']}.json.tmp").exists())
        r2 = record([280, 600, 250, 260], 2)
        rep = report(r["date"])
        check("report 可读", rep["available"] and rep["tests"] == 2)
        check("日报漏拍聚合", rep["lapses_total"] == 1)
        check("日报误点聚合", rep["false_starts_total"] == 2)
        check("log 追加", len((Path(td) / "log.jsonl").read_text(encoding="utf-8").splitlines()) == 2)
        check("无数据日期", report("2000-01-01")["available"] is False)
        partial = Path(td) / "2099-01-01.json"
        partial.write_text(json.dumps({"date": "2099-01-01", "tests": [
            {"median_ms": 250, "min_ms": 200, "lapses": 1, "false_starts": 0},
            {"median_ms": "bad", "min_ms": None},
        ]}, ensure_ascii=False), encoding="utf-8")
        pr = report("2099-01-01")
        check("部分坏记录可跳过", pr["available"] and pr["tests"] == 1 and pr["skipped"] == 1)
        broken = Path(td) / "2099-01-02.json"
        broken.write_text("{bad json", encoding="utf-8")
        check("损坏日报不崩溃", report("2099-01-02")["available"] is False)
        started = datetime(2026, 10, 5, 23, 59, 50)
        r3 = record([300, 310], 0, started_at=started)
        check("跨午夜按开始时间归属", r3["date"] == "2026-10-05" and r3["time"] == "23:59")
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="桌面反应力测试")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "report", "selftest"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "report":
        res = report(args.date)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              (f"反应力：中位 {res['median_ms']} 毫秒（{res['tests']} 次，最快 {res['best_ms']}）"
               if res.get("available") else f"无数据（{res.get('reason')}）"))
        return 0
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
