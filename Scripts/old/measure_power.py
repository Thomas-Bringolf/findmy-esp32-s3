#!/usr/bin/env python3
"""
Measure the beacon's awake/sleep duty cycle from the firmware's PWR telemetry.

The firmware prints two lines per steady-state cycle on the console:

    I (..) open_haystack: PWR wake t=<us since boot>
    I (..) open_haystack: PWR sleep awake_us=<us> sleep_us=<us>

awake_us is the time from wake-up until light sleep starts (advertising
burst, key rotation, logging); sleep_us is the light-sleep request for the
remainder of the advertising period.

Typical run (the debug window must end first, otherwise nothing sleeps):

    python3 measure_power.py --reset --dbg-sec 60 --seconds 60

The device is left at that dbg_sec; pass --dbg-sec 600 to put it back.

Optionally turn the duty cycle into an average current with measured
per-state currents of your board:

    python3 measure_power.py --reset --dbg-sec 60 --seconds 60 \
        --ma-awake 45 --ma-sleep 1.6

Usage:
    python3 measure_power.py [--port /dev/ttyACM0] [--seconds 60] [--reset]
                             [--dbg-sec N] [--adv-ms 2000] [--rot-sec 120]
                             [--ma-awake X] [--ma-sleep Y] [--json out.json]
"""
import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pair_device import reply_of  # noqa: E402

PWR_SLEEP = re.compile(r"PWR sleep awake_us=(\d+) sleep_us=(\d+)")
PWR_WAKE = re.compile(r"PWR wake t=(\d+)")


def pulse_reset(s: serial.Serial) -> None:
    s.dtr = False
    s.rts = True
    time.sleep(0.1)
    s.rts = False
    time.sleep(3.0)


def send(s: serial.Serial, line: str, wait: float = 3.0) -> str:
    s.write(line.encode() + b"\r\n")
    deadline = time.time() + wait
    while time.time() < deadline:
        reply = reply_of(s.readline().decode(errors="replace"))
        if reply is not None:
            return reply
    return "TIMEOUT"


def capture(s: serial.Serial, seconds: float):
    """Read for `seconds` and return (awake_us list, sleep_us list, wakes)."""
    awake, sleep, wakes = [], [], []
    buf = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        chunk = s.read(256)
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode(errors="replace")
            m = PWR_SLEEP.search(text)
            if m:
                awake.append(int(m.group(1)))
                sleep.append(int(m.group(2)))
            elif PWR_WAKE.search(text):
                wakes.append(time.time())
    return awake, sleep, wakes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="how long to listen in the steady state")
    ap.add_argument("--dbg-sec", type=int, default=None,
                    help="set the debug window first (it must end before the "
                         "device enters its light-sleep loop)")
    ap.add_argument("--reset", action="store_true",
                    help="pulse the reset line first so a debug window exists")
    ap.add_argument("--adv-ms", type=int, default=2000)
    ap.add_argument("--rot-sec", type=int, default=120)
    ap.add_argument("--ma-awake", type=float, default=None,
                    help="measured current in mA while awake")
    ap.add_argument("--ma-sleep", type=float, default=None,
                    help="measured current in mA while in light sleep")
    ap.add_argument("--json", type=Path, default=None,
                    help="write the raw numbers to this file")
    args = ap.parse_args()

    s = serial.Serial(args.port, 115200, timeout=0.5)
    try:
        # A debug window is needed to talk to the device at all, so --reset
        # brings one up before anything else.
        if args.reset:
            print("rebooting to get a console ...")
            pulse_reset(s)

        restarted = args.reset
        if args.dbg_sec is not None:
            r = send(s, f"CONFIG {args.adv_ms} {args.rot_sec} {args.dbg_sec}")
            if not r.startswith("OK CONFIG"):
                print(f"ERROR: CONFIG failed: {r}")
                return 2
            print(f"  {r}")
            restarted = True      # a dbg_sec change needs a new session

        # The config only applies to the session that starts next.
        if restarted:
            print("rebooting into the session under test ...")
            pulse_reset(s)

        dbg = args.dbg_sec
        if dbg is None:
            m = re.search(r"dbg_sec=(\d+)", send(s, "STATUS?"))
            dbg = int(m.group(1)) if m else 0

        if restarted:
            print(f"  waiting out the {dbg} s debug window ...")
            time.sleep(dbg + 5)
        else:
            print("listening in the current steady state ...")

        print(f"listening {args.seconds:.0f} s for PWR telemetry ...")
        awake, sleep, wakes = capture(s, args.seconds)
    finally:
        s.close()

    if not awake:
        print("no PWR sleep lines captured - is the device in its light-sleep "
              "loop (debug window over, paired)?")
        return 1

    med_a = statistics.median(awake)
    med_s = statistics.median(sleep)
    duty = med_a / (med_a + med_s)

    print(f"\ncycles captured : {len(awake)}  (wake marks: {len(wakes)})")
    print(f"awake  median   : {med_a / 1000:.2f} ms   "
          f"min {min(awake) / 1000:.2f} ms  max {max(awake) / 1000:.2f} ms")
    print(f"sleep  median   : {med_s / 1e6:.3f} s")
    print(f"cycle  median   : {(med_a + med_s) / 1e6:.3f} s")
    print(f"awake duty      : {duty * 100:.2f} %")

    avg_ma = None
    if args.ma_awake is not None and args.ma_sleep is not None:
        avg_ma = duty * args.ma_awake + (1 - duty) * args.ma_sleep
        print(f"avg current     : {avg_ma:.2f} mA "
              f"({args.ma_awake:.1f} mA awake / {args.ma_sleep:.1f} mA asleep)")

    if args.json:
        args.json.write_text(json.dumps({
            "port": args.port,
            "seconds": args.seconds,
            "adv_ms": args.adv_ms,
            "rot_sec": args.rot_sec,
            "dbg_sec": args.dbg_sec,
            "cycles": len(awake),
            "awake_us": awake,
            "sleep_us": sleep,
            "awake_median_us": med_a,
            "sleep_median_us": med_s,
            "awake_duty": duty,
            "avg_current_ma": avg_ma,
        }, indent=2) + "\n")
        print(f"raw data -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
