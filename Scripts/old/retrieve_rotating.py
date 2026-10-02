#!/usr/bin/env python3
"""
Retrieve Find My location reports for the rotating-key beacons in devices.json.

For each device the query window is aligned to the ESP32's actual slot
counter (last_known_slot/slot_synced_at from `pair_device.py --sync`),
so reboots or clock drift of the beacon never lose reports.

Usage:
    python3 retrieve_rotating.py [apple_id] [--device ID] [--watch SECONDS]
                                 [--back N]

    The apple_id is optional once a session exists (state/account_state.json):
    the account name is then taken from the saved authentication data.

    --back N widens the first-fetch window from the default
    MAX_BACKTRACK_SLOTS (30) to N slots into the past; use it when the
    archive is still empty and you want the whole chain since pairing.
"""
import asyncio
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from findmy import AsyncAppleAccount, LocalAnisetteProvider
from findmy.accessory import FindMyAccessory
from findmy.errors import EmptyResponseError
from findmy.reports import LoginState


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
STATE_DIR = SCRIPTS_DIR / "state"
KEYS_DIR = ROOT / "KeyGen" / "keys"
DEVICES_JSON = STATE_DIR / "devices.json"
REPORTS_JSON = STATE_DIR / "reports.json"
SESSION_FILE = STATE_DIR / "account_state.json"

# never query more than this many slots into the past (1 h at 120 s slots)
MAX_BACKTRACK_SLOTS = 30


class BeaconAccessory(FindMyAccessory):
    """FindMyAccessory with configurable rollover interval and primary keys
    only (we do not advertise secondary keys)."""

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


def load_devices() -> list[dict]:
    if not DEVICES_JSON.exists():
        print(f"{DEVICES_JSON} not found - run pair_device.py first")
        sys.exit(1)
    return json.loads(DEVICES_JSON.read_text())["devices"]


def make_accessory(dev: dict) -> BeaconAccessory:
    paired_at = datetime.fromisoformat(dev["paired_at"])
    alignment_date = paired_at
    alignment_index = 0
    if dev.get("slot_synced_at") and dev.get("last_known_slot") is not None:
        alignment_date = datetime.fromisoformat(dev["slot_synced_at"])
        alignment_index = int(dev["last_known_slot"])
    return BeaconAccessory(
        slot_seconds=int(dev.get("slot_seconds", 120)),
        master_key=base64.b64decode(dev["master_key"]),
        skn=base64.b64decode(dev["skn"]),
        paired_at=paired_at,
        alignment_date=alignment_date,
        alignment_index=alignment_index,
        name=dev["id"],
        identifier=dev["id"],
    )


async def login(apple_id: str | None):
    """Restore the saved session if possible (the Apple ID then comes from
    the session itself); only ask for credentials on a fresh login."""
    if SESSION_FILE.exists():
        try:
            account = AsyncAppleAccount.from_json(str(SESSION_FILE))
            if account.login_state == LoginState.LOGGED_IN:
                print(f"Restored saved session for {account.account_name} (no 2FA needed)")
                return account
            print(f"Saved session state: {account.login_state}, re-login required")
        except Exception as e:
            print(f"Could not restore session: {e}")

    if not apple_id:
        apple_id = input("Apple ID: ").strip()
    print(f"Logging in as {apple_id}...")
    print("Apple ID password: ", end="", flush=True)
    password = sys.stdin.readline().strip()

    account = AsyncAppleAccount(LocalAnisetteProvider())
    state = await account.login(apple_id, password)

    if state == LoginState.REQUIRE_2FA:
        methods = await account.get_2fa_methods()
        for i, m in enumerate(methods):
            kind = f"SMS to {m.phone_number}" if hasattr(m, "phone_number") else "trusted device push"
            print(f"  {i}: {kind}")
        method = methods[0]
        await method.request()
        print("2FA code sent - check your device.")
        state = await method.submit(input("Enter 2FA code: ").strip())

    if state != LoginState.LOGGED_IN:
        print(f"Login failed (state: {state})")
        await account.close()
        return None

    print("Login successful!")
    account.to_json(str(SESSION_FILE))
    SESSION_FILE.chmod(0o600)
    print(f"Session saved to {SESSION_FILE}")
    return account


# results archive (reports.json)


def load_results() -> dict:
    if REPORTS_JSON.exists():
        return json.loads(REPORTS_JSON.read_text())
    return {}


def save_results(results: dict) -> None:
    REPORTS_JSON.write_text(json.dumps(results, indent=2) + "\n")
    try:
        REPORTS_JSON.chmod(0o600)
    except OSError:
        pass


def report_to_dict(r, slot: int, ktype) -> dict:
    return {
        "slot": slot,
        "type": ktype.name.lower(),
        "time": r.timestamp.isoformat(),
        "latitude": r.latitude,
        "longitude": r.longitude,
        "accuracy_m": r.horizontal_accuracy,
        "confidence": r.confidence,
        "status": r.status,
    }


def report_key(d: dict) -> tuple:
    return (d["slot"], d["time"], round(d["latitude"], 6), round(d["longitude"], 6))


