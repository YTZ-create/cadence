import json
import sys
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "latest_sync.json"
STAGES = {1: "deep", 2: "light", 3: "rem", 4: "awake"}


def fmt(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def dur(x):
    return x.get("duration_s", x.get("duration_raw", 0))


def totals(s):
    h = s.get("header_totals", {})
    if "field_8" in h:
        return h["field_8"], h["field_10"], h["field_12"], h["field_14"]
    return h.get("deep_s", 0), h.get("light_s", 0), h.get("rem_s", 0), h.get("awake_s", 0)


def main():
    d = json.loads(DATA.read_text(encoding="utf-8"))
    hr = d.get("heart_rate", [])
    sessions = {}
    for s in d.get("sleep", []):
        cur = sessions.get(s["start"])
        if cur is None or s["end"] > cur["end"]:
            sessions[s["start"]] = s
    sessions = sorted(sessions.values(), key=lambda x: x["start"])
    print(f"sleep sessions: {len(sessions)}   hr samples total: {len(hr)}")

    for s in sessions:
        st, en = s["start"], s["end"]
        deep, light, rem, awake = [x / 60 for x in totals(s)]
        in_win = [h for h in hr if st <= h["ts"] <= en]
        vals = [h["bpm"] for h in in_win]
        print(f"\n== {s['start_time']} -> {s['end_time']}  ({(en-st)/60:.0f} min)")
        print(f"   header: deep {deep:.0f}  light {light:.0f}  rem {rem:.0f}  awake {awake:.0f} min")
        if vals:
            print(f"   HR: {len(vals)} samples  min {min(vals)}  avg {sum(vals)/len(vals):.1f}  max {max(vals)}")
        sums = {}
        stage_hr = {}
        for x in s["stages"]:
            code = x["stage"]
            sums[code] = sums.get(code, 0) + dur(x)
            w0, w1 = x["ts"], x["ts"] + dur(x)
            hv = [h["bpm"] for h in hr if w0 <= h["ts"] < w1]
            stage_hr.setdefault(code, []).extend(hv)
        print("   records:", {STAGES.get(k, k): round(v / 60) for k, v in sorted(sums.items())}, "min")
        print("   record total:", round(sum(sums.values()) / 60), "min vs session", round((en - st) / 60), "min")
        for code in sorted(stage_hr):
            v = stage_hr[code]
            if v:
                print(f"     HR in {STAGES.get(code, code):<5}: n={len(v):<3} min {min(v)}  avg {sum(v)/len(v):.1f}  max {max(v)}")

    long = max(sessions, key=lambda x: x["end"] - x["start"], default=None)
    if long:
        st, en = long["start"], long["end"]
        print("\nHourly HR during main sleep:")
        buckets = {}
        for h in hr:
            if st <= h["ts"] <= en:
                buckets.setdefault(time.strftime("%Y-%m-%d %H", time.localtime(h["ts"])), []).append(h["bpm"])
        for k in sorted(buckets):
            v = buckets[k]
            print(f"   {k[-2:]}:00  n={len(v):<3} avg {sum(v)/len(v):.1f}  min {min(v)}  max {max(v)}")

    spo2 = [x["percent"] for x in d.get("spo2", [])]
    stress = [x["value"] for x in d.get("stress", [])]
    if spo2:
        print(f"\nSpO2: {len(spo2)} samples  min {min(spo2)}  avg {sum(spo2)/len(spo2):.0f}  max {max(spo2)}")
    if stress:
        print(f"Stress: {len(stress)} samples  min {min(stress)}  avg {sum(stress)/len(stress):.0f}  max {max(stress)}")


if __name__ == "__main__":
    main()
