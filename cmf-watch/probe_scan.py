import asyncio

from bleak import BleakScanner


async def main():
    devs = await BleakScanner.discover(timeout=15.0, return_adv=True)
    rows = []
    for addr, (d, adv) in devs.items():
        name = adv.local_name or d.name or ""
        rows.append((name, addr, adv.rssi, list(adv.service_uuids), {k: v.hex() for k, v in adv.manufacturer_data.items()}))
    for name, addr, rssi, uuids, mfr in sorted(rows, key=lambda r: (r[0] == "", r[0].lower())):
        print(f"{addr}  rssi={rssi}  name={name!r}")
        if uuids:
            print(f"    uuids: {uuids}")
        if mfr:
            print(f"    mfr:   {mfr}")


asyncio.run(main())
