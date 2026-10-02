#!/usr/bin/env python3
"""
Verify the beacon actually advertises keys we can decrypt.

Reads devices.json (UART-paired beacons; keys rotate) and accepts a match
with any recent slot key of any device. Falls back to the legacy static
key files if devices.json is absent.

Usage: python3 verify_beacon.py [scan_seconds]
"""
import asyncio
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bleak import BleakScanner
from findmy.accessory import FindMyAccessory
from findmy.keys import KeyPair

from fm_beacon import beacon_mac, payload_matches

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
STATE_DIR = SCRIPTS_DIR / "state"
KEYS_DIR = ROOT / "KeyGen" / "keys"
DEVICES_JSON = STATE_DIR / "devices.json"
KEYS = KEYS_DIR


class BeaconAccessory(FindMyAccessory):
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


def rotating_targets() -> dict[str, KeyPair]:
    data = json.loads(DEVICES_JSON.read_text())
    now = datetime.now(timezone.utc)
    out = {}
    for dev in data["devices"]:
        paired_at = datetime.fromisoformat(dev["paired_at"])
        alignment_date = paired_at
        alignment_index = 0
        if dev.get("slot_synced_at") and dev.get("last_known_slot") is not None:
            alignment_date = datetime.fromisoformat(dev["slot_synced_at"])
            alignment_index = int(dev["last_known_slot"])
        acc = BeaconAccessory(
            slot_seconds=int(dev.get("slot_seconds", 120)),
            master_key=base64.b64decode(dev["master_key"]),
            skn=base64.b64decode(dev["skn"]),
            paired_at=paired_at,
            alignment_date=alignment_date,
            alignment_index=alignment_index,
            name=dev["id"], identifier=dev["id"],
        )
        # accept a lag of up to 96 slots (reboots etc.) behind the expected slot
        max_i = acc.get_max_index(now)
        min_i = max(0, max_i - 96)
        print(f"{dev['id']}: accepting slots {min_i}..{max_i}")
        for ind in range(min_i, max_i + 1):
            key = acc._primary_key_at(ind)
            out[beacon_mac(key)] = key
    return out


def static_targets() -> dict[str, KeyPair]:
    priv = base64.b64decode((KEYS / "private_0.key").read_text().strip())
    pub = base64.b64decode((KEYS / "public_0.key").read_text().strip())
    kp = KeyPair(priv)
    assert kp.adv_key_bytes == pub[:28], "private/public key mismatch"
    return {beacon_mac(kp): kp}


def expected() -> dict[str, KeyPair]:
    if DEVICES_JSON.exists():
        return rotating_targets()
    return static_targets()


async def scan(duration: float, targets: dict[str, KeyPair]):
    found = {"ok": False, "mac": None, "got": None, "rssi": None}

    def cb(device, adv):
        mfr = adv.manufacturer_data.get(0x004C)
        if mfr is None:
            return
        key = targets.get(device.address.upper())
        if key is None:
            return
        found["mac"], found["got"] = device.address, mfr.hex()
        if payload_matches(mfr, key):
            found.update(ok=True, rssi=adv.rssi)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()
    return found


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    targets = expected()
    print(f"expecting {len(targets)} known key(s)/MAC(s)")
    print(f"Scanning {duration}s ...")

    res = asyncio.run(scan(duration, targets))

    print()
    if res["ok"]:
        print("PASS: beacon advertises a key we hold")
        print(f"  MAC   : {res['mac']}")
        print(f"  RSSI  : {res['rssi']} dBm")
        return 0
    if res["mac"]:
        print(f"FAIL: MAC {res['mac']} seen but payload differs!")
        print(f"  received: {res['got']}")
        return 2
    print("FAIL: no known beacon MAC seen (unpaired? powered? slot too old - run pair_device.py --sync)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
