import asyncio
import collections
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmf_watch import Watch, ensure_session
import parse_data

ADDR = os.environ.get("CMF_WATCH_ADDRESS") or None


class Args:
    repair = False


def hist(frames):
    c = collections.Counter((a, b) for a, b, _ in frames)
    return {f"0x{a:04x}/0x{b:04x}": n for (a, b), n in sorted(c.items())}


async def main():
    w = Watch(address=ADDR, verbose=False)
    await w.connect(10)
    try:
        await ensure_session(w, Args())

        r = await w.send(0xFFFF, 0x8005, b"\xa5", expect=0x0005, timeout=6)
        print("ACK1:", [(hex(a), hex(b), c.hex()) for a, b, c in r])
        r = await w.send(0xFFFF, 0x9057, b"\xa5", expect=0xA057, timeout=6)
        print("ACK2:", [(hex(a), hex(b), c.hex()) for a, b, c in r])
        for a, b, c in r:
            if b == 0xA057 and len(c) >= 8:
                since = struct.unpack("<I", c[:4])[0]
                print("  since:", since, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)), "flags:", c[4:8].hex())

        frames = await w.collect(25)
        s = parse_data.summarize(frames)
        print("fetch:", hist(frames))
        print("counts:", {k: v for k, v in s["counts"].items() if v}, "unparsed:", s["unparsed"])
        if "--quick" in sys.argv:
            return

        for cmd1, cmd2 in ((0x0058, 0x0002), (0x0057, 0x0002)):
            for payload in (b"", b"\xa5"):
                r = await w.send(cmd1, cmd2, payload, expect=None, timeout=4)
                print(f"sent 0x{cmd1:04x}/0x{cmd2:04x} payload={payload.hex() or '-'} ->",
                      [(hex(a), hex(b), c.hex()[:80]) for a, b, c in r][:4])
                f2 = await w.collect(12)
                s2 = parse_data.summarize(f2)
                print("   hist:", hist(f2), "counts:", {k: v for k, v in s2["counts"].items() if v})
    finally:
        await w.disconnect()


asyncio.run(main())
