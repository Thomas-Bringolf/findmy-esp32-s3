#!/usr/bin/env python3
"""
Edge-case tests for the beacon's UART console / pairing protocol.

The device must be in its post-boot debug window; by default the script
issues a DTR/RTS pulse first so it always starts in one.

Groups:
  A  basic protocol + malformed input (device paired)
  B  pairing window: arm / preserve / expire / consume + happy-path re-pair
  C  unpaired state after WIPE, including the wake-into-console fallback
  D  config restore; new keys are recorded in devices.json

Note: `CONFIG <adv> <rot> 0` (disables the debug window entirely) is NOT
tested on purpose - a device paired with dbg_sec=0 gives you ~0 ms of
console after every reset, which is unrecoverable over UART.

Usage:
    python3 test_uart.py [--port /dev/ttyACM0] [--id esp32-s3-test] [--no-reset]
"""
import argparse
import base64
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_device import (  # noqa: E402
    Beacon,
    PIN_FILE,
    derive_expected,
    load_devices,
    save_devices,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail and not ok else ""))
    return ok


def step(msg: str) -> None:
    print(f"\n=== {msg}")


def pulse_reset(b: Beacon) -> None:
    b.s.dtr = False
    b.s.rts = True
    time.sleep(0.1)
    b.s.rts = False
    time.sleep(1.5)


def open_port(port: str, reset: bool, retries: int = 10) -> Beacon:
    for _ in range(retries):
        try:
            b = Beacon(port)
        except Exception as exc:  # device still re-enumerating
            time.sleep(1.0)
            last = exc
            continue
        if reset:
            pulse_reset(b)
        return b
    raise RuntimeError(f"cannot open {port}: {last}")


