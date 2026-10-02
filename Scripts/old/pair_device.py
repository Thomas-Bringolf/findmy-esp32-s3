#!/usr/bin/env python3
"""
Pair an ESP32-S3 Find My beacon over UART, or sync its current slot.

Protocol (line based, replies may be interleaved with log output):
    PING                  -> PONG fw=1 paired=<0|1>
    PAIR <pin>            -> OK PAIRING | ERR PIN          (arms pairing for 60 s)
    KEYS <mk> <skn> [adv_ms rot_sec dbg_sec]
                          -> OK KEYS | ERR ...
    SLOT?                 -> SLOT <n> | ERR UNPAIRED
    KEY?                  -> KEY <hex56> | ERR UNPAIRED
    DEBUG <0|1>           -> OK DEBUG <0|1>
    STAT?                 -> STAT paired=.. slot=.. debug=..
    STATUS?              -> STATUS paired=.. slot=.. debug=.. adv_ms=.. rot_sec=.. dbg_sec=..
    CONFIG <adv_ms> <rot_sec> <dbg_sec>   -> OK CONFIG ... (reapply on live device)

Timing defaults if not supplied at pairing time: adv_ms=2000 (0.2-60 s),
rot_sec=120 (key rotation), dbg_sec=600 (0 or 60-3600; out-of-range -> 600).

Usage:
    python3 pair_device.py --port /dev/ttyACM0 --id esp32-s3-test          # pair
    python3 pair_device.py --sync --id esp32-s3-test [--port ...]          # sync slot
    python3 pair_device.py --list
"""
import argparse
import base64
import json
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import serial

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
STATE_DIR = SCRIPTS_DIR / "state"
KEYS_DIR = ROOT / "KeyGen" / "keys"
DEVICES_JSON = STATE_DIR / "devices.json"
PIN_FILE = KEYS_DIR / "pairing_pin.txt"

MARKERS = ("PONG", "OK ", "ERR", "SLOT ", "KEY ", "STAT ", "STATUS ")

# Console lines produced by esp_log look like "I (1234) uart_cmd: ...".
# Their payload can contain a marker word (e.g. "BLE_ERR_CMD_DISALLOWED"),
# so the prefix is stripped before matching a reply.
LOG_PREFIX = re.compile(r"^[IVDEW] \(\d+\) [^:]+: ")


def reply_of(raw: str) -> str | None:
    """Return the reply contained in `raw`, or None if it is a log line."""
    text = LOG_PREFIX.sub("", raw.strip())
    for m in MARKERS:
        if text.startswith(m):
            return text
    return None


class Beacon:
    def __init__(self, port: str, timeout: float = 6.0):
        self.s = serial.Serial(port, 115200, timeout=0.5)
        self.timeout = timeout
        time.sleep(0.3)
        self.s.reset_input_buffer()

    def close(self):
        self.s.close()

    def cmd(self, line: str, wait: float | None = None) -> str:
        wait = self.timeout if wait is None else wait
        # drop any late reply of the previous command so it cannot be
        # mistaken for this command's answer
        prev_timeout = self.s.timeout
        self.s.timeout = 0.05
        try:
            while self.s.readline():
                pass
        finally:
            self.s.timeout = prev_timeout
        self.s.write(line.encode() + b"\r\n")
        deadline = time.time() + wait
        while time.time() < deadline:
            raw = self.s.readline().decode(errors="replace")
            reply = reply_of(raw)
            if reply is not None:
                return reply
        return "TIMEOUT"

    def ping(self) -> bool:
        return self.cmd("PING").startswith("PONG")


def load_devices() -> dict:
    if DEVICES_JSON.exists():
        return json.loads(DEVICES_JSON.read_text())
    return {"devices": []}


def save_devices(data: dict) -> None:
    DEVICES_JSON.write_text(json.dumps(data, indent=2) + "\n")
    DEVICES_JSON.chmod(0o600)


def find_device(data: dict, dev_id: str) -> dict | None:
    for d in data["devices"]:
        if d["id"] == dev_id:
            return d
    return None


def derive_expected(master: bytes, skn: bytes, paired_at: datetime):
    from findmy.accessory import FindMyAccessory

    return FindMyAccessory(
        master_key=master, skn=skn,
        sks=b"\x00" * 32,  # unused: primary-key chain only
        paired_at=paired_at, name="t", identifier="t",
    )


