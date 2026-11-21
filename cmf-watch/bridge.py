import asyncio
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))

from cmf_watch import Watch, load_state, save_state  # noqa: E402
import parse_data  # noqa: E402

ADDRESS = os.environ.get("CMF_WATCH_ADDRESS") or None
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)
ACTIVITY_FILE = DATA_DIR / "latest_sync.json"
RAW_FRAMES = DATA_DIR / "last_frames.json"

MERGE_KEYS = {
    "activity": lambda x: x["ts"],
    "sleep": lambda x: x["start"],
    "workouts": lambda x: (x["start"], x["end"], x["type_code"]),
    "gps": lambda x: x["ts"],
    "heart_rate": lambda x: x["ts"],
    "workout_heart_rate": lambda x: x["ts"],
    "resting_heart_rate": lambda x: x["ts"],
    "spo2": lambda x: x["ts"],
    "stress": lambda x: x["ts"],
}

# The watch accepts one BLE client at a time, so all BLE work is serialized.
BLE_LOCK = threading.Lock()


async def _ensure(w):
    st = load_state()
    ak = st.get("authkey")
    if ak:
        try:
            await w.reconnect(bytes.fromhex(ak))
            r = await w.send(0xFFFF, 0x8006, expect=0x0006, timeout=5.0)
            if any(c2 == 0x0006 and len(body) == 4 for _, c2, body in r):
                return
        except BaseException:
            pass
    await w.handshake()
    save_state({"address": w.address or "", "secret": w.secret.hex(), "authkey": w.authkey.hex()})


async def _open(timeout=8.0):
    w = Watch(address=ADDRESS, verbose=False)
    await w.connect(timeout)
    await _ensure(w)
    return w


def _run(coro):
    with BLE_LOCK:
        return asyncio.run(coro)


def health():
    async def go():
        w = await _open()
        try:
            level = None
            try:
                raw = await w.client.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
                level = raw[0] if raw else None
            except Exception:
                pass
            return {"ok": level is not None, "battery": level}
        finally:
            await w.disconnect()

    return _run(go())


def _merge_saved(new):
    if not ACTIVITY_FILE.exists():
        return new
    try:
        prev = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return new
    if "counts" not in prev:
        return new
    for name, key in MERGE_KEYS.items():
        merged = {key(x): x for x in prev.get(name, [])}
        for x in new.get(name, []):
            old = merged.get(key(x))
            if name == "sleep" and old is not None and old.get("end", 0) > x.get("end", 0):
                continue
            merged[key(x)] = x
        new[name] = sorted(merged.values(), key=lambda x: x.get("start") or x.get("ts") or 0)
    prev_unparsed = prev.get("unparsed", {})
    for name, n in new.get("unparsed", {}).items():
        prev_unparsed[name] = prev_unparsed.get(name, 0) + n
    new["unparsed"] = prev_unparsed
    new["counts"] = {name: len(new[name]) for name in MERGE_KEYS}
    return new


def sync(duration=25.0):
    async def go():
        w = await _open()
        try:
            await w.send(0xFFFF, 0x8005, expect=0x0005, timeout=6.0)
            await w.send(0xFFFF, 0x9057, expect=0xA057, timeout=6.0)
            frames = await w.collect(duration)
            try:
                RAW_FRAMES.write_text(json.dumps(
                    [{"cmd1": c1, "cmd2": c2, "body": body.hex()} for c1, c2, body in frames],
                    ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            out = {"synced_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            out.update(parse_data.summarize(frames))
            out = _merge_saved(out)
            ACTIVITY_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            return out
        finally:
            await w.disconnect()

    return _run(go())


def notify(title, body):
    title_b = str(title).encode("utf-8")[:20]
    body_b = str(body).encode("utf-8")[:128]

    async def go():
        w = await _open()
        try:
            payload = bytes([0xFF, 0x00]) + struct.pack(">I", int(time.time())) + bytes([len(title_b)]) + title_b + body_b
            got = await w.send(0x0065, 0x0001, payload, expect=0x0003, timeout=8.0)
            ack = any(c2 == 0x0003 for _, c2, _ in got)
            return {"ok": ack, "ack": ack}
        finally:
            await w.disconnect()

    return _run(go())


class Handler(BaseHTTPRequestHandler):
    server_version = "cmf-bridge/0.1"

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _saved(self, name):
        if not ACTIVITY_FILE.exists():
            return 404, {"error": "no sync yet; POST /sync first"}
        data = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
        return 200, data.get(name, [])

    def do_GET(self):
        try:
            if self.path == "/health":
                self._send(200, health())
            elif self.path == "/activity":
                self._send(200, json.loads(ACTIVITY_FILE.read_text(encoding="utf-8")) if ACTIVITY_FILE.exists() else {"error": "no sync yet"})
            elif self.path == "/sleep":
                self._send(*self._saved("sleep"))
            elif self.path == "/workouts":
                self._send(*self._saved("workouts"))
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            if self.path == "/notify":
                title = payload.get("title") or "OpenClaw"
                body = payload.get("body") or ""
                if not body:
                    self._send(400, {"error": "body required"})
                    return
                self._send(200, notify(title, body))
            elif self.path == "/sync":
                duration = min(max(float(payload.get("duration", 25)), 5.0), 300.0)
                self._send(200, sync(duration))
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(500, {"error": str(exc)})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"cmf-bridge listening on http://127.0.0.1:{port}  watch={ADDRESS}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