def wait_ping(b: Beacon, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if b.ping():
            return True
        time.sleep(0.5)
    return False


def gen_keys() -> tuple[str, str]:
    master = secrets.token_bytes(28)
    skn = secrets.token_bytes(32)
    return base64.b64encode(master).decode(), base64.b64encode(skn).decode()


def test_group_a(b: Beacon, pin: str) -> None:
    step("A: basic protocol and malformed input")

    r = b.cmd("PING")
    check("PING -> PONG fw=2", r.startswith("PONG fw=2"), r)

    r = b.cmd("STAT?")
    check("STAT?", r.startswith("STAT paired="), r)

    r = b.cmd("STATUS?")
    check("STATUS?", r.startswith("STATUS paired="), r)

    r = b.cmd("FOOBAR")
    check("unknown command -> ERR CMD", r.startswith("ERR CMD"), r)

    r = b.cmd("PING with extra args")
    check("extra args on PING still answered", r.startswith("PONG"), r)

    # empty line / whitespace-only line: no reply, but no crash either
    b.cmd("")
    b.cmd("   ")
    r = b.cmd("PING")
    check("empty line is ignored, device alive", r.startswith("PONG"), r)

    # line longer than the 256 byte buffer must be dropped, not executed
    # (chunked write: a single 402-byte burst can stall the USB CDC path)
    for off in range(0, 400, 100):
        b.s.write(b"A" * 100)
        time.sleep(0.02)
    b.s.write(b"\r\n")
    time.sleep(0.5)
    r = b.cmd("PING")
    check("400-char line dropped, device alive", r.startswith("PONG"), r)

    r = b.cmd("PAIR")
    check("PAIR without PIN -> ERR ARGS", r.startswith("ERR ARGS"), r)

    r = b.cmd("PAIR 11111111")
    check("PAIR wrong PIN -> ERR PIN", r.startswith("ERR PIN"), r)

    r = b.cmd("WIPE")
    check("WIPE without PIN -> ERR ARGS", r.startswith("ERR ARGS"), r)

    r = b.cmd("WIPE 11111111")
    check("WIPE wrong PIN -> ERR PIN", r.startswith("ERR PIN"), r)

    r = b.cmd("KEYS")
    check("KEYS without args -> ERR ARGS", r.startswith("ERR ARGS"), r)

    r = b.cmd("KEYS AAAA BBBB")
    check("KEYS while not armed -> ERR NOPAIR", r.startswith("ERR NOPAIR"), r)

    for args, want in (
        ("", "ERR ARGS"),
        ("2000 120", "ERR ARGS"),
        ("abc 120 600", "ERR ARGS"),
        ("2000x 120 600", "ERR ARGS"),
        ("-1 120 600", "ERR ARGS"),
        ("4294967296 120 600", "ERR ARGS"),
        ("2000 120 600 9", "ERR ARGS"),
        ("+2000 120 600", "ERR ARGS"),
    ):
        r = b.cmd(f"CONFIG {args}".strip())
        check(f"CONFIG {args.strip() or '<none>'} -> {want}", r.startswith(want), r)

    # the tokenizer collapses repeated separators, which is fine
    r = b.cmd("CONFIG  2000  120  600")
    check("extra separators are tolerated", r.startswith("OK CONFIG"), r)

    r = b.cmd("DEBUG 5")
    check("DEBUG 5 -> ERR ARGS", r.startswith("ERR ARGS"), r)
    r = b.cmd("DEBUG x")
    check("DEBUG x -> ERR ARGS", r.startswith("ERR ARGS"), r)
    r = b.cmd("DEBUG 1")
    check("DEBUG 1 -> OK DEBUG 1", r.startswith("OK DEBUG 1"), r)
    r = b.cmd("DEBUG 0")
    check("DEBUG 0 -> OK DEBUG 0", r.startswith("OK DEBUG 0"), r)

    # clamping: out-of-range values are corrected, not rejected
    r = b.cmd("CONFIG 100 120 600")
    check("adv_ms below minimum clamps to 200", r.startswith("OK CONFIG adv_ms=200"), r)
    r = b.cmd("CONFIG 999999 120 600")
    check("adv_ms above maximum clamps to 60000", r.startswith("OK CONFIG adv_ms=60000"), r)
    r = b.cmd("CONFIG 2000 0 600")
    check("rot_sec below minimum clamps to 1", "rot_sec=1" in r, r)
    r = b.cmd("CONFIG 2000 999999 600")
    check("rot_sec above maximum clamps to 86400", "rot_sec=86400" in r, r)
    r = b.cmd("CONFIG 2000 120 5")
    check("dbg_sec out of range falls back to 600", "dbg_sec=600" in r, r)

    r = b.cmd("CONFIG 2000 120 600")
    check("config restored to defaults", r.startswith("OK CONFIG adv_ms=2000 rot_sec=120 dbg_sec=600"), r)


def test_group_b(b: Beacon, pin: str) -> None:
    step("B: pairing window (arm / preserve / expire / consume)")

    mk, skn = gen_keys()
    r = b.cmd(f"KEYS {mk} {skn}")
    check("KEYS before PAIR -> ERR NOPAIR", r.startswith("ERR NOPAIR"), r)

    r = b.cmd(f"PAIR {pin}")
    check("PAIR correct PIN -> OK PAIRING", r.startswith("OK PAIRING"), r)

    r = b.cmd("KEYS not-base64 %%%%")
    check("bad base64 -> ERR B64", r.startswith("ERR B64"), r)
    r = b.cmd("KEYS not-base64 %%%%")
    check("window survives a failed KEYS (still armed)", r.startswith("ERR B64"), r)

    r = b.cmd("KEYS AAAA BBBB 2000 120 600")
    check("bad length -> ERR B64, window still armed", r.startswith("ERR B64"), r)

    r = b.cmd("CONFIG abc 120 600")
    check("bad CONFIG while armed -> ERR ARGS (window kept)", r.startswith("ERR ARGS"), r)

    print("  ... waiting 61 s for the pairing window to expire ...")
    time.sleep(61)
    r = b.cmd(f"KEYS {mk} {skn}")
    check("window expired -> ERR NOPAIR", r.startswith("ERR NOPAIR"), r)

    # happy path re-pair, with optional timing arguments
    r = b.cmd(f"PAIR {pin}")
    check("re-arm -> OK PAIRING", r.startswith("OK PAIRING"), r)
    r = b.cmd(f"KEYS {mk} {skn} 2000 120 600", wait=8)
    check("KEYS accepted -> OK KEYS", r.startswith("OK KEYS"), r)

    r = b.cmd(f"KEYS {mk} {skn}")
    check("second KEYS -> ERR NOPAIR (window consumed)", r.startswith("ERR NOPAIR"), r)

    r = b.cmd("SLOT?")
    check("SLOT? -> SLOT 0 after re-pair", r.startswith("SLOT 0"), r)

    r = b.cmd("KEY?", wait=8)
    if check("KEY? answered", r.startswith("KEY "), r):
        paired_at = datetime.now(timezone.utc)
        acc = derive_expected(base64.b64decode(mk), base64.b64decode(skn), paired_at)
        expected = acc._primary_key_at(0).adv_key_bytes.hex()
        check("device key matches findmy derivation", r.split()[1] == expected,
              f"device={r.split()[1]} expected={expected}")


def test_group_c(b: Beacon, port: str, pin: str) -> Beacon:
    step("C: unpaired state after WIPE")

    r = b.cmd(f"WIPE {pin}", wait=5)
    check("WIPE correct PIN -> OK WIPE", r.startswith("OK WIPE"), r)
    b.close()

    b = open_port(port, reset=True)
    if not check("device back after WIPE", wait_ping(b, 15)):
        raise RuntimeError("device did not come back after WIPE")

    r = b.cmd("PING")
    check("PING reports paired=0", r.strip().endswith("paired=0"), r)
    r = b.cmd("SLOT?")
    check("SLOT? while unpaired -> ERR UNPAIRED", r.startswith("ERR UNPAIRED"), r)
    r = b.cmd("KEY?")
    check("KEY? while unpaired -> ERR UNPAIRED", r.startswith("ERR UNPAIRED"), r)

    mk, skn = gen_keys()
    r = b.cmd(f"KEYS {mk} {skn}")
    check("KEYS while unarmed -> ERR NOPAIR", r.startswith("ERR NOPAIR"), r)
    r = b.cmd("PAIR 11111111")
    check("unpaired, wrong PIN -> ERR PIN", r.startswith("ERR PIN"), r)
    r = b.cmd(f"PAIR {pin}")
    check("unpaired, correct PIN -> OK PAIRING", r.startswith("OK PAIRING"), r)
    r = b.cmd("CONFIG abc 120 600")
    check("bad CONFIG while armed -> ERR ARGS", r.startswith("ERR ARGS"), r)

    # WIPE reset the config to defaults, so shorten the window NOW - it only
    # takes effect for the session started by the reset below.
    r = b.cmd("CONFIG 2000 120 60")
    check("short debug window requested", r.startswith("OK CONFIG"), r)
    return b


def test_group_c_fallback(b: Beacon, port: str, pin: str):
    step("C2: deep-sleep wake returns to the console, then re-pair")

    b.close()
    b = open_port(port, reset=True)
    if not check("fresh session started", wait_ping(b, 15)):
        raise RuntimeError("device did not boot into a debug session")

    print("  ... waiting 70 s for the 60 s debug window to end, the device "
          "to deep-sleep and to wake into the console again ...")
    time.sleep(70)

    if not check("console alive after deep-sleep wake", wait_ping(b, 30)):
        raise RuntimeError("device did not return to the console after deep sleep")

    # Now the happy path from the unpaired state, inside the post-wake window.
    mk, skn = gen_keys()
    r = b.cmd(f"PAIR {pin}")
    check("re-pair after fallback -> OK PAIRING", r.startswith("OK PAIRING"), r)
    r = b.cmd(f"KEYS {mk} {skn} 2000 120 600", wait=8)
    check("KEYS after fallback -> OK KEYS", r.startswith("OK KEYS"), r)
    paired_at = datetime.now(timezone.utc)

    r = b.cmd("SLOT?")
    check("slot 0 after re-pair", r.startswith("SLOT 0"), r)
    r = b.cmd("KEY?", wait=8)
    if check("KEY? answered", r.startswith("KEY "), r):
        acc = derive_expected(base64.b64decode(mk), base64.b64decode(skn), paired_at)
        expected = acc._primary_key_at(0).adv_key_bytes.hex()
        check("device key matches findmy derivation", r.split()[1] == expected,
              f"device={r.split()[1]} expected={expected}")

    r = b.cmd("CONFIG 2000 120 600")
    check("debug window restored to 600 s", r.startswith("OK CONFIG adv_ms=2000 rot_sec=120 dbg_sec=600"), r)
    return b, mk, skn, paired_at


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--id", default="esp32-s3-test")
    ap.add_argument("--no-reset", action="store_true",
                    help="assume the device is already in its debug window")
    args = ap.parse_args()

    pin = PIN_FILE.read_text().strip()
    b = open_port(args.port, reset=not args.no_reset)
    if not wait_ping(b, 20):
        print("ERROR: device not answering PING (not in its debug window?)")
        return 2

    # The suite needs one long console session. Persist a 600 s window and
    # restart, because a dbg_sec change never affects a session that is
    # already running (an earlier aborted run can leave dbg_sec=60 behind).
    if not args.no_reset:
        r = "TIMEOUT"
        for _ in range(3):
            r = b.cmd("CONFIG 2000 120 600", wait=5)
            if r.startswith("OK CONFIG"):
                break
            time.sleep(1.0)
        if not r.startswith("OK CONFIG"):
            print(f"ERROR: cannot prime the debug window: {r}")
            return 2
        b.close()
        b = open_port(args.port, reset=True)
        if not wait_ping(b, 20):
            print("ERROR: device did not reboot into the debug window")
            return 2
        r = b.cmd("STATUS?")
        if "dbg_sec=600" not in r:
            print(f"ERROR: debug window is not 600 s: {r}")
            return 2

    mk = skn = None
    paired_at = None

    try:
        test_group_a(b, pin)
        test_group_b(b, pin)
        b = test_group_c(b, args.port, pin)
        b, mk, skn, paired_at = test_group_c_fallback(b, args.port, pin)
    except Exception as exc:
        print(f"\nABORT: {exc}")
        RESULTS.append(("suite completed", False, str(exc)))
    finally:
        try:
            b.close()
        except Exception:
            pass

    # Record the keys the device now advertises so verify_beacon.py works.
    if mk and skn:
        data = load_devices()
        entry = {
            "id": args.id,
            "port": args.port,
            "paired_at": paired_at.isoformat(),
            "master_key": mk,
            "skn": skn,
            "slot_seconds": 120,
            "last_known_slot": 0,
            "slot_synced_at": paired_at.isoformat(),
            "adv_ms": 2000,
            "rot_sec": 120,
            "dbg_sec": 600,
        }
        data["devices"] = [d for d in data["devices"] if d["id"] != args.id]
        data["devices"].append(entry)
        save_devices(data)
        print(f"\nrecorded new keys for '{args.id}' in devices.json")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
