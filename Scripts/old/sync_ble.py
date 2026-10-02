#!/usr/bin/env python3
"""
Sync a paired beacon's slot counter from its BLE advertisement (no USB).

Scans for Apple Offline Finding advertisements (0x12 payload type), records
every MAC with RSSI and the time it was last seen, then walks the key chain
of each paired device from slot 0 upwards until a computed MAC matches one
of the captured advertisements. The matched index becomes the device's
last_known_slot (what retrieve_rotating.py uses to size its query window).

The pair time estimated from the slot (now - slot * slot_seconds) can drift
from the real pairing time if the device was powered off for a while -
technical correctness of the sync is unaffected.

Usage:
    python3 sync_ble.py                          # sync all paired devices
    python3 sync_ble.py --device esp32-s3-test
    python3 sync_ble.py --window 20 --max-slots 500
"""
import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bleak import BleakScanner
from findmy.accessory import FindMyAccessory

from fm_beacon import beacon_mac

SCRIPTS_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPTS_DIR / "state"
DEVICES_JSON = STATE_DIR / "devices.json"

APPLE_MFR = 0x004C
OF_TYPE = 0x12
OF_LEN = 0x19


def load_devices() -> list[dict]:
    if not DEVICES_JSON.exists():
        print(f"{DEVICES_JSON} not found - pair a device first")
        sys.exit(1)
    return json.loads(DEVICES_JSON.read_text())["devices"]


def save_devices(devices: list[dict]) -> None:
    DEVICES_JSON.write_text(json.dumps({"devices": devices}, indent=2) + "\n")
    DEVICES_JSON.chmod(0o600)


class RotatingAccessory(FindMyAccessory):
    def __init__(self, *, slot_seconds: int, **kwargs):
        super().__init__(sks=b"\x00" * 32, **kwargs)
        self._slot_interval = timedelta(seconds=slot_seconds)

    @property
    def interval(self) -> timedelta:
        return self._slot_interval

    def keys_at(self, ind: int):
        if ind < 0:
            return set()
        return {self._primary_key_at(ind)}


async def scan(window: float) -> dict[str, dict]:
    seen: dict[str, dict] = {}

    def cb(device, adv):
        mfr = adv.manufacturer_data.get(APPLE_MFR)
        if mfr is None or len(mfr) < 4 or mfr[0] != OF_TYPE or mfr[1] != OF_LEN:
            return
        now = time.time()
        mac = device.address.upper()
        e = seen.setdefault(mac, {"payload": mfr.hex(), "rssi": adv.rssi,
                                  "first": now, "last": now})
        e["last"] = now
        e["rssi"] = adv.rssi

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(window)
    await scanner.stop()
    return seen


def fmt_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def sync_device(dev: dict, macs: set[str], max_slots: int | None) -> bool:
    dev_id = dev["id"]
    ss = int(dev.get("slot_seconds", 120))
    paired_at = datetime.fromisoformat(dev["paired_at"])
    acc = RotatingAccessory(
        slot_seconds=ss,
        master_key=base64.b64decode(dev["master_key"]),
        skn=base64.b64decode(dev["skn"]),
        paired_at=paired_at, name=dev_id, identifier=dev_id,
    )

    last = int(dev.get("last_known_slot") or 0)
    now = datetime.now(timezone.utc)
    # the slot cannot be larger than the time elapsed since pairing
    # (it only advances while the device is running), add a small margin
    upper = int((now - paired_at).total_seconds() // ss) + 2
    upper = max(upper, last + 96)
    if max_slots is not None:
        upper = min(upper, max_slots)

    print(f"{dev_id}: walking chain slot 0..{upper} "
          f"(last known {last}, synced {dev.get('slot_synced_at', '?')})")
    for ind in range(upper + 1):
        key = acc._primary_key_at(ind)
        mac = beacon_mac(key)
        if mac in macs:
            old = last
            est_pair = now - timedelta(seconds=ind * ss)
            drift = (est_pair - paired_at).total_seconds()
            dev["last_known_slot"] = ind
            dev["slot_synced_at"] = now.isoformat()
            dev["sync_method"] = "ble"
            print(f"  MATCH slot {ind}  (mac {mac})")
            print(f"  previous sync: slot {old}")
            print(f"  estimated pair time: {est_pair.astimezone():%Y-%m-%d %H:%M:%S} "
                  f"(stored {paired_at.astimezone():%Y-%m-%d %H:%M:%S}, "
                  f"~{fmt_dur(abs(drift))} {'off' if drift > 0 else 'ahead'} - "
                  f"drift = time the device was off)")
            return True
    print("  no match - beacon out of range, unpowered, or --max-slots too low")
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--window", type=float, default=15.0,
                   help="scan window in seconds (default 15)")
    p.add_argument("--device", help="only sync this device id")
    p.add_argument("--max-slots", type=int, default=None, metavar="N",
                   help="cap the chain search at N slots")
    args = p.parse_args()

    devices = load_devices()
    if args.device:
        devices = [d for d in devices if d["id"] == args.device]
        if not devices:
            print(f"device '{args.device}' not found in {DEVICES_JSON}")
            return 1

    print(f"scanning {args.window:g}s for Apple Offline Finding beacons...")
    seen = asyncio.run(scan(args.window))

    if not seen:
        print("no 0x12 advertisements captured - is BLE up and the beacon running?")
        return 1

    now = time.time()
    print(f"\ncaptured {len(seen)} OF beacon(s):")
    for mac, e in sorted(seen.items(), key=lambda kv: -kv[1]["rssi"]):
        age = fmt_dur(now - e["last"])
        print(f"  {mac}  rssi={e['rssi']:>4}  last_seen={fmt_local(e['last'])} "
              f"({age} ago)  {e['payload'][:24]}...")

    macs = set(seen)
    print()
    results = [sync_device(d, macs, args.max_slots) for d in devices]
    ok = any(results)

    if ok:
        save_devices(devices)
        print(f"\nupdated {DEVICES_JSON}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
