#!/usr/bin/env python3
"""
CLI monitor for the Find My beacon results archive (reports.json).

Redraws a colored dashboard every few seconds:

  * headline with device info and archive size
  * FRESHNESS metric: time elapsed since the newest report
  * table of the latest reports (slot, time, age, position, accuracy)
  * per-slot report counts of the recent slots as a bar strip

Usage:
    python3 monitor.py [--interval SECONDS] [--device ID] [--once]
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
STATE_DIR = SCRIPTS_DIR / "state"
KEYS_DIR = ROOT / "KeyGen" / "keys"
DEVICES_JSON = STATE_DIR / "devices.json"
REPORTS_JSON = STATE_DIR / "reports.json"

# ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[97m"

FRESH_OK = 45 * 60        # green up to 45 min (upload delay is ~26 min median)
FRESH_WARN = 3 * 3600     # yellow up to 3 h

BAR_CHARS = " ▁▂▃▄▅▆▇█"


def fmt_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    if s < 86400:
        h, m = divmod(s, 3600)
        return f"{h}h{m // 60:02d}min"
    d, h = divmod(s, 86400)
    return f"{d}d{h}h"


def freshness_color(age: float) -> str:
    if age <= FRESH_OK:
        return GREEN
    if age <= FRESH_WARN:
        return YELLOW
    return RED


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def current_slot(dev: dict) -> int:
    """Expected slot right now, from the last serial sync."""
    try:
        synced = parse_time(dev["slot_synced_at"])
        elapsed = datetime.now(timezone.utc) - synced
        return int(dev["last_known_slot"] + elapsed.total_seconds()
                   // int(dev.get("slot_seconds", 120)))
    except (KeyError, ValueError):
        return -1


def render(device_id: str | None) -> str:
    now = datetime.now(timezone.utc)
    devices = {d["id"]: d for d in load_json(DEVICES_JSON).get("devices", [])}
    results = load_json(REPORTS_JSON)

    if not results:
        body = f"{DIM}no archive yet - run retrieve_rotating.py first{RESET}"
        ids = []
    else:
        ids = [device_id] if device_id and device_id in results else list(results)
        if device_id and device_id not in results:
            body = f"{RED}device '{device_id}' not in reports.json{RESET}"
            ids = []

    out = []
    width = 74
    out.append(f"{CYAN}{BOLD}╔{'═' * (width - 2)}╗{RESET}")
    title = " ESP32 FIND MY - BEACON MONITOR "
    pad = width - 2 - len(title)
    out.append(f"{CYAN}{BOLD}║{title}{' ' * pad}║{RESET}")
    out.append(f"{CYAN}{BOLD}╚{'═' * (width - 2)}╝{RESET}")

    for i, dev_id in enumerate(ids):
        state = results[dev_id]
        reports = state.get("reports", [])
        dev = devices.get(dev_id, {})

        if i:
            out.append("")

        slot_secs = dev.get("slot_seconds", "?")
        out.append(f"{WHITE}{BOLD}▍ {dev_id}{RESET}  "
                   f"{DIM}slots: {slot_secs}s  paired: {dev.get('paired_at', '?')}{RESET}")

        if not reports:
            out.append(f"  {DIM}no reports yet{RESET}")
            continue

        newest = max(parse_time(r["time"]) for r in reports)
        age = (now - newest).total_seconds()
        col = freshness_color(age)
        out.append(f"  {BOLD}FRESHNESS{RESET}  "
                   f"{col}{BOLD}{fmt_age(age)}{RESET}{col} since last report{RESET}  "
                   f"{DIM}(report from {newest.astimezone().strftime('%H:%M:%S')}, "
                   f"slot {max(r['slot'] for r in reports)}){RESET}")

        # freshness bar
        bar_w = 40
        filled = int(bar_w * min(1.0, age / FRESH_WARN))
        out.append(f"  {col}{'█' * filled}{'░' * (bar_w - filled)}{RESET}")

        # latest reports table
        out.append(f"  {MAGENTA}{BOLD}── latest reports {'─' * 36}{RESET}")
        out.append(f"  {DIM}{'slot':>5}  {'time (local)':<8}  {'age':>8}  "
                   f"{'lat':>10}  {'lon':>10}  {'acc':>6}{RESET}")
        for r in sorted(reports, key=lambda x: x["time"])[-8:]:
            t = parse_time(r["time"]).astimezone()
            r_age = fmt_age((now - parse_time(r["time"])).total_seconds())
            out.append(f"  {r['slot']:>5}  {t.strftime('%H:%M:%S'):<8}  {r_age:>8}  "
                       f"{r['latitude']:>10.5f}  {r['longitude']:>10.5f}  "
                       f"{str(r['accuracy_m']) + 'm':>6}")

        # per-slot coverage strip of recent slots
        cur = current_slot(dev)
        if cur >= 0:
            counts = {}
            for r in reports:
                counts[r["slot"]] = counts.get(r["slot"], 0) + 1
            lo = max(0, cur - 39)
            strip = "".join(BAR_CHARS[min(8, counts.get(s, 0))] for s in range(lo, cur + 1))
            out.append(f"  {MAGENTA}{BOLD}── slot coverage (last {cur - lo + 1}) {'─' * 22}{RESET}")
            out.append(f"  {GREEN}{strip}{RESET}")
            label_lo = f"slot {lo}"
            label_hi = f"now ({cur})"
            gap = max(1, len(strip) - len(label_lo) - len(label_hi))
            out.append(f"  {DIM}{label_lo}{' ' * gap}{label_hi}{RESET}")

        out.append(f"  {DIM}archive: {len(reports)} report(s), "
                   f"{len({r['slot'] for r in reports})} slots with data, "
                   f"last fetched slot {state.get('last_fetched_slot', '?')}{RESET}")

    if not ids and results is not None and "body" in locals():
        out.append(body)

    out.append("")
    out.append(f"{DIM}refreshing every few seconds - Ctrl-C to quit{RESET}")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--device")
    p.add_argument("--once", action="store_true", help="render once and exit")
    args = p.parse_args()

    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(render(args.device))
            sys.stdout.flush()
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write(RESET + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