def do_pair(args) -> int:
    pin = args.pin or PIN_FILE.read_text().strip()
    data = load_devices()
    if find_device(data, args.id) is not None and not args.force:
        print(f"device id '{args.id}' already in {DEVICES_JSON} (use --force to re-pair)")
        return 1

    b = Beacon(args.port)
    try:
        if not b.ping():
            print("ERROR: device not responding to PING")
            return 1

        r = b.cmd(f"PAIR {pin}")
        if not r.startswith("OK PAIRING"):
            print(f"ERROR: pairing refused: {r}")
            return 1

        master = secrets.token_bytes(28)
        skn = secrets.token_bytes(32)
        b64 = lambda x: base64.b64encode(x).decode()
        adv_ms = args.adv_ms or 2000
        rot_sec = args.rot_sec or 120
        dbg_sec = args.dbg_sec if args.dbg_sec is not None else 600
        r = b.cmd(f"KEYS {b64(master)} {b64(skn)} {adv_ms} {rot_sec} {dbg_sec}", wait=8)
        if not r.startswith("OK KEYS"):
            print(f"ERROR: device rejected keys: {r}")
            return 1

        slot = b.cmd("SLOT?")
        key = b.cmd("KEY?", wait=8)
        if not slot.startswith("SLOT 0") or not key.startswith("KEY "):
            print(f"ERROR: unexpected state after pairing: {slot} / {key}")
            return 1

        # cross-check the device's derived key against the findmy library
        paired_at = datetime.now(timezone.utc)
        acc = derive_expected(master, skn, paired_at)
        expected = acc._primary_key_at(0).adv_key_bytes.hex()
        got = key.split()[1]
        if got != expected:
            print(f"ERROR: key mismatch! device={got} findmy={expected}")
            return 1

        if args.debug is not None:
            b.cmd(f"DEBUG {1 if args.debug else 0}")

        st = b.cmd("STATUS?")
        if not st.startswith("STATUS "):
            print(f"WARNING: device did not answer STATUS?: {st}")

        entry = {
            "id": args.id,
            "port": args.port,
            "paired_at": paired_at.isoformat(),
            "master_key": b64(master),
            "skn": b64(skn),
            "slot_seconds": args.slot_seconds,
            "last_known_slot": 0,
            "slot_synced_at": paired_at.isoformat(),
            "adv_ms": adv_ms,
            "rot_sec": rot_sec,
            "dbg_sec": dbg_sec,
        }

        # replace existing entry with same id (re-pair) or append
        data["devices"] = [d for d in data["devices"] if d["id"] != args.id]
        data["devices"].append(entry)
        save_devices(data)

        print(f"paired '{args.id}' on {args.port}")
        print(f"  slot 0 X: {got}")
        print(f"  adv_ms={adv_ms} rot_sec={rot_sec} dbg_sec={dbg_sec}")
        print(f"  stored in {DEVICES_JSON}")
        return 0
    finally:
        b.close()


def do_sync(args) -> int:
    data = load_devices()
    dev = find_device(data, args.id)
    if dev is None:
        print(f"device '{args.id}' not found in {DEVICES_JSON}")
        return 1

    port = args.port or dev.get("port")
    b = Beacon(port)
    try:
        if not b.ping():
            print("ERROR: device not responding to PING")
            return 1

        slot = b.cmd("SLOT?")
        if not slot.startswith("SLOT "):
            print(f"ERROR: {slot}")
            return 1
        slot_n = int(slot.split()[1])

        # verify the device's key matches what we derive for that slot
        master = base64.b64decode(dev["master_key"])
        skn = base64.b64decode(dev["skn"])
        acc = derive_expected(master, skn,
                              datetime.fromisoformat(dev["paired_at"]))
        expected = acc._primary_key_at(slot_n).adv_key_bytes.hex()
        key = b.cmd("KEY?", wait=8)
        if not key.startswith("KEY ") or key.split()[1] != expected:
            print(f"ERROR: key mismatch at slot {slot_n}: {key} vs {expected}")
            return 1

        now = datetime.now(timezone.utc)
        dev["last_known_slot"] = slot_n
        dev["slot_synced_at"] = now.isoformat()
        if port != dev.get("port"):
            dev["port"] = port
        save_devices(data)

        print(f"synced '{args.id}': slot {slot_n} at {now.isoformat()}")
        return 0
    finally:
        b.close()


def do_list(_) -> int:
    data = load_devices()
    if not data["devices"]:
        print(f"no devices in {DEVICES_JSON}")
        return 0
    for d in data["devices"]:
        print(f"{d['id']:20s} port={d.get('port', '?'):15s} "
              f"paired={d['paired_at']} slot={d.get('last_known_slot', '?')} "
              f"adv_ms={d.get('adv_ms', '?')} rot_sec={d.get('rot_sec', '?')} "
              f"dbg_sec={d.get('dbg_sec', '?')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument("--id", help="device id used in devices.json")
    p.add_argument("--pin", help="pairing PIN (default: KeyGen/keys/pairing_pin.txt)")
    p.add_argument("--slot-seconds", type=int, default=120,
                   help="slot length the firmware was built with (default 120)")
    p.add_argument("--force", action="store_true", help="overwrite existing device id")
    p.add_argument("--debug", type=int, default=None, metavar="0|1",
                   help="set device debug flag after pairing")
    p.add_argument("--adv-ms", type=int, default=None,
                   help="advertisement period in ms (200..60000, default 2000)")
    p.add_argument("--rot-sec", type=int, default=None,
                   help="key rotation period in seconds (default 120)")
    p.add_argument("--dbg-sec", type=int, default=None,
                   help="debug window in seconds (0 or 60..3600, default 600)")
    p.add_argument("--sync", action="store_true", help="sync current slot into devices.json")
    p.add_argument("--list", action="store_true", help="list known devices")
    args = p.parse_args()

    if args.list:
        return do_list(args)
    if not args.id:
        p.error("--id is required (unless --list)")
    if args.sync:
        return do_sync(args)
    return do_pair(args)


if __name__ == "__main__":
    sys.exit(main())
