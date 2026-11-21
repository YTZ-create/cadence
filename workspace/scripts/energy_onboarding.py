# -*- coding: utf-8 -*-
"""energy_onboarding.py — 精力冷启动问卷（一次性 5 题）

用法：
  python -X utf8 scripts/energy_onboarding.py status [--json]
  python -X utf8 scripts/energy_onboarding.py save --weekday-bed 23:30 --weekday-wake 07:30 \
      --weekend-bed 00:30 --weekend-wake 09:00 --natural-sleep-min 480 \
      --chronotype 3 --post-lunch 1 --nap-caffeine "偶尔午睡，下午咖啡"

字段：
  chronotype 1=明显早晨型 … 5=明显晚上型
  post-lunch 0=不困 1=轻微 2=明显 3=很困
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path(__file__).resolve().parents[2])
WS = ROOT / "workspace"
ENERGY_DIR = Path(os.environ.get("OPENCLAW_ENERGY_DIR") or (WS / "data" / "health" / "energy"))
PATH = ENERGY_DIR / "onboarding.json"

DEFAULTS = {
    "weekday_bed": "23:30",
    "weekday_wake": "07:30",
    "weekend_bed": "00:30",
    "weekend_wake": "09:00",
    "natural_sleep_min": 480,
    "chronotype": 3,
    "post_lunch_sleepiness": 1,
    "nap_caffeine": "",
}


def _load() -> dict | None:
    if not PATH.exists():
        return None
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(obj: dict):
    ENERGY_DIR.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _hm_ok(s: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", s or ""))


def status() -> dict:
    data = _load()
    return {"completed": data is not None, "path": str(PATH), "data": data}


def save(args) -> dict:
    old = _load() or {}
    data = dict(DEFAULTS)
    data.update({k: v for k, v in (old or {}).items() if k in DEFAULTS})
    for key in ("weekday_bed", "weekday_wake", "weekend_bed", "weekend_wake"):
        val = getattr(args, key, None)
        if val is not None:
            if not _hm_ok(val):
                return {"error": f"{key} 需为 HH:MM"}
            data[key] = val
    if args.natural_sleep_min is not None:
        if not 240 <= args.natural_sleep_min <= 720:
            return {"error": "natural-sleep-min 需在 240–720"}
        data["natural_sleep_min"] = args.natural_sleep_min
    if args.chronotype is not None:
        if not 1 <= args.chronotype <= 5:
            return {"error": "chronotype 需在 1–5"}
        data["chronotype"] = args.chronotype
    if args.post_lunch is not None:
        if not 0 <= args.post_lunch <= 3:
            return {"error": "post-lunch 需在 0–3"}
        data["post_lunch_sleepiness"] = args.post_lunch
    if args.nap_caffeine is not None:
        data["nap_caffeine"] = args.nap_caffeine
    data["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _save(data)
    return {"ok": True, "path": str(PATH), "data": data}


def selftest() -> int:
    global ENERGY_DIR, PATH
    import tempfile

    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    with tempfile.TemporaryDirectory() as td:
        ENERGY_DIR = Path(td)
        PATH = ENERGY_DIR / "onboarding.json"
        check("默认未完成", status()["completed"] is False)
        args = argparse.Namespace(
            weekday_bed="23:30", weekday_wake="07:30",
            weekend_bed="00:30", weekend_wake="09:00",
            natural_sleep_min=480, chronotype=3, post_lunch=1,
            nap_caffeine="偶尔午睡",
        )
        r = save(args)
        check("保存成功", r.get("ok") is True)
        check("status 已完成", status()["completed"] is True)
        bad = argparse.Namespace(**{**vars(args), "chronotype": 9})
        check("拒绝非法档位", "error" in save(bad))
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="精力冷启动问卷")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("save")
    p.add_argument("--weekday-bed", default=None)
    p.add_argument("--weekday-wake", default=None)
    p.add_argument("--weekend-bed", default=None)
    p.add_argument("--weekend-wake", default=None)
    p.add_argument("--natural-sleep-min", type=int, default=None)
    p.add_argument("--chronotype", type=int, default=None)
    p.add_argument("--post-lunch", type=int, default=None)
    p.add_argument("--nap-caffeine", default=None)
    p.add_argument("--json", action="store_true")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "status":
        res = status()
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              ("已完成" if res["completed"] else "未完成"))
        return 0
    res = save(args)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
          (res.get("error") or "已保存"))
    return 0 if "error" not in res else 1


if __name__ == "__main__":
    sys.exit(main())
