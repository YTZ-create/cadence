import struct
import time

STAGE_NAMES = {1: "deep", 2: "light", 3: "rem", 4: "awake"}

ACTIVITY_TYPES = {
    0x01: "outdoor_walking",
    0x02: "outdoor_running",
    0x03: "indoor_running",
    0x04: "mountain_hike",
    0x05: "outdoor_cycling",
    0x06: "jump_rope",
    0x07: "soccer",
    0x08: "basketball",
    0x09: "badminton",
    0x0A: "table_tennis",
    0x0B: "cricket",
    0x0C: "tennis",
    0x0D: "dynamic_cycle",
    0x0E: "rower",
    0x0F: "yoga",
    0x10: "free_training",
    0x13: "strength_training",
    0x18: "cross_trainer",
    0x19: "indoor_walking",
    0x1A: "hiking",
    0x72: "indoor_cycling",
}


def _ts(value):
    if not value:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _fmt(value, divisor=1.0):
    if not value:
        return None
    return round(value / divisor, 2)


def parse_activity(payload):
    out = []
    for off in range(0, len(payload) - 31, 32):
        ts, steps, dist, cal = struct.unpack_from("<IIII", payload, off)
        out.append({"ts": ts, "time": _ts(ts), "steps": steps, "distance_m": dist, "calories_raw": cal})
    return out


def parse_pairs(payload, field):
    out = []
    for off in range(0, len(payload) - 7, 8):
        ts, value = struct.unpack_from("<II", payload, off)
        out.append({"ts": ts, "time": _ts(ts), field: value})
    return out


def parse_resting_hr(payload):
    if len(payload) >= 5 and len(payload) % 5 == 0:
        out = []
        for off in range(0, len(payload) - 4, 5):
            ts = struct.unpack_from("<I", payload, off)[0]
            out.append({"ts": ts, "time": _ts(ts), "bpm": payload[off + 4]})
        return out
    return parse_pairs(payload, "bpm")


def parse_sleep(payload):
    if len(payload) < 18:
        return None
    start, end = struct.unpack_from("<II", payload, 0)
    # 时间戳合理性校验：2000-01-01 ~ 当前+2天、结束晚于开始、跨度≤48h；否则视为非睡眠帧
    now = time.time()
    if not (946684800 <= start <= now + 172800 and 946684800 <= end <= now + 172800 and start < end and end - start <= 172800):
        return None
    totals = struct.unpack_from("<HHHH", payload, 8)
    stages = []
    for off in range(18, len(payload) - 7, 8):
        ts, duration, stage = struct.unpack_from("<IHh", payload, off)
        stages.append(
            {
                "ts": ts,
                "time": _ts(ts),
                "duration_s": duration,
                "duration_min": round(duration / 60, 1),
                "stage": stage,
                "stage_name": STAGE_NAMES.get(stage, "unknown"),
            }
        )
    return {
        "start": start,
        "start_time": _ts(start),
        "end": end,
        "end_time": _ts(end),
        "duration_min": round((end - start) / 60, 1) if end > start else None,
        "header_totals": {
            "deep_s": totals[0],
            "light_s": totals[1],
            "rem_s": totals[2],
            "awake_s": totals[3],
        },
        "stages": stages,
    }


def _workout_record_size(length):
    if length >= 54 and length % 54 == 0:
        return 54
    if length % 32 == 0:
        return 32
    return 0


def parse_workouts(payload):
    size = _workout_record_size(len(payload))
    if not size:
        return []
    out = []
    for off in range(0, len(payload) - size + 1, size):
        start = struct.unpack_from("<I", payload, off)[0]
        duration = struct.unpack_from("<h", payload, off + 4)[0]
        kind = payload[off + 6]
        end = struct.unpack_from("<I", payload, off + 26)[0]
        has_gps = payload[off + 30] == 1
        out.append(
            {
                "start": start,
                "start_time": _ts(start),
                "end": end,
                "end_time": _ts(end),
                "duration_s": duration,
                "type_code": kind,
                "type": ACTIVITY_TYPES.get(kind, f"unknown_0x{kind:02x}"),
                "has_gps": has_gps,
                "record_size": size,
                "extra_hex": payload[off + 32:off + size].hex(),
            }
        )
    return out


def parse_gps(payload):
    out = []
    for off in range(0, len(payload) - 11, 12):
        ts, lon, lat = struct.unpack_from("<Iii", payload, off)
        out.append(
            {
                "ts": ts,
                "time": _ts(ts),
                "lat": round(lat / 1e7, 6),
                "lon": round(lon / 1e7, 6),
            }
        )
    return out


def _dedupe(items, key):
    seen = {}
    for item in items:
        seen[key(item)] = item
    return list(seen.values())


def summarize(frames):
    buckets = {
        "activity": [],
        "sleep": [],
        "workouts": [],
        "gps": [],
        "heart_rate": [],
        "workout_heart_rate": [],
        "resting_heart_rate": [],
        "spo2": [],
        "stress": [],
    }
    unknown = {}
    for cmd1, cmd2, body in frames:
        key = (cmd1, cmd2)
        if key == (0x0056, 0x0001):
            buckets["activity"] += parse_activity(body)
        elif key == (0x0058, 0x0001):
            session = parse_sleep(body)
            if session:
                buckets["sleep"].append(session)
            else:
                unknown["0x0058/0x0001_invalid"] = unknown.get("0x0058/0x0001_invalid", 0) + 1
        elif key == (0x0057, 0x0001):
            buckets["workouts"] += parse_workouts(body)
        elif key == (0xFFFF, 0xA05A):
            buckets["gps"] += parse_gps(body)
        elif key == (0x0053, 0x0001):
            buckets["heart_rate"] += parse_pairs(body, "bpm")
        elif key == (0x00E0, 0x0001):
            buckets["workout_heart_rate"] += parse_pairs(body, "bpm")
        elif key == (0x00DA, 0x0001):
            buckets["resting_heart_rate"] += parse_resting_hr(body)
        elif key == (0x0055, 0x0001):
            buckets["spo2"] += parse_pairs(body, "percent")
        elif key == (0x009D, 0x0001):
            buckets["stress"] += parse_pairs(body, "value")
        else:
            unknown[f"0x{cmd1:04x}/0x{cmd2:04x}"] = unknown.get(f"0x{cmd1:04x}/0x{cmd2:04x}", 0) + 1

    buckets["activity"] = _dedupe(buckets["activity"], lambda x: x["ts"])
    by_start = {}
    for session in buckets["sleep"]:
        current = by_start.get(session["start"])
        if current is None or session["end"] > current["end"]:
            by_start[session["start"]] = session
    buckets["sleep"] = list(by_start.values())
    buckets["workouts"] = _dedupe(buckets["workouts"], lambda x: (x["start"], x["end"], x["type_code"]))
    buckets["gps"] = _dedupe(buckets["gps"], lambda x: x["ts"])
    for name in ("heart_rate", "workout_heart_rate", "resting_heart_rate", "spo2", "stress"):
        buckets[name] = _dedupe(buckets[name], lambda x: x["ts"])

    for name in ("activity", "sleep", "workouts", "gps", "heart_rate", "workout_heart_rate", "resting_heart_rate", "spo2", "stress"):
        buckets[name].sort(key=lambda x: x.get("start") or x.get("ts") or 0)

    buckets["counts"] = {name: len(items) for name, items in buckets.items() if isinstance(items, list)}
    buckets["unparsed"] = unknown
    return buckets