async def fetch_new_reports(account, acc: BeaconAccessory, dev_id: str, results: dict,
                            backtrack: int = MAX_BACKTRACK_SLOTS) -> list:
    """
    Incremental fetch, one request per slot (Apple caps batched responses).

    Starts at the newest slot that already has archived reports (more may
    still be uploaded for it until the next slot reports), or at most
    `backtrack` slots into the past for a first fetch. Returns the list of
    newly discovered reports; the archive on disk is updated per slot.
    """
    now = datetime.now(timezone.utc)
    max_i = acc.get_max_index(now)

    state = results.setdefault(dev_id, {"reports": [], "last_fetched_slot": None})
    known = {report_key(d) for d in state["reports"]}

    slots_with_reports = [d["slot"] for d in state["reports"]]
    floor = max(0, max_i - backtrack)
    if slots_with_reports:
        # re-fetch from the newest slot that has reports (uploads may still
        # arrive for it) - older slots are considered complete
        start = max(max(slots_with_reports), floor)
    else:
        start = floor

    print(f"query window: slots {start}..{max_i} "
          f"({max_i - start + 1} request(s); archive has "
          f"{len(state['reports'])} report(s) so far)")

    fresh = []

    for slot in range(max_i, start - 1, -1):
        keys = {}  # b64 -> key
        for key in acc.keys_at(slot):
            keys.setdefault(key.hashed_adv_key_b64, key)

        try:
            raw = await account.fetch_raw_reports([(list(keys.keys()), [])])
        except EmptyResponseError:
            raw = []

        added = 0
        for r in raw:
            key = keys.get(base64.b64encode(r.hashed_adv_key_bytes).decode())
            if key is None:
                continue
            r.decrypt(key)
            d = report_to_dict(r, slot, key.key_type)
            k = report_key(d)
            if k not in known:
                known.add(k)
                state["reports"].append(d)
                fresh.append(d)
                added += 1

        if raw:
            print(f"  slot {slot}: {len(raw)} report(s) on server, {added} new")

        state["last_fetched_slot"] = slot
        save_results(results)
        await asyncio.sleep(0.2)

    state["reports"].sort(key=lambda d: d["time"])
    save_results(results)
    return fresh


async def run(apple_id: str | None, dev_filter: str | None, watch: int,
              backtrack: int = MAX_BACKTRACK_SLOTS):
    devices = load_devices()
    if dev_filter:
        devices = [d for d in devices if d["id"] == dev_filter]
        if not devices:
            print(f"no device '{dev_filter}' in devices.json")
            return
    accessories = {d["id"]: make_accessory(d) for d in devices}

    for dev_id, acc in accessories.items():
        print(f"\n# device '{dev_id}' (paired {acc.paired_at}, "
              f"slot {acc._alignment_index} at last sync, "
              f"{int(acc.interval.total_seconds())}s slots)")

    account = await login(apple_id)
    if account is None:
        return

    results = load_results()

    try:
        attempt = 0
        while True:
            attempt += 1
            print(f"\nFetching location reports (attempt {attempt})...")
            fresh_total = 0
            for dev_id, acc in accessories.items():
                print(f"\n--- {dev_id}")
                fresh = await fetch_new_reports(account, acc, dev_id, results, backtrack)
                fresh_total += len(fresh)
                for i, d in enumerate(fresh):
                    print(f"  NEW #{i + 1}  [slot {d['slot']} ({d['type']})]")
                    print(f"    Time:       {d['time']}")
                    print(f"    Latitude:   {d['latitude']}")
                    print(f"    Longitude:  {d['longitude']}")
                    print(f"    Accuracy:   {d['accuracy_m']} m")
                    print(f"    Confidence: {d['confidence']}")
                archive = results[dev_id]["reports"]
                print(f"  archive: {len(archive)} report(s) in {REPORTS_JSON.name}")
            print(f"\nNew this run: {fresh_total} report(s)")

            if fresh_total or not watch:
                if not fresh_total:
                    print("No new reports - keep the beacon near a locked iPhone.")
                return

            print(f"retrying in {watch}s (Ctrl-C to stop)")
            await asyncio.sleep(watch)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        save_results(results)
        try:
            await account.close()
        except Exception:
            pass


def main():
    args = sys.argv[1:]
    watch = 0
    if "--watch" in args:
        i = args.index("--watch")
        try:
            watch = int(args[i + 1])
            del args[i:i + 2]
        except (IndexError, ValueError):
            print("--watch requires seconds")
            sys.exit(1)

    dev = None
    if "--device" in args:
        i = args.index("--device")
        try:
            dev = args[i + 1]
            del args[i:i + 2]
        except IndexError:
            print("--device requires an id")
            sys.exit(1)

    back = MAX_BACKTRACK_SLOTS
    if "--back" in args:
        i = args.index("--back")
        try:
            back = int(args[i + 1])
            del args[i:i + 2]
        except (IndexError, ValueError):
            print("--back requires a slot count")
            sys.exit(1)

    apple_id = args[0] if args else None
    asyncio.run(run(apple_id, dev, watch, back))


if __name__ == "__main__":
    main()
