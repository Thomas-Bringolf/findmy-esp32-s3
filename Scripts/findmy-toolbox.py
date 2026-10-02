#!/usr/bin/env python3
"""
findmy-toolbox.py - one tool for the ESP32-S3 Find My beacon.

Usage:
    python3 findmy-toolbox.py                 interactive menu
    python3 findmy-toolbox.py <command> [...] run one command
    python3 findmy-toolbox.py help            command overview

Commands:
    pair       pair a beacon over UART (keys + PIN, stores devices.json)
    sync       sync the slot counter over USB
    sync-ble   sync the slot counter from the BLE advertisement
    devices    list paired devices
    test       console protocol test suite (resets the device)
    power      awake/sleep duty cycle from the PWR telemetry
    retrieve   fetch location reports  (--bg/--status/--follow/--stop)
    watch      retrieve and keep polling
    monitor    live dashboard
    verify     check the advertisement on air
    scan       raw BLE scan for Find My packets
    pin        set a new console PIN
    unlock     unlock the console (device left unlocked)
    lock       lock the console (device left locked)
    log        show or follow state/toolbox.log
    help       show this overview

State lives in state/ (devices.json, reports.json, account_state.json,
toolbox.log, retrieve.lock). Logging goes to stderr and state/toolbox.log.
"""
import argparse
import asyncio
import base64
import fcntl
import getpass
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import statistics
import string
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
STATE_DIR = SCRIPTS_DIR / "state"
DEVICES_JSON = STATE_DIR / "devices.json"
REPORTS_JSON = STATE_DIR / "reports.json"
SESSION_FILE = STATE_DIR / "account_state.json"
LOG_FILE = STATE_DIR / "toolbox.log"
LOCK_FILE = STATE_DIR / "retrieve.lock"

DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_ID = "esp32-s3-test"
DEFAULT_PIN = "00000000"
DEFAULT_WATCH = 120
DEFAULT_SCAN_SECS = 15
DEFAULT_SECS = 60
DEFAULT_DEVICE = ""
MAX_BACKTRACK_SLOTS = 30
RETRIEVE_SLEEP_S = 90
PIN_LEN = 8

MARKERS = ("PONG", "OK ", "ERR", "SLOT ", "KEY ", "STAT ", "STATUS ", "LOCKED")
LOG_PREFIX = re.compile(r"^[IVDEW] \(\d+\) [^:]+: ")

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

FRESH_OK = 45 * 60
FRESH_WARN = 3 * 3600
BAR_CHARS = " ▁▂▃▄▅▆▇█"

CHANNEL_COLOR = {
    "MAIN": WHITE, "UI": CYAN, "PAIR": GREEN, "SYNC": BLUE,
    "SYNC-BLE": BLUE, "KEYGEN": YELLOW, "RETRIEVE": MAGENTA, "TEST": WHITE,
    "POWER": YELLOW, "VERIFY": GREEN, "SCAN": BLUE, "MONITOR": CYAN,
    "WORKER": MAGENTA, "ERROR": RED,
}

LOG_COLOR = sys.stderr.isatty() and "NO_COLOR" not in os.environ
UI_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(color: str, text: str, ui: bool = False) -> str:
    if not (UI_COLOR if ui else LOG_COLOR):
        return text
    return f"{color}{text}{RESET}"


def ui(text: str, color: str = "") -> None:
    print(paint(color, text, ui=True) if color else text)


def fresh_pin() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(PIN_LEN))


def valid_pin(pin: str | None) -> bool:
    return bool(pin) and len(pin) == PIN_LEN and pin.isdigit()


REDACTIONS = [
    (re.compile(r'("master_key"\s*:\s*")[^"]+'), r"\1***"),
    (re.compile(r'("skn"\s*:\s*")[^"]+'), r"\1***"),
    (re.compile(r"(?i)\b(master_key|skn|password|passwd)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"\b(UNLOCK|LOCK|PIN) \d{8}\b"), r"\1 ****"),
    (re.compile(r"(KEYS )\S+ \S+"), r"\1*** ***"),
    (re.compile(r"(?i)\b(2fa code[: ]+)\d{4,8}\b"), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class ChannelFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "channel"):
            record.channel = "MAIN"
        return True


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        channel = getattr(record, "channel", "MAIN")
        message = redact(record.getMessage())
        color = CHANNEL_COLOR.get(channel, WHITE)
        body = paint(color, f"[{channel:<9}]")
        if record.levelno >= logging.ERROR:
            message = paint(RED, message)
        return f"{stamp} {body} {paint(DIM, '->')} {message}"


class FileFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        channel = getattr(record, "channel", "MAIN")
        return f"{stamp} [{channel:<9}] -> {redact(record.getMessage())}"


_logger = logging.getLogger("toolbox")


def setup_logging(verbose: bool = False) -> None:
    _logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    _logger.propagate = False
    _logger.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    console.addFilter(ChannelFilter())
    _logger.addHandler(console)

    STATE_DIR.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(FileFormatter())
    file_handler.addFilter(ChannelFilter())
    _logger.addHandler(file_handler)
    try:
        LOG_FILE.chmod(0o600)
    except OSError:
        pass


def log(channel: str, message: str, level: int = logging.INFO) -> None:
    _logger.log(level, message, extra={"channel": channel})


def log_error(channel: str, message: str) -> None:
    log(channel, message, logging.ERROR)


class UserError(RuntimeError):
    pass


def prompt(label: str, default: str | None = None) -> str:
    if not sys.stdin.isatty():
        if default is None:
            raise UserError(f"{label} required but stdin is not a terminal")
        return default
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            raw = input(f"{label}{suffix}: ").strip()
        except EOFError:
            raw = ""
        if raw:
            return raw
        if default is not None:
            return default
        print(paint(YELLOW, "  a value is required", ui=True))


def prompt_valid_pin(label: str, default: str | None = None) -> str:
    while True:
        raw = prompt(label, default)
        if valid_pin(raw):
            return raw
        print(paint(RED, f"  {PIN_LEN} digits, please", ui=True))


def load_devices() -> dict:
    if DEVICES_JSON.exists():
        try:
            return json.loads(DEVICES_JSON.read_text())
        except json.JSONDecodeError as exc:
            raise UserError(f"{DEVICES_JSON} is not valid JSON: {exc}") from exc
    return {"devices": []}


def save_devices(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = DEVICES_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(DEVICES_JSON)


def find_device(data: dict, dev_id: str) -> dict | None:
    for device in data["devices"]:
        if device["id"] == dev_id:
            return device
    return None


def pick_device(data: dict, want: str | None, *, required: bool = True) -> str | None:
    devices = data["devices"]
    if not devices:
        if required:
            raise UserError(f"no devices in {DEVICES_JSON} - run 'pair' first")
        return None
    if want:
        if find_device(data, want) is None:
            raise UserError(f"device '{want}' not found in {DEVICES_JSON}")
        return want
    if len(devices) == 1:
        return devices[0]["id"]
    if not sys.stdin.isatty():
        return devices[0]["id"]
    print("devices:")
    for i, device in enumerate(devices, 1):
        print(f"  {i}) {device['id']}  ({device.get('port', '?')})")
    while True:
        raw = prompt("device", devices[0]["id"])
        if raw.isdigit() and 1 <= int(raw) <= len(devices):
            return devices[int(raw) - 1]["id"]
        for device in devices:
            if device["id"] == raw:
                return raw
        print(paint(RED, "  unknown device", ui=True))


def store_pin(dev_id: str, pin: str) -> None:
    data = load_devices()
    device = find_device(data, dev_id)
    if device is None:
        return
    if device.get("pin") == pin:
        return
    device["pin"] = pin
    save_devices(data)
    log("MAIN", f"PIN for '{dev_id}' updated in {DEVICES_JSON.name}")


def device_pin(device: dict) -> str:
    pin = device.get("pin")
    return pin if valid_pin(pin) else DEFAULT_PIN


def save_entry(entry: dict) -> None:
    data = load_devices()
    data["devices"] = [d for d in data["devices"] if d["id"] != entry["id"]]
    data["devices"].append(entry)
    save_devices(data)


def fresh_device_entry(dev_id: str, port: str, master: bytes, skn: bytes,
                       paired_at: datetime, pin: str, adv_ms: int,
                       rot_sec: int, dbg_sec: int) -> dict:
    b64 = lambda x: base64.b64encode(x).decode()
    return {
        "id": dev_id,
        "port": port,
        "paired_at": paired_at.isoformat(),
        "master_key": b64(master),
        "skn": b64(skn),
        "slot_seconds": 120,
        "last_known_slot": 0,
        "slot_synced_at": paired_at.isoformat(),
        "adv_ms": adv_ms,
        "rot_sec": rot_sec,
        "dbg_sec": dbg_sec,
        "pin": pin,
    }


def accessory_class():
    from findmy.accessory import FindMyAccessory

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

    return RotatingAccessory


def make_accessory(dev: dict):
    from datetime import datetime as dt

    paired_at = dt.fromisoformat(dev["paired_at"])
    alignment_date = paired_at
    alignment_index = 0
    if dev.get("slot_synced_at") and dev.get("last_known_slot") is not None:
        alignment_date = dt.fromisoformat(dev["slot_synced_at"])
        alignment_index = int(dev["last_known_slot"])
    cls = accessory_class()
    return cls(
        slot_seconds=int(dev.get("slot_seconds", 120)),
        master_key=base64.b64decode(dev["master_key"]),
        skn=base64.b64decode(dev["skn"]),
        paired_at=paired_at,
        alignment_date=alignment_date,
        alignment_index=alignment_index,
        name=dev["id"],
        identifier=dev["id"],
    )


def derive_expected(master: bytes, skn: bytes, paired_at: datetime) -> str:
    from findmy.accessory import FindMyAccessory

    acc = FindMyAccessory(
        master_key=master, skn=skn,
        sks=b"\x00" * 32,
        paired_at=paired_at, name="t", identifier="t",
    )
    return acc._primary_key_at(0).adv_key_bytes.hex()


def beacon_mac(key) -> str:
    b = key.adv_key_bytes
    addr = (b[0] | 0xC0, b[1], b[2], b[3], b[4], (b[5] & 0x3F) | 0xC0)
    return ":".join(f"{x:02X}" for x in reversed(addr))


OF_TYPE = 0x12
OF_LEN = 0x19
APPLE_MFR = 0x004C


def payload_matches(mfr: bytes, key) -> bool:
    if mfr is None or len(mfr) < 4:
        return False
    if mfr[0] != OF_TYPE or mfr[1] != OF_LEN:
        return False
    expected = key.of_data(status=0, hint=0)
    if len(mfr) != len(expected):
        return False
    return mfr[:2] == expected[:2] and mfr[3:] == expected[3:]


def status_decode(status_byte: int) -> str:
    bits = []
    bits.append("UNLOCKED" if status_byte & 0x01 else "locked")
    bits.append("CONFIG" if status_byte & 0x02 else "sleep-cycle")
    return f"0x{status_byte:02x} ({'+'.join(bits)})"


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


def fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


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


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def reply_of(raw: str) -> str | None:
    text = LOG_PREFIX.sub("", raw.strip())
    for marker in MARKERS:
        if text.startswith(marker):
            return text
    return None


class BeaconError(RuntimeError):
    pass


class Beacon:
    """One UART session: opens the port, waits for the console, unlocks it
    on the way in and locks it again on the way out."""

    def __init__(self, port: str, pin: str | None = None, *,
                 dev_id: str | None = None, auto_unlock: bool = True,
                 auto_lock: bool = True, channel: str = "PAIR",
                 timeout: float = 6.0, pin_from_flag: bool = False):
        import serial

        self.port = port
        self.pin = pin
        self.dev_id = dev_id
        self.auto_unlock = auto_unlock
        self.auto_lock = auto_lock
        self.channel = channel
        self.timeout = timeout
        self.pin_from_flag = pin_from_flag
        self.unlocked = False
        self.s = serial.Serial(port, 115200, timeout=0.5)

    def __enter__(self) -> "Beacon":
        self.after_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def after_open(self, reset: bool = False, ready_timeout: float = 20.0) -> None:
        time.sleep(0.3)
        self.s.reset_input_buffer()
        if reset:
            self.pulse_reset()
        if not self.wait_ready(ready_timeout):
            raise BeaconError(
                f"device on {self.port} is not answering - asleep or not in "
                f"its console? (pass --reset, press reset, or use 'test' to "
                f"force one)")
        if self.auto_unlock:
            self.unlock()

    def close(self) -> None:
        if self.s is None:
            return
        try:
            if self.auto_lock and self.unlocked:
                try:
                    self.lock()
                except BeaconError as exc:
                    log_error(self.channel,
                              f"could not lock {self.port} on close: {exc}")
        finally:
            try:
                self.s.close()
            except Exception:
                pass
            self.s = None
            self.unlocked = False

    def pulse_reset(self) -> None:
        self.s.dtr = False
        self.s.rts = True
        time.sleep(0.1)
        self.s.rts = False
        time.sleep(1.5)

    def cmd(self, line: str, wait: float | None = None) -> str:
        wait = self.timeout if wait is None else wait
        prev_timeout = self.s.timeout
        self.s.timeout = 0.05
        try:
            while self.s.readline():
                pass
        finally:
            self.s.timeout = prev_timeout
        self.s.write(line.encode() + b"\r\n")
        log(self.channel, f"> {line}", logging.DEBUG)
        deadline = time.time() + wait
        while time.time() < deadline:
            raw = self.s.readline().decode(errors="replace")
            if "Guru Meditation" in raw or raw.startswith("Backtrace:"):
                log(self.channel, f"device crashed: {raw.strip()}",
                    logging.ERROR)
            reply = reply_of(raw)
            if reply is not None:
                log(self.channel, f"< {reply}", logging.DEBUG)
                return reply
        log(self.channel,
            f"< (no reply to {line.strip() or '<empty>'} in {wait:g}s)",
            logging.DEBUG)
        return "TIMEOUT"

    def alive(self) -> bool:
        reply = self.cmd("PING", wait=1.5)
        return reply.startswith("PONG") or reply == "LOCKED"

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.alive():
                return True
            time.sleep(0.4)
        return False

    def unlock(self, pin: str | None = None) -> str:
        pin = pin or self.pin or DEFAULT_PIN
        first_pin = pin
        can_prompt = (not self.pin_from_flag) and sys.stdin.isatty()
        attempts = 0
        while True:
            attempts += 1
            reply = self.cmd(f"UNLOCK {pin}")
            if reply.startswith("OK UNLOCK"):
                self.pin = pin
                self.unlocked = True
                log(self.channel, f"console unlocked on {self.port}")
                if self.dev_id and pin != first_pin:
                    store_pin(self.dev_id, pin)
                return pin
            if reply.startswith("ERR LOCK"):
                raise BeaconError(
                    f"UNLOCK refused, lockout active: {reply} - wait it out")
            if reply.startswith("ERR PIN"):
                log_error(self.channel,
                          f"wrong PIN on {self.port} (attempt {attempts})")
                if attempts == 1 and can_prompt:
                    pin = prompt_valid_pin(
                        "PIN (a wiped device still has 00000000)", DEFAULT_PIN)
                    continue
                if self.pin_from_flag:
                    raise BeaconError(f"wrong PIN (from --pin): {reply}")
                raise BeaconError(
                    f"wrong PIN for {self.port} and no terminal to ask on")
            if reply == "TIMEOUT":
                raise BeaconError(f"no answer to UNLOCK on {self.port}")
            raise BeaconError(f"unexpected reply to UNLOCK: {reply}")

    def lock(self, pin: str | None = None) -> None:
        pin = pin or self.pin or DEFAULT_PIN
        reply = self.cmd(f"LOCK {pin}")
        if reply.startswith("OK LOCK"):
            self.unlocked = False
            log(self.channel, f"console locked on {self.port}")
            return
        if reply == "LOCKED":
            self.unlocked = False
            log(self.channel, f"{self.port} is already locked")
            return
        if reply.startswith("ERR PIN"):
            raise BeaconError(f"wrong PIN for LOCK: {self.port} left UNLOCKED")
        raise BeaconError(f"unexpected reply to LOCK: {reply}")


def resolve_port(args_port: str | None, device: dict | None) -> str:
    if args_port:
        return args_port
    if device and device.get("port"):
        return device["port"]
    if sys.stdin.isatty():
        return prompt("port", DEFAULT_PORT)
    return DEFAULT_PORT


def resolve_device_id(data: dict, want: str | None) -> str:
    if want:
        return want
    if data["devices"]:
        return pick_device(data, None) or DEFAULT_ID
    if sys.stdin.isatty():
        return prompt("device id", DEFAULT_ID)
    return DEFAULT_ID


def unlock_pin_for(device: dict | None, flag: str | None) -> str:
    if flag:
        return flag
    if device:
        return device_pin(device)
    return DEFAULT_PIN


def cmd_devices(args) -> int:
    data = load_devices()
    devices = data["devices"]
    if not devices:
        ui(f"no devices in {DEVICES_JSON}")
        return 0
    header = (f"{'id':<20} {'port':<14} {'slot':>5} {'adv':>6} {'rot':>6} "
              f"{'dbg':>5}  {'paired':<20} pin")
    ui(header, MAGENTA + BOLD)
    for d in devices:
        ui(f"{d['id']:<20} {d.get('port', '?'):<14} "
           f"{str(d.get('last_known_slot', '?')):>5} "
           f"{str(d.get('adv_ms', '?')):>6} {str(d.get('rot_sec', '?')):>6} "
           f"{str(d.get('dbg_sec', '?')):>5}  {d.get('paired_at', '?'):<20} "
           f"{device_pin(d)}")
    log("MAIN", f"{len(devices)} device(s) in {DEVICES_JSON.name}")
    return 0


def cmd_pair(args) -> int:
    channel = "PAIR"
    data = load_devices()
    dev_id = resolve_device_id(data, args.id)
    existing = find_device(data, dev_id)
    if existing and not args.force:
        log_error(channel, f"'{dev_id}' is already in {DEVICES_JSON.name} "
                           f"(use --force to re-pair)")
        return 1

    port = resolve_port(args.port, existing)
    pin = unlock_pin_for(existing, args.pin)
    adv_ms = args.adv_ms or 2000
    rot_sec = args.rot_sec or 120
    dbg_sec = args.dbg_sec if args.dbg_sec is not None else 600

    log(channel, f"pairing '{dev_id}' on {port}")
    master = secrets.token_bytes(28)
    skn = secrets.token_bytes(32)
    b64 = lambda x: base64.b64encode(x).decode()

    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        reply = beacon.cmd(
            f"KEYS {b64(master)} {b64(skn)} {adv_ms} {rot_sec} {dbg_sec}",
            wait=8)
        if not reply.startswith("OK KEYS"):
            raise UserError(f"device rejected keys: {reply}")

        slot = beacon.cmd("SLOT?")
        key = beacon.cmd("KEY?", wait=8)
        if not slot.startswith("SLOT 0") or not key.startswith("KEY "):
            raise UserError(f"unexpected state after KEYS: {slot} / {key}")

        paired_at = datetime.now(timezone.utc)
        expected = derive_expected(master, skn, paired_at)
        got = key.split()[1]
        if got != expected:
            raise UserError(f"key mismatch device={got} findmy={expected}")

        if args.debug is not None:
            beacon.cmd(f"DEBUG {1 if args.debug else 0}")
        beacon.cmd("STATUS?")

        new_pin = args.new_pin or fresh_pin()
        if not valid_pin(new_pin):
            raise UserError(f"--new-pin must be {PIN_LEN} digits")
        reply = beacon.cmd(f"PIN {new_pin}")
        if not reply.startswith("OK PIN"):
            raise UserError(f"PIN change refused: {reply}")
        beacon.pin = new_pin

        save_entry(fresh_device_entry(dev_id, port, master, skn, paired_at,
                                      new_pin, adv_ms, rot_sec, dbg_sec))
    finally:
        beacon.close()

    log(channel, f"paired '{dev_id}' on {port}")
    ui(f"\npaired '{dev_id}' on {port}")
    ui(f"  slot 0 X : {got}")
    ui(f"  timings  : adv_ms={adv_ms} rot_sec={rot_sec} dbg_sec={dbg_sec}")
    ui(f"  PIN      : {paint(GREEN, new_pin, ui=True)}")
    ui(f"  stored   : {DEVICES_JSON}")
    return 0


def cmd_sync(args) -> int:
    channel = "SYNC"
    data = load_devices()
    dev_id = pick_device(data, args.id)
    device = find_device(data, dev_id)
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    log(channel, f"syncing '{dev_id}' on {port}")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        slot = beacon.cmd("SLOT?")
        if not slot.startswith("SLOT "):
            raise UserError(slot)
        slot_n = int(slot.split()[1])

        master = base64.b64decode(device["master_key"])
        skn = base64.b64decode(device["skn"])
        paired_at = datetime.fromisoformat(device["paired_at"])
        expected = derive_expected(master, skn, paired_at)
        key = beacon.cmd("KEY?", wait=8)
        if not key.startswith("KEY ") or key.split()[1] != expected:
            got = key.split()[1] if key.startswith("KEY ") else key
            raise UserError(f"key mismatch at slot {slot_n}: {got} vs {expected}")

        now = datetime.now(timezone.utc)
        device["last_known_slot"] = slot_n
        device["slot_synced_at"] = now.isoformat()
        if device.get("port") != port:
            device["port"] = port
        save_devices(data)
    finally:
        beacon.close()

    log(channel, f"'{dev_id}' at slot {slot_n}")
    ui(f"synced '{dev_id}': slot {slot_n} at {now.isoformat()}")
    return 0


async def ble_scan(window: float) -> dict[str, dict]:
    from bleak import BleakScanner

    seen: dict[str, dict] = {}

    def cb(device, adv):
        mfr = adv.manufacturer_data.get(APPLE_MFR)
        if mfr is None or len(mfr) < 4 or mfr[0] != OF_TYPE or mfr[1] != OF_LEN:
            return
        now = time.time()
        mac = device.address.upper()
        entry = seen.setdefault(mac, {"payload": mfr.hex(), "status": mfr[2],
                                      "rssi": adv.rssi, "first": now,
                                      "last": now})
        entry["last"] = now
        entry["rssi"] = adv.rssi
        entry["status"] = mfr[2]

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(window)
    await scanner.stop()
    return seen


def cmd_sync_ble(args) -> int:
    channel = "SYNC-BLE"
    data = load_devices()
    devices = data["devices"]
    if args.device:
        devices = [d for d in devices if d["id"] == args.device]
        if not devices:
            log_error(channel, f"device '{args.device}' not in {DEVICES_JSON.name}")
            return 1
    if not devices:
        log_error(channel, f"no devices in {DEVICES_JSON.name}")
        return 1

    log(channel, f"scanning {args.window:g}s for Apple Offline Finding beacons")
    try:
        seen = asyncio.run(ble_scan(args.window))
    except PermissionError:
        log_error(channel, "BLE scan needs permissions - run with sudo or "
                           "grant capabilities to python3")
        return 1

    if not seen:
        log_error(channel, "no 0x12 advertisements captured - is the beacon "
                           "running and BLE up?")
        return 1

    now = time.time()
    ui(f"\ncaptured {len(seen)} OF beacon(s):")
    for mac, e in sorted(seen.items(), key=lambda kv: -kv[1]["rssi"]):
        ui(f"  {mac}  rssi={e['rssi']:>4}  "
           f"last_seen={datetime.fromtimestamp(e['last']):%H:%M:%S} "
           f"({fmt_dur(now - e['last'])} ago)  status={status_decode(e['status'])}")

    macs = set(seen)
    ui("")
    ok = False
    for device in devices:
        dev_id = device["id"]
        ss = int(device.get("slot_seconds", 120))
        paired_at = datetime.fromisoformat(device["paired_at"])
        acc = make_accessory({**device, "slot_synced_at": None,
                              "last_known_slot": None})

        last = int(device.get("last_known_slot") or 0)
        utc_now = datetime.now(timezone.utc)
        upper = int((utc_now - paired_at).total_seconds() // ss) + 2
        upper = max(upper, last + 96)
        if args.max_slots is not None:
            upper = min(upper, args.max_slots)

        ui(f"{dev_id}: walking chain slot 0..{upper} "
           f"(last known {last}, synced {device.get('slot_synced_at', '?')})")
        matched = False
        for ind in range(upper + 1):
            key = acc._primary_key_at(ind)
            mac = beacon_mac(key)
            if mac in macs:
                est_pair = utc_now - timedelta(seconds=ind * ss)
                drift = (est_pair - paired_at).total_seconds()
                device["last_known_slot"] = ind
                device["slot_synced_at"] = utc_now.isoformat()
                device["sync_method"] = "ble"
                ui(f"  MATCH slot {ind}  (mac {mac})", GREEN + BOLD)
                ui(f"  previous sync: slot {last}")
                ui(f"  estimated pair time: "
                   f"{est_pair.astimezone():%Y-%m-%d %H:%M:%S} "
                   f"(stored {paired_at.astimezone():%Y-%m-%d %H:%M:%S}, "
                   f"~{fmt_dur(abs(drift))} {'off' if drift > 0 else 'ahead'})")
                matched = True
                break
        if not matched:
            ui("  no match - beacon out of range, unpowered, or --max-slots "
               "too low", YELLOW)
        ok = ok or matched

    if ok:
        save_devices(data)
        log(channel, f"updated {DEVICES_JSON.name}")
        ui(f"\nupdated {DEVICES_JSON}")
        return 0
    return 1


def gen_keys() -> tuple[bytes, bytes]:
    return secrets.token_bytes(28), secrets.token_bytes(32)


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class Suite:
    def __init__(self, channel: str = "TEST"):
        self.channel = channel
        self.results: list[tuple[str, bool, str]] = []

    def step(self, msg: str) -> None:
        ui(f"\n=== {msg}", CYAN + BOLD)
        log(self.channel, msg)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, ok, detail))
        tag = paint(GREEN, "PASS", ui=True) if ok else paint(RED, "FAIL", ui=True)
        ui(f"  [{tag}] {name}" + (f"  <- {detail}" if detail and not ok else ""))
        if not ok:
            log(self.channel, f"FAIL {name} <- {detail}", logging.ERROR)
        return ok

    @property
    def failed(self) -> list[str]:
        return [name for name, ok, _ in self.results if not ok]

    def summary(self) -> int:
        failed = self.failed
        ui("")
        ui(f"{len(self.results) - len(failed)}/{len(self.results)} checks passed",
           GREEN + BOLD if not failed else RED + BOLD)
        for name in failed:
            ui(f"  FAILED: {name}", RED)
        log(self.channel,
            f"{len(self.results) - len(failed)}/{len(self.results)} checks passed"
            + (f", failures: {'; '.join(failed)}" if failed else ""))
        return 1 if failed else 0


def open_beacon(port: str, pin: str, *, unlock: bool = True,
                reset: bool = False, ready_timeout: float = 20.0,
                channel: str = "TEST", dev_id: str | None = None,
                pin_from_flag: bool = False,
                auto_lock: bool = False) -> Beacon:
    last: Exception | None = None
    for _ in range(10):
        try:
            beacon = Beacon(port, pin, dev_id=dev_id, auto_unlock=False,
                            auto_lock=auto_lock, channel=channel,
                            pin_from_flag=pin_from_flag)
        except Exception as exc:
            last = exc
            time.sleep(1.0)
            continue
        try:
            beacon.after_open(reset=reset, ready_timeout=ready_timeout)
        except Exception as exc:
            last = exc
            beacon.close()
            time.sleep(0.5)
            continue
        if unlock:
            try:
                beacon.unlock(pin)
            except BeaconError:
                beacon.close()
                raise
        return beacon
    raise BeaconError(f"cannot open {port}: {last}")


def check_key_derivation(suite: Suite, beacon: Beacon, master_b64: str,
                         skn_b64: str, name: str) -> None:
    reply = beacon.cmd("KEY?", wait=8)
    if not suite.check(name + " (KEY? answered)", reply.startswith("KEY "), reply):
        return
    paired_at = datetime.now(timezone.utc)
    expected = derive_expected(base64.b64decode(master_b64),
                               base64.b64decode(skn_b64), paired_at)
    got = reply.split()[1]
    suite.check(name + " (key matches findmy)", got == expected,
                f"device={got} expected={expected}")


def cmd_test(args) -> int:
    channel = "TEST"
    data = load_devices()
    device = find_device(data, args.id) if args.id else (
        data["devices"][0] if data["devices"] else None)
    dev_id = args.id or (device["id"] if device else DEFAULT_ID)
    port = resolve_port(args.port, device)
    pin0 = args.pin or (device_pin(device) if device else DEFAULT_PIN)
    pin_final = fresh_pin()
    suite = Suite(channel)

    mk2 = skn2 = None
    paired_at_p3 = None
    beacon: Beacon | None = None

    log(channel, f"running the console suite on {port} as '{dev_id}'")
    try:
        beacon = open_beacon(port, pin0, unlock=False,
                             reset=not args.no_reset, dev_id=dev_id,
                             pin_from_flag=args.pin is not None)

        suite.step("P0: the console boots locked")
        reply = beacon.cmd("PING")
        if reply.startswith("PONG"):
            suite.check("device starts locked", False,
                        "already unlocked, forcing LOCK")
            beacon.lock(pin0)
            reply = beacon.cmd("PING")
        suite.check("PING while locked -> LOCKED", reply == "LOCKED", reply)
        for cmd, name in (("KEY?", "KEY?"), ("WIPE", "WIPE"),
                          ("STAT?", "STAT?"), ("DEBUG 1", "DEBUG"),
                          ("CONFIG 2000 120 600", "CONFIG")):
            reply = beacon.cmd(cmd)
            suite.check(f"{name} while locked -> LOCKED", reply == "LOCKED", reply)
        reply = beacon.cmd("UNLOCK 11111111")
        suite.check("wrong PIN -> ERR PIN", reply.startswith("ERR PIN"), reply)
        try:
            beacon.unlock(pin0)
            suite.check("UNLOCK correct PIN -> OK UNLOCK", True)
            pin0 = beacon.pin
        except BeaconError as exc:
            suite.check("UNLOCK correct PIN -> OK UNLOCK", False, str(exc))
            raise

        suite.step("P1: protocol and malformed input")
        reply = beacon.cmd("PING")
        suite.check("PING -> PONG fw=3", reply.startswith("PONG fw=3"), reply)
        suite.check("STAT?", beacon.cmd("STAT?").startswith("STAT paired="),
                    beacon.cmd("STAT?"))
        suite.check("STATUS?", beacon.cmd("STATUS?").startswith("STATUS paired="),
                    beacon.cmd("STATUS?"))
        reply = beacon.cmd("FOOBAR")
        suite.check("unknown command -> ERR CMD", reply.startswith("ERR CMD"), reply)
        reply = beacon.cmd("PING with extra args")
        suite.check("extra args on PING still answered", reply.startswith("PONG"),
                    reply)
        beacon.cmd("")
        beacon.cmd("   ")
        reply = beacon.cmd("PING")
        suite.check("empty line ignored, device alive", reply.startswith("PONG"),
                    reply)
        for off in range(0, 400, 100):
            beacon.s.write(b"A" * 100)
            time.sleep(0.02)
        beacon.s.write(b"\r\n")
        time.sleep(0.5)
        reply = beacon.cmd("PING")
        suite.check("400-char line dropped, device alive", reply.startswith("PONG"),
                    reply)
        reply = beacon.cmd("KEYS")
        suite.check("KEYS without args -> ERR ARGS", reply.startswith("ERR ARGS"),
                    reply)
        for args_str, want in (
            ("", "ERR ARGS"),
            ("2000 120", "ERR ARGS"),
            ("abc 120 600", "ERR ARGS"),
            ("2000x 120 600", "ERR ARGS"),
            ("-1 120 600", "ERR ARGS"),
            ("4294967296 120 600", "ERR ARGS"),
            ("2000 120 600 9", "ERR ARGS"),
            ("+2000 120 600", "ERR ARGS"),
        ):
            reply = beacon.cmd(f"CONFIG {args_str}".strip())
            suite.check(
                f"CONFIG {args_str.strip() or '<none>'} -> {want}",
                reply.startswith(want), reply)
        reply = beacon.cmd("CONFIG  2000  120  600")
        suite.check("extra separators tolerated", reply.startswith("OK CONFIG"),
                    reply)
        reply = beacon.cmd("DEBUG 5")
        suite.check("DEBUG 5 -> ERR ARGS", reply.startswith("ERR ARGS"), reply)
        reply = beacon.cmd("DEBUG x")
        suite.check("DEBUG x -> ERR ARGS", reply.startswith("ERR ARGS"), reply)
        suite.check("DEBUG 1 -> OK DEBUG 1",
                    beacon.cmd("DEBUG 1").startswith("OK DEBUG 1"))
        suite.check("DEBUG 0 -> OK DEBUG 0",
                    beacon.cmd("DEBUG 0").startswith("OK DEBUG 0"))
        reply = beacon.cmd("CONFIG 100 120 600")
        suite.check("adv_ms below minimum clamps to 200",
                    reply.startswith("OK CONFIG adv_ms=200"), reply)
        reply = beacon.cmd("CONFIG 999999 120 600")
        suite.check("adv_ms above maximum clamps to 60000",
                    reply.startswith("OK CONFIG adv_ms=60000"), reply)
        reply = beacon.cmd("CONFIG 2000 0 600")
        suite.check("rot_sec below minimum clamps to 1", "rot_sec=1" in reply,
                    reply)
        reply = beacon.cmd("CONFIG 2000 999999 600")
        suite.check("rot_sec above maximum clamps to 86400",
                    "rot_sec=86400" in reply, reply)
        reply = beacon.cmd("CONFIG 2000 120 5")
        suite.check("dbg_sec out of range falls back to 600",
                    "dbg_sec=600" in reply, reply)
        reply = beacon.cmd("CONFIG 2000 120 600")
        suite.check("config restored to defaults",
                    reply.startswith("OK CONFIG adv_ms=2000 rot_sec=120 dbg_sec=600"),
                    reply)

        suite.step("P2: PIN rotation and lock round-trip")
        reply = beacon.cmd("PIN 12ab3456")
        suite.check("PIN with letters -> ERR ARGS", reply.startswith("ERR ARGS"),
                    reply)
        reply = beacon.cmd("PIN 1234")
        suite.check("PIN too short -> ERR ARGS", reply.startswith("ERR ARGS"), reply)
        reply = beacon.cmd(f"PIN {pin_final}")
        suite.check("PIN rotation -> OK PIN", reply.startswith("OK PIN"), reply)
        reply = beacon.cmd(f"LOCK {pin_final}")
        suite.check("LOCK -> OK LOCK", reply.startswith("OK LOCK"), reply)
        beacon.unlocked = False
        reply = beacon.cmd("PING")
        suite.check("PING after LOCK -> LOCKED", reply == "LOCKED", reply)
        reply = beacon.cmd("UNLOCK 22222222")
        suite.check("wrong PIN after rotation -> ERR PIN",
                    reply.startswith("ERR PIN"), reply)
        try:
            beacon.unlock(pin_final)
            suite.check("UNLOCK with the new PIN -> OK UNLOCK", True)
        except BeaconError as exc:
            suite.check("UNLOCK with the new PIN -> OK UNLOCK", False, str(exc))
            raise

        suite.step("P3: KEYS, then WIPE = factory reset")
        mk, skn = gen_keys()
        reply = beacon.cmd(f"KEYS {b64e(mk)} {b64e(skn)}", wait=8)
        suite.check("KEYS accepted -> OK KEYS", reply.startswith("OK KEYS"), reply)
        suite.check("SLOT? -> SLOT 0 after re-key",
                    beacon.cmd("SLOT?").startswith("SLOT 0"), beacon.cmd("SLOT?"))
        check_key_derivation(suite, beacon, b64e(mk), b64e(skn), "re-keyed")

        reply = beacon.cmd("WIPE", wait=5)
        suite.check("WIPE -> OK WIPE", reply.startswith("OK WIPE"), reply)
        beacon.close()
        beacon = None

        beacon = open_beacon(port, DEFAULT_PIN, unlock=False, ready_timeout=25,
                             dev_id=dev_id)
        reply = beacon.cmd("PING")
        suite.check("boots locked after WIPE", reply == "LOCKED", reply)
        try:
            beacon.unlock(DEFAULT_PIN)
            suite.check("factory PIN unlocks after WIPE", True)
        except BeaconError as exc:
            suite.check("factory PIN unlocks after WIPE", False, str(exc))
            raise
        reply = beacon.cmd("PING")
        suite.check("unpaired after WIPE", reply.strip().endswith("paired=0"), reply)
        suite.check("SLOT? while unpaired -> ERR UNPAIRED",
                    beacon.cmd("SLOT?").startswith("ERR UNPAIRED"),
                    beacon.cmd("SLOT?"))
        suite.check("KEY? while unpaired -> ERR UNPAIRED",
                    beacon.cmd("KEY?").startswith("ERR UNPAIRED"),
                    beacon.cmd("KEY?"))
        reply = beacon.cmd(f"PIN {pin_final}")
        suite.check("PIN while unpaired -> ERR UNPAIRED",
                    reply.startswith("ERR UNPAIRED"), reply)

        mk2, skn2 = gen_keys()
        reply = beacon.cmd(f"KEYS {b64e(mk2)} {b64e(skn2)}", wait=8)
        suite.check("KEYS after wipe -> OK KEYS", reply.startswith("OK KEYS"), reply)
        paired_at_p3 = datetime.now(timezone.utc)
        reply = beacon.cmd(f"PIN {pin_final}")
        suite.check("PIN after wipe -> OK PIN", reply.startswith("OK PIN"), reply)
        check_key_derivation(suite, beacon, b64e(mk2), b64e(skn2), "re-paired")

        suite.step("P4: countdown expires, device sleeps, reset brings it back")
        reply = beacon.cmd("CONFIG 2000 120 60")
        suite.check("short countdown requested",
                    reply.startswith("OK CONFIG"), reply)
        reply = beacon.cmd(f"LOCK {pin_final}")
        suite.check("LOCK starts the countdown", reply.startswith("OK LOCK"), reply)
        beacon.unlocked = False

        print("  ... waiting out the 60 s countdown ...")
        silent = False
        deadline = time.time() + 80
        while time.time() < deadline:
            if not beacon.alive():
                silent = True
                break
            time.sleep(2)
        suite.check("device stops answering after the countdown", silent,
                    "still answering after 80 s")

        beacon.close()
        beacon = None
        beacon = open_beacon(port, pin_final, unlock=False, reset=True,
                             ready_timeout=30, dev_id=dev_id)
        reply = beacon.cmd("PING")
        suite.check("console back after reset, still locked", reply == "LOCKED",
                    reply)
        try:
            beacon.unlock(pin_final)
            suite.check("stored PIN unlocks after the reset", True)
        except BeaconError as exc:
            suite.check("stored PIN unlocks after the reset", False, str(exc))
            raise
        reply = beacon.cmd("STATUS?")
        suite.check("countdown setting survived", "dbg_sec=60" in reply, reply)
        reply = beacon.cmd("CONFIG 2000 120 600")
        suite.check("defaults restored",
                    reply.startswith("OK CONFIG adv_ms=2000 rot_sec=120 dbg_sec=600"),
                    reply)

        suite.step("P5: UNLOCK anti-bruteforce")
        reply = beacon.cmd(f"LOCK {pin_final}")
        suite.check("locked again for the lockout test",
                    reply.startswith("OK LOCK"), reply)
        beacon.unlocked = False
        lockout = None
        for _ in range(6):
            reply = beacon.cmd("UNLOCK 33333333")
            if reply.startswith("ERR LOCK"):
                lockout = reply
                break
        suite.check("repeated wrong PINs arm a lockout", lockout is not None,
                    lockout or f"last reply: {reply}")
        if lockout is not None:
            reply = beacon.cmd(f"UNLOCK {pin_final}")
            suite.check("correct PIN refused during the lockout",
                        reply.startswith("ERR LOCK"), reply)
            wait_s = int(lockout.split()[-1]) + 1 if lockout.split()[-1].isdigit() \
                else 31
            print(f"  ... waiting {wait_s} s for the lockout to expire ...")
            time.sleep(wait_s)
            reply = beacon.cmd(f"UNLOCK {pin_final}", wait=8)
            suite.check("lockout expires -> OK UNLOCK",
                        reply.startswith("OK UNLOCK"), reply)
            if reply.startswith("OK UNLOCK"):
                beacon.unlocked = True
                beacon.pin = pin_final

        reply = beacon.cmd(f"LOCK {pin_final}")
        suite.check("device left locked", reply.startswith("OK LOCK"), reply)
        beacon.unlocked = False
    except Exception as exc:
        ui(f"\nABORT: {exc}", RED + BOLD)
        log(channel, f"suite aborted: {exc}", logging.ERROR)
        suite.results.append(("suite completed", False, str(exc)))
    finally:
        if beacon is not None:
            beacon.close()

    if mk2 and skn2 and paired_at_p3:
        save_entry(fresh_device_entry(dev_id, port, mk2, skn2, paired_at_p3,
                                      pin_final, 2000, 120, 600))
        ui(f"\nrecorded new keys for '{dev_id}' in {DEVICES_JSON.name}")
        ui(f"PIN: {paint(GREEN, pin_final, ui=True)}")

    return suite.summary()


def capture_pwr(port: str, seconds: float, serial_obj):
    import serial as serial_mod

    awake: list[int] = []
    sleeps: list[int] = []
    wakes: list[float] = []
    buf = b""
    s = serial_obj
    reopened = False
    deadline = time.time() + seconds
    while time.time() < deadline:
        chunk = b""
        if s is not None:
            try:
                chunk = s.read(256)
            except OSError:
                log("POWER", f"console read failed on {port}", logging.WARNING)
                s = None
        if not chunk and s is None:
            if not reopened:
                log("POWER", f"reopening {port}", logging.WARNING)
                reopened = True
            try:
                s = serial_mod.Serial(port, 115200, timeout=0.5)
            except Exception:
                time.sleep(0.5)
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode(errors="replace")
            match = PWR_SLEEP_RE.search(text)
            if match:
                awake.append(int(match.group(1)))
                sleeps.append(int(match.group(2)))
            elif PWR_WAKE_RE.search(text):
                wakes.append(time.time())
    return awake, sleeps, wakes


PWR_SLEEP_RE = re.compile(r"PWR sleep awake_us=(\d+) sleep_us=(\d+)")
PWR_WAKE_RE = re.compile(r"PWR wake t=(\d+)")


def cmd_power(args) -> int:
    channel = "POWER"
    data = load_devices()
    device = None
    if args.id:
        device = find_device(data, args.id)
    elif data["devices"]:
        picked = pick_device(data, None)
        device = find_device(data, picked)
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)
    dev_id = device["id"] if device else None
    dbg_sec = args.dbg_sec if args.dbg_sec is not None else 60

    log(channel, f"measuring the duty cycle on {port} "
                 f"(dbg_sec={dbg_sec}, {args.seconds:g}s of telemetry)")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel, auto_lock=False,
                    pin_from_flag=args.pin is not None)
    awake, sleeps, wakes = [], [], []
    try:
        beacon.after_open(reset=args.reset)
        reply = beacon.cmd(f"CONFIG {args.adv_ms} {args.rot_sec} {dbg_sec}")
        if not reply.startswith("OK CONFIG"):
            raise UserError(f"CONFIG failed: {reply}")
        ui(f"  {reply}")
        match = re.search(r"dbg_sec=(\d+)", reply)
        actual = int(match.group(1)) if match else dbg_sec
        if actual != dbg_sec:
            log(channel, f"device clamped dbg_sec to {actual} s "
                         f"(allowed 60..3600)", logging.WARNING)
            ui(f"  device clamped dbg_sec to {actual} s", YELLOW)
            dbg_sec = actual
        log(channel, f"countdown set to {dbg_sec} s")
        beacon.lock(pin)
        log(channel, f"waiting out the {dbg_sec} s countdown")
        ui(f"waiting out the {dbg_sec} s countdown ...")
        time.sleep(dbg_sec + 5)
        ui(f"listening {args.seconds:.0f} s for PWR telemetry ...")
        log(channel, f"capturing {args.seconds:g} s of PWR telemetry")
        awake, sleeps, wakes = capture_pwr(port, args.seconds, beacon.s)
    finally:
        beacon.close()

    if not awake:
        log_error(channel, "no PWR sleep lines captured - is the device paired "
                           "and past its countdown?")
        return 1

    med_a = statistics.median(awake)
    med_s = statistics.median(sleeps)
    duty = med_a / (med_a + med_s)

    ui(f"\ncycles captured : {len(awake)}  (wake marks: {len(wakes)})")
    ui(f"awake  median   : {med_a / 1000:.2f} ms   "
       f"min {min(awake) / 1000:.2f} ms  max {max(awake) / 1000:.2f} ms")
    ui(f"sleep  median   : {med_s / 1e6:.3f} s")
    ui(f"cycle  median   : {(med_a + med_s) / 1e6:.3f} s")
    ui(f"awake duty      : {duty * 100:.2f} %", GREEN)

    avg_ma = None
    if args.ma_awake is not None and args.ma_sleep is not None:
        avg_ma = duty * args.ma_awake + (1 - duty) * args.ma_sleep
        ui(f"avg current     : {avg_ma:.2f} mA "
           f"({args.ma_awake:.1f} mA awake / {args.ma_sleep:.1f} mA asleep)")
    ui(f"\nnote: dbg_sec is left at {dbg_sec} - the next 'pair' or 'pin' run "
       f"sets it back", DIM)

    if args.json:
        args.json.write_text(json.dumps({
            "port": port,
            "seconds": args.seconds,
            "adv_ms": args.adv_ms,
            "rot_sec": args.rot_sec,
            "dbg_sec": dbg_sec,
            "cycles": len(awake),
            "awake_us": awake,
            "sleep_us": sleeps,
            "awake_median_us": med_a,
            "sleep_median_us": med_s,
            "awake_duty": duty,
            "avg_current_ma": avg_ma,
        }, indent=2) + "\n")
        ui(f"raw data -> {args.json}")
    log(channel, f"duty {duty * 100:.2f}% over {len(awake)} cycles")
    return 0


def load_results() -> dict:
    if REPORTS_JSON.exists():
        return json.loads(REPORTS_JSON.read_text())
    return {}


def save_results(results: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
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
    return (d["slot"], d["time"], round(d["latitude"], 6),
            round(d["longitude"], 6))


async def fetch_new_reports(account, acc, dev_id: str, results: dict,
                            backtrack: int, channel: str) -> list:
    from findmy.errors import EmptyResponseError

    now = datetime.now(timezone.utc)
    max_i = acc.get_max_index(now)
    state = results.setdefault(dev_id, {"reports": [], "last_fetched_slot": None})
    known = {report_key(d) for d in state["reports"]}
    slots_with_reports = [d["slot"] for d in state["reports"]]
    floor = max(0, max_i - backtrack)
    if slots_with_reports:
        start = max(max(slots_with_reports), floor)
    else:
        start = floor

    log(channel, f"{dev_id}: query window slots {start}..{max_i} "
                 f"({max_i - start + 1} request(s), archive has "
                 f"{len(state['reports'])} report(s))")

    fresh = []
    for slot in range(max_i, start - 1, -1):
        keys = {}
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
            log(channel, f"{dev_id}: slot {slot}: {len(raw)} report(s) on "
                         f"server, {added} new")
        state["last_fetched_slot"] = slot
        save_results(results)
        await asyncio.sleep(0.2)

    state["reports"].sort(key=lambda d: d["time"])
    save_results(results)
    return fresh


async def apple_login(apple_id: str | None, channel: str):
    from findmy import AsyncAppleAccount, LocalAnisetteProvider
    from findmy.reports import LoginState

    if SESSION_FILE.exists():
        try:
            account = AsyncAppleAccount.from_json(str(SESSION_FILE))
            if account.login_state == LoginState.LOGGED_IN:
                log(channel, f"restored saved session for {account.account_name}")
                return account
            log(channel, f"saved session state: {account.login_state}, "
                         f"re-login required", logging.WARNING)
        except Exception as exc:
            log(channel, f"could not restore session: {exc}", logging.WARNING)

    if not sys.stdin.isatty():
        log_error(channel, "no saved Apple session and no terminal to log in on")
        return None

    apple_id = (apple_id or input("Apple ID: ")).strip()
    log(channel, f"logging in as {apple_id}")
    password = getpass.getpass("Apple ID password: ")

    account = AsyncAppleAccount(LocalAnisetteProvider())
    state = await account.login(apple_id, password)

    if state == LoginState.REQUIRE_2FA:
        methods = await account.get_2fa_methods()
        for i, method in enumerate(methods):
            kind = (f"SMS to {method.phone_number}"
                    if hasattr(method, "phone_number") else "trusted device push")
            print(f"  {i}: {kind}")
        method = methods[0]
        await method.request()
        print("2FA code sent - check your device.")
        state = await method.submit(input("Enter 2FA code: ").strip())

    if state != LoginState.LOGGED_IN:
        log_error(channel, f"login failed (state: {state})")
        await account.close()
        return None

    account.to_json(str(SESSION_FILE))
    try:
        SESSION_FILE.chmod(0o600)
    except OSError:
        pass
    log(channel, f"session saved to {SESSION_FILE}")
    return account


def print_new_report(dev_id: str, d: dict) -> None:
    ui(f"  NEW  [slot {d['slot']} ({d['type']})] {dev_id}")
    ui(f"    Time:       {d['time']}")
    ui(f"    Latitude:   {d['latitude']}")
    ui(f"    Longitude:  {d['longitude']}")
    ui(f"    Accuracy:   {d['accuracy_m']} m")
    ui(f"    Confidence: {d['confidence']}")


async def retrieve_run(apple_id: str | None, dev_filter: str | None,
                       watch: int, backtrack: int,
                       channel: str = "RETRIEVE") -> int:
    data = load_devices()
    devices = data["devices"]
    if dev_filter:
        devices = [d for d in devices if d["id"] == dev_filter]
        if not devices:
            log_error(channel, f"no device '{dev_filter}' in {DEVICES_JSON.name}")
            return 1
    if not devices:
        log_error(channel, f"no devices in {DEVICES_JSON.name} - run 'pair' first")
        return 1

    accessories = {d["id"]: make_accessory(d) for d in devices}
    for dev_id, acc in accessories.items():
        log(channel, f"'{dev_id}': paired {acc.paired_at}, "
                     f"slot {acc._alignment_index} at last sync, "
                     f"{int(acc.interval.total_seconds())}s slots")

    account = await apple_login(apple_id, channel)
    if account is None:
        return 1

    results = load_results()
    try:
        attempt = 0
        while True:
            attempt += 1
            log(channel, f"fetch attempt {attempt}")
            fresh_total = 0
            for dev_id, acc in accessories.items():
                fresh = await fetch_new_reports(account, acc, dev_id, results,
                                                backtrack, channel)
                fresh_total += len(fresh)
                for d in fresh:
                    log(channel, f"{dev_id}: new report slot={d['slot']} "
                                 f"time={d['time']} lat={d['latitude']} "
                                 f"lon={d['longitude']} acc={d['accuracy_m']}")
                    print_new_report(dev_id, d)
                archive = results[dev_id]["reports"]
                log(channel, f"{dev_id}: archive has {len(archive)} report(s) "
                             f"in {REPORTS_JSON.name}")
            log(channel, f"new this run: {fresh_total} report(s)")

            if fresh_total or not watch:
                if not fresh_total:
                    ui("No new reports - keep the beacon near a locked iPhone.",
                       DIM)
                return 0
            log(channel, f"retrying in {watch}s (Ctrl-C to stop)")
            await asyncio.sleep(watch)
    except KeyboardInterrupt:
        log(channel, "stopped")
        return 130
    finally:
        save_results(results)
        try:
            await account.close()
        except Exception:
            pass


def retrieve_running_info() -> dict | None:
    if not LOCK_FILE.exists():
        return None
    try:
        probe = LOCK_FILE.open("r+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
            return None
        except BlockingIOError:
            pass
        try:
            info = json.loads(LOCK_FILE.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            info = {}
        pid = info.get("pid")
        alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
        return {"pid": pid, "started": info.get("started"), "alive": alive}
    finally:
        probe.close()


def retrieve_start_bg() -> int:
    channel = "WORKER"
    STATE_DIR.mkdir(exist_ok=True)
    existing = retrieve_running_info()
    if existing:
        log(channel, f"retrieval worker already running (pid {existing['pid']})")
        ui(f"already running (pid {existing['pid']})")
        return 1

    handle = LOCK_FILE.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(channel, "retrieval worker already running")
        handle.close()
        return 1

    write_handle(LOCK_FILE, {"pid": os.getpid(),
                             "started": datetime.now(timezone.utc).isoformat(),
                             "state": "starting"})

    pid = os.fork()
    if pid > 0:
        time.sleep(0.3)
        worker = pid
        try:
            worker = json.loads(LOCK_FILE.read_text() or "{}").get("pid", pid)
        except (OSError, json.JSONDecodeError):
            pass
        log(channel, f"retrieval worker started in the background (pid {worker})")
        ui(f"retrieval worker started (pid {worker}), logs in {LOG_FILE.name}")
        ui("  use 'retrieve --status', 'retrieve --follow' or 'retrieve --stop'")
        return 0

    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    run_retrieve_worker(handle)


def write_handle(path: Path, payload: dict) -> None:
    try:
        with path.open("w") as fh:
            fh.write(json.dumps(payload))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def run_retrieve_worker(handle) -> None:
    channel = "WORKER"
    worker_pid = os.getpid()

    def stop(signum, frame):
        log(channel, f"worker stopping (signal {signum})")
        try:
            write_handle(LOCK_FILE, {"pid": worker_pid, "state": "stopped"})
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    write_handle(LOCK_FILE, {"pid": worker_pid,
                             "started": datetime.now(timezone.utc).isoformat(),
                             "state": "running"})
    log(channel, f"retrieval worker running (pid {worker_pid})")

    while True:
        try:
            asyncio.run(retrieve_run(None, None, 0, MAX_BACKTRACK_SLOTS,
                                     channel=channel))
        except Exception as exc:
            log_error(channel, f"fetch failed: {exc}")
        write_handle(LOCK_FILE, {"pid": worker_pid,
                                 "started": datetime.now(timezone.utc).isoformat(),
                                 "state": "running",
                                 "last_cycle": datetime.now(timezone.utc).isoformat()})
        log(channel, f"next cycle in {RETRIEVE_SLEEP_S}s")
        time.sleep(RETRIEVE_SLEEP_S)


def retrieve_status() -> int:
    channel = "RETRIEVE"
    info = retrieve_running_info()
    if not info:
        log(channel, "no retrieval worker running")
        ui("retrieval worker: not running")
        return 0
    if not info["alive"]:
        ui(f"retrieval worker: stale lock file (pid {info['pid']} is gone)")
        return 1
    ui(f"retrieval worker: running (pid {info['pid']}, "
       f"started {info.get('started', '?')})")
    return 0


def retrieve_stop() -> int:
    channel = "WORKER"
    info = retrieve_running_info()
    if not info or not info["alive"]:
        log(channel, "no retrieval worker running")
        ui("retrieval worker: not running")
        return 0
    try:
        os.kill(int(info["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        log_error(channel, f"could not signal pid {info['pid']}: {exc}")
        return 1
    for _ in range(20):
        if retrieve_running_info() is None:
            break
        time.sleep(0.25)
    log(channel, f"retrieval worker {info['pid']} stopped")
    ui("retrieval worker stopped")
    return 0


def retrieve_follow(lines: int = 50, filtered: bool = True) -> int:
    channel = "RETRIEVE"
    if not LOG_FILE.exists():
        log_error(channel, f"{LOG_FILE} does not exist yet")
        return 1

    def keep(line: str) -> bool:
        if not filtered:
            return True
        return "[RETRIEVE" in line or "[WORKER " in line

    content = LOG_FILE.read_text(errors="replace").splitlines()
    for line in [l for l in content if keep(l)][-lines:]:
        print(line, flush=True)
    print(paint(DIM, f"following {LOG_FILE.name} - Ctrl-C to stop", ui=True),
          flush=True)
    size = LOG_FILE.stat().st_size
    try:
        while True:
            time.sleep(0.5)
            new_size = LOG_FILE.stat().st_size
            if new_size < size:
                size = 0
            if new_size == size:
                continue
            with LOG_FILE.open("r") as fh:
                fh.seek(size)
                chunk = fh.read()
                size = fh.tell()
            for line in chunk.splitlines():
                if keep(line):
                    print(line, flush=True)
    except KeyboardInterrupt:
        print("", flush=True)
        return 0


def cmd_retrieve(args) -> int:
    channel = "RETRIEVE"
    if args.status:
        return retrieve_status()
    if args.stop:
        return retrieve_stop()
    if args.follow:
        return retrieve_follow(args.lines)
    if args.bg:
        return retrieve_start_bg()

    watch = args.watch or 0
    log(channel, f"fetching reports (watch={watch})")
    try:
        return asyncio.run(retrieve_run(args.apple_id, args.device, watch,
                                        args.back, channel=channel))
    except KeyboardInterrupt:
        log(channel, "stopped")
        return 130


def cmd_watch(args) -> int:
    channel = "RETRIEVE"
    interval = args.interval or DEFAULT_WATCH
    log(channel, f"watching for reports every {interval}s")
    try:
        return asyncio.run(retrieve_run(None, args.device, interval,
                                        MAX_BACKTRACK_SLOTS, channel=channel))
    except KeyboardInterrupt:
        log(channel, "stopped")
        return 130


def render_monitor(device_id: str | None) -> str:
    now = datetime.now(timezone.utc)
    devices = {d["id"]: d for d in load_json(DEVICES_JSON).get("devices", [])}
    results = load_json(REPORTS_JSON)
    p = lambda color, text: paint(color, text, ui=True)

    if not results:
        body = p(DIM, "no archive yet - run 'findmy-toolbox.py retrieve' first")
        ids = []
    else:
        ids = [device_id] if device_id and device_id in results else list(results)
        if device_id and device_id not in results:
            body = p(RED, f"device '{device_id}' not in reports.json")
            ids = []

    out = []
    width = 74
    out.append(p(CYAN + BOLD, f"╔{'═' * (width - 2)}╗"))
    title = " ESP32 FIND MY - BEACON MONITOR "
    out.append(p(CYAN + BOLD, f"║{title}{' ' * (width - 2 - len(title))}║"))
    out.append(p(CYAN + BOLD, f"╚{'═' * (width - 2)}╝"))

    for i, dev_id in enumerate(ids):
        state = results[dev_id]
        reports = state.get("reports", [])
        dev = devices.get(dev_id, {})
        if i:
            out.append("")
        out.append(p(WHITE + BOLD, f"▍ {dev_id}") + "  " +
                   p(DIM, f"slots: {dev.get('slot_seconds', '?')}s  "
                          f"paired: {dev.get('paired_at', '?')}"))
        if not reports:
            out.append("  " + p(DIM, "no reports yet"))
            continue

        newest = max(parse_time(r["time"]) for r in reports)
        age = (now - newest).total_seconds()
        col = freshness_color(age)
        out.append("  " + p(BOLD, "FRESHNESS") + "  " +
                   p(col + BOLD, fmt_age(age)) + p(col, " since last report") +
                   "  " + p(DIM, f"(report from "
                                 f"{newest.astimezone().strftime('%H:%M:%S')}, "
                                 f"slot {max(r['slot'] for r in reports)})"))
        bar_w = 40
        filled = int(bar_w * min(1.0, age / FRESH_WARN))
        out.append("  " + p(col, "█" * filled + "░" * (bar_w - filled)))

        out.append("  " + p(MAGENTA + BOLD, "── latest reports " + "─" * 36))
        out.append("  " + p(DIM, f"{'slot':>5}  {'time (local)':<8}  {'age':>8}  "
                                 f"{'lat':>10}  {'lon':>10}  {'acc':>6}"))
        for r in sorted(reports, key=lambda x: x["time"])[-8:]:
            t = parse_time(r["time"]).astimezone()
            r_age = fmt_age((now - parse_time(r["time"])).total_seconds())
            out.append(f"  {r['slot']:>5}  {t.strftime('%H:%M:%S'):<8}  "
                       f"{r_age:>8}  {r['latitude']:>10.5f}  "
                       f"{r['longitude']:>10.5f}  "
                       f"{str(r['accuracy_m']) + 'm':>6}")

        cur = current_slot(dev)
        if cur >= 0:
            counts: dict[int, int] = {}
            for r in reports:
                counts[r["slot"]] = counts.get(r["slot"], 0) + 1
            lo = max(0, cur - 39)
            strip = "".join(BAR_CHARS[min(8, counts.get(s, 0))]
                            for s in range(lo, cur + 1))
            out.append("  " + p(MAGENTA + BOLD,
                                f"── slot coverage (last {cur - lo + 1}) "
                                + "─" * 22))
            out.append("  " + p(GREEN, strip))
            label_lo = f"slot {lo}"
            label_hi = f"now ({cur})"
            gap = max(1, len(strip) - len(label_lo) - len(label_hi))
            out.append("  " + p(DIM, f"{label_lo}{' ' * gap}{label_hi}"))

        out.append("  " + p(DIM, f"archive: {len(reports)} report(s), "
                                 f"{len({r['slot'] for r in reports})} slots with "
                                 f"data, last fetched slot "
                                 f"{state.get('last_fetched_slot', '?')}"))

    if not ids and "body" in locals():
        out.append(body)
    out.append("")
    out.append(p(DIM, "refreshing every few seconds - Ctrl-C to quit"))
    return "\n".join(out)


def current_slot(dev: dict) -> int:
    try:
        synced = parse_time(dev["slot_synced_at"])
        elapsed = datetime.now(timezone.utc) - synced
        return int(dev["last_known_slot"] + elapsed.total_seconds()
                   // int(dev.get("slot_seconds", 120)))
    except (KeyError, ValueError, TypeError):
        return -1


def cmd_monitor(args) -> int:
    channel = "MONITOR"
    log(channel, f"monitoring every {args.interval:g}s"
                 + (f" for {args.device}" if args.device else ""))
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(render_monitor(args.device))
            sys.stdout.flush()
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write(RESET + "\n")
        log(channel, "stopped")
        return 0


async def verify_scan(duration: float, targets: dict) -> dict:
    from bleak import BleakScanner

    found = {"ok": False, "mac": None, "got": None, "rssi": None, "status": None}

    def cb(device, adv):
        mfr = adv.manufacturer_data.get(APPLE_MFR)
        if mfr is None:
            return
        key = targets.get(device.address.upper())
        if key is None:
            return
        found["mac"], found["got"] = device.address, mfr.hex()
        if len(mfr) > 2:
            found["status"] = mfr[2]
        if payload_matches(mfr, key):
            found.update(ok=True, rssi=adv.rssi)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()
    return found


def rotating_targets(channel: str) -> dict:
    data = load_devices()
    devices = data["devices"]
    if not devices:
        raise UserError(f"no devices in {DEVICES_JSON.name} - run 'pair' first")
    now = datetime.now(timezone.utc)
    out: dict = {}
    for dev in devices:
        acc = make_accessory(dev)
        max_i = acc.get_max_index(now)
        min_i = max(0, max_i - 96)
        log(channel, f"{dev['id']}: accepting slots {min_i}..{max_i}")
        for ind in range(min_i, max_i + 1):
            key = acc._primary_key_at(ind)
            out[beacon_mac(key)] = key
    return out


def cmd_verify(args) -> int:
    channel = "VERIFY"
    try:
        targets = rotating_targets(channel)
    except UserError as exc:
        log_error(channel, str(exc))
        return 1
    log(channel, f"expecting {len(targets)} known key(s)/MAC(s), "
                 f"scanning {args.seconds:g}s")
    ui(f"expecting {len(targets)} known key(s)/MAC(s)")
    ui(f"Scanning {args.seconds:g}s ...")
    try:
        res = asyncio.run(verify_scan(args.seconds, targets))
    except PermissionError:
        log_error(channel, "BLE scan needs permissions - run with sudo")
        return 1

    ui("")
    if res["ok"]:
        log(channel, f"beacon advertises a key we hold (mac={res['mac']}, "
                     f"rssi={res['rssi']} dBm, status={status_decode(res['status'])})")
        ui("PASS: beacon advertises a key we hold", GREEN + BOLD)
        ui(f"  MAC   : {res['mac']}")
        ui(f"  RSSI  : {res['rssi']} dBm")
        ui(f"  status: {status_decode(res['status']) if res['status'] is not None else '?'}")
        return 0
    if res["mac"]:
        log_error(channel, f"MAC {res['mac']} seen but payload differs "
                           f"(status {status_decode(res['status'])})")
        ui(f"FAIL: MAC {res['mac']} seen but payload differs!", RED + BOLD)
        ui(f"  received: {res['got']}")
        if res["status"] is not None:
            ui(f"  status  : {status_decode(res['status'])}")
        return 2
    log_error(channel, "no known beacon MAC seen - unpaired, powered off, or "
                       "slot too old? run 'sync' or 'sync-ble'")
    ui("FAIL: no known beacon MAC seen (unpaired? powered? slot too old - "
       "run 'sync')", RED + BOLD)
    return 1


def parse_of_payload(data: bytes) -> dict | None:
    if len(data) < 4 or data[0] != OF_TYPE or data[1] != OF_LEN:
        return None
    payload = data[2:2 + data[1]]
    if len(payload) < 23:
        return None
    return {
        "status": payload[0],
        "public_key_x_suffix": payload[1:23].hex(),
        "key_high_bits": f"0x{payload[23]:02x}" if len(payload) > 23 else "0x00",
        "hint": f"0x{payload[24]:02x}" if len(payload) > 24 else "0x00",
    }


async def scan_capture(duration: float, seen: dict) -> None:
    from bleak import BleakScanner

    def cb(device, adv):
        mfr = adv.manufacturer_data.get(APPLE_MFR)
        if mfr is None:
            return
        parsed = parse_of_payload(mfr)
        if parsed is None:
            return
        mac = device.address.upper()
        entry = seen.setdefault(mac, {**parsed, "rssi": adv.rssi,
                                      "address": device.address,
                                      "name": device.name, "hits": 0,
                                      "raw": mfr.hex()})
        entry["hits"] += 1
        entry["rssi"] = adv.rssi
        entry["status"] = parsed["status"]

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()


def cmd_scan(args) -> int:
    channel = "SCAN"
    duration = args.seconds
    log(channel, f"scanning {duration:g}s for Apple Offline Finding packets")
    seen: dict = {}
    try:
        asyncio.run(scan_capture(duration, seen))
    except PermissionError:
        log_error(channel, "BLE scan needs permissions - run with sudo")
        return 1

    if not seen:
        log(channel, "no Offline Finding advertisements captured")
        ui("no Find My advertisements captured", YELLOW)
        return 1

    ui(f"\ncaptured {len(seen)} beacon(es):")
    for mac, e in sorted(seen.items(), key=lambda kv: -kv[1]["rssi"]):
        ui(f"  {mac}  rssi={e['rssi']:>4}  hits={e['hits']:<4}  "
           f"status={status_decode(e['status'])}")
        ui(f"    key  : {e['public_key_x_suffix']}")
        ui(f"    hint : {e['hint']}  key_high_bits: {e['key_high_bits']}")
        log(channel, f"{mac} rssi={e['rssi']} status={e['status']:#04x} "
                     f"payload={e['raw']}")
    ui("")
    return 0


def cmd_pin(args) -> int:
    channel = "MAIN"
    data = load_devices()
    dev_id = pick_device(data, args.id)
    device = find_device(data, dev_id)
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    new_pin = args.new_pin
    if not new_pin:
        if not sys.stdin.isatty():
            raise UserError("--new-pin is required without a terminal")
        new_pin = prompt_valid_pin("new PIN")
        if prompt_valid_pin("repeat new PIN") != new_pin:
            raise UserError("the two PINs do not match")
    if not valid_pin(new_pin):
        raise UserError(f"the PIN must be {PIN_LEN} digits")

    log(channel, f"changing the console PIN of '{dev_id}' on {port}")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        reply = beacon.cmd(f"PIN {new_pin}")
        if not reply.startswith("OK PIN"):
            raise UserError(f"PIN change refused: {reply}")
        beacon.pin = new_pin
    finally:
        beacon.close()

    store_pin(dev_id, new_pin)
    log(channel, f"PIN of '{dev_id}' changed")
    ui(f"\nPIN for '{dev_id}' is now {paint(GREEN, new_pin, ui=True)}")
    ui("the device is locked again", DIM)
    return 0


def cmd_unlock(args) -> int:
    channel = "MAIN"
    data = load_devices()
    device = None
    if args.id:
        device = find_device(data, args.id)
    elif data["devices"]:
        device = find_device(data, pick_device(data, None))
    dev_id = device["id"] if device else None
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    log(channel, f"unlocking the console on {port}")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel, auto_lock=False,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
    finally:
        beacon.close()
    ui(f"console on {port} is UNLOCKED and left unlocked",
       GREEN + BOLD)
    ui("it stays awake until you run 'lock' or power-cycle it", DIM)
    return 0


def cmd_lock(args) -> int:
    channel = "MAIN"
    data = load_devices()
    device = None
    if args.id:
        device = find_device(data, args.id)
    elif data["devices"]:
        device = find_device(data, pick_device(data, None))
    dev_id = device["id"] if device else None
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    log(channel, f"locking the console on {port}")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    auto_unlock=False, auto_lock=False,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        beacon.lock(pin)
    finally:
        beacon.close()
    ui(f"console on {port} is LOCKED (countdown running)", GREEN + BOLD)
    return 0


def cmd_wipe(args) -> int:
    channel = "MAIN"
    data = load_devices()
    dev_id = pick_device(data, args.id)
    device = find_device(data, dev_id)
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    if not args.yes:
        if not sys.stdin.isatty():
            raise UserError("factory reset needs confirmation: pass --yes")
        ui(f"this erases the key chain and the console PIN of '{dev_id}' "
           f"on {port}", YELLOW + BOLD)
        ui(f"the device reboots unpaired with the factory PIN {DEFAULT_PIN}, "
           f"and '{dev_id}' is removed from {DEVICES_JSON.name}", DIM)
        if prompt("type 'wipe' to continue").lower() != "wipe":
            ui("cancelled - nothing was changed", DIM)
            log(channel, "WIPE cancelled")
            return 0

    log(channel, f"factory-resetting '{dev_id}' on {port} (WIPE)")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel, auto_lock=False,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        reply = beacon.cmd("WIPE", wait=5)
        if not reply.startswith("OK WIPE"):
            raise UserError(f"WIPE refused: {reply}")
    finally:
        beacon.close()

    data = load_devices()
    if find_device(data, dev_id) is not None:
        data["devices"] = [d for d in data["devices"] if d["id"] != dev_id]
        save_devices(data)
        log(channel, f"'{dev_id}' dropped from {DEVICES_JSON.name}")

    ui(f"\n'{dev_id}' was factory-reset on {port}", GREEN + BOLD)
    ui("the keys and the PIN are gone from the device and from "
       f"{DEVICES_JSON.name}", DIM)
    ui(f"it is in config mode now: the console opens with the factory PIN "
       f"{DEFAULT_PIN} and the device never sleeps", DIM)
    ui("run 'pair' to provision fresh keys", DIM)
    return 0


def cmd_log(args) -> int:
    channel = "MAIN"
    if not LOG_FILE.exists():
        log_error(channel, f"{LOG_FILE} does not exist yet")
        return 1
    if args.follow:
        return retrieve_follow(args.lines, filtered=False)

    lines = LOG_FILE.read_text(errors="replace").splitlines()[-args.lines:]
    for line in lines:
        print(line)
    log(channel, f"showed the last {len(lines)} log line(s)")
    return 0


HELP_TEXT = """\
findmy-toolbox.py - everything for the ESP32-S3 Find My beacon

  pair       pair a beacon over UART (keys + fresh PIN -> devices.json)
  sync       sync the slot counter over USB
  sync-ble   sync the slot counter from the BLE advertisement
  devices    list paired devices
  test       console protocol test suite (resets the device)
  power      awake/sleep duty cycle from the PWR telemetry
  retrieve   fetch location reports
               --bg       run a background worker (state/retrieve.lock)
               --status   is the worker running?
               --follow   tail the retrieval log
               --stop     stop the background worker
  watch      retrieve and keep polling (default every 120 s)
  monitor    live dashboard (Ctrl-C to quit)
  verify     check the advertisement on air
  scan       raw BLE scan for Find My packets
  pin        set a new console PIN
  unlock     unlock the console (device left unlocked)
  lock       lock the console (device left locked)
  wipe       factory reset: erase keys + PIN, unpair (--yes to confirm)
  log        show or follow state/toolbox.log
  help       show this overview

Options before or after the command: -v/--verbose (debug logging).

State: state/{devices.json,reports.json,account_state.json,toolbox.log,
retrieve.lock}. Logs go to stderr and state/toolbox.log (secrets redacted).
"""


def cmd_help(args) -> int:
    print(HELP_TEXT, end="")
    return 0


COMMANDS = {
    "pair": cmd_pair,
    "sync": cmd_sync,
    "sync-ble": cmd_sync_ble,
    "devices": cmd_devices,
    "test": cmd_test,
    "power": cmd_power,
    "retrieve": cmd_retrieve,
    "watch": cmd_watch,
    "monitor": cmd_monitor,
    "verify": cmd_verify,
    "scan": cmd_scan,
    "pin": cmd_pin,
    "unlock": cmd_unlock,
    "lock": cmd_lock,
    "wipe": cmd_wipe,
    "log": cmd_log,
    "help": cmd_help,
}

COMMAND_CHANNEL = {
    "pair": "PAIR", "sync": "SYNC", "sync-ble": "SYNC-BLE", "devices": "MAIN",
    "test": "TEST", "power": "POWER", "retrieve": "RETRIEVE", "watch": "RETRIEVE",
    "monitor": "MONITOR", "verify": "VERIFY", "scan": "SCAN", "pin": "MAIN",
    "unlock": "MAIN", "lock": "MAIN", "wipe": "MAIN", "log": "MAIN", "help": "UI",
}

MENU_ENTRIES = [
    ("pair", "pair a beacon over UART"),
    ("sync", "sync the slot counter (USB)"),
    ("sync-ble", "sync the slot counter from BLE"),
    ("devices", "list paired devices"),
    ("test", "console protocol test suite"),
    ("power", "awake/sleep duty cycle"),
    ("retrieve", "fetch location reports"),
    ("watch", "retrieve + keep polling"),
    ("monitor", "live dashboard"),
    ("verify", "check the advertisement on air"),
    ("scan", "raw BLE scan"),
    ("pin", "set a new console PIN"),
    ("unlock", "unlock the console (left unlocked)"),
    ("lock", "lock the console (left locked)"),
    ("wipe", "factory reset: erase keys + PIN"),
    ("log", "show or follow toolbox.log"),
]


def status_lines() -> list[str]:
    p = lambda color, text: paint(color, text, ui=True)
    try:
        devices = load_devices().get("devices", [])
    except UserError as exc:
        return [p(RED, f"devices.json: {exc}")]
    results = load_json(REPORTS_JSON)
    info = retrieve_running_info()

    newest_age = None
    report_count = 0
    for state in results.values():
        reports = state.get("reports", [])
        report_count += len(reports)
        if reports:
            age = (datetime.now(timezone.utc)
                   - max(parse_time(r["time"]) for r in reports)).total_seconds()
            newest_age = age if newest_age is None else min(newest_age, age)

    worker = p(DIM, "stopped")
    if info and info["alive"]:
        worker = p(GREEN, f"running (pid {info['pid']})")
    elif info:
        worker = p(YELLOW, "stale lock file")

    lines = [
        p(DIM, f"devices : {len(devices)}"
               + (f" ({', '.join(d['id'] for d in devices)})" if devices else "")),
        p(DIM, f"reports : {report_count}"
               + (f", newest {fmt_age(newest_age)} ago" if newest_age is not None
                  else "")),
        p(DIM, f"worker  : {worker}"),
        p(DIM, f"log     : {LOG_FILE.relative_to(SCRIPTS_DIR)}"),
    ]
    return lines


def menu() -> int:
    p = lambda color, text: paint(color, text, ui=True)
    width = 74
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            print(p(CYAN + BOLD, f"╔{'═' * (width - 2)}╗"))
            title = " ESP32 FIND MY - TOOLBOX "
            print(p(CYAN + BOLD, f"║{title}{' ' * (width - 2 - len(title))}║"))
            print(p(CYAN + BOLD, f"╚{'═' * (width - 2)}╝"))
            for line in status_lines():
                print("  " + line)
            print()
            for i, (name, description) in enumerate(MENU_ENTRIES, 1):
                print(f"  {i:>2}) {name:<9} {description}")
            print("   0) quit")
            print()
            choice = input(p(BOLD, "> ")).strip().lower()
            if choice in ("0", "q", "quit", "exit"):
                log("UI", "menu closed")
                return 0
            name = None
            if choice.isdigit() and 1 <= int(choice) <= len(MENU_ENTRIES):
                name = MENU_ENTRIES[int(choice) - 1][0]
            elif choice in COMMANDS:
                name = choice
            if name is None:
                print(p(RED, f"unknown choice '{choice}'"))
                time.sleep(1.0)
                continue
            log("UI", f"menu: {name}")
            args = build_parser().parse_args([name])
            try:
                rc = COMMANDS[name](args)
            except (UserError, BeaconError, OSError) as exc:
                print(p(RED, f"\n{exc}"))
                log_error(COMMAND_CHANNEL.get(name, "MAIN"), str(exc))
                rc = 1
            if rc:
                print(p(YELLOW, f"\n{name} exited with status {rc}"))
            input(p(DIM, "\npress Enter to return to the menu "))
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        log("UI", "menu closed")
        return 0


def build_parser() -> argparse.ArgumentParser:
    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("--verbose", "-v", action="store_true",
                         default=argparse.SUPPRESS, help="debug logging")

    parser = argparse.ArgumentParser(
        prog="findmy-toolbox.py",
        description=HELP_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="debug logging")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    pair = sub.add_parser("pair", parents=[verbose],
                          help="pair a beacon over UART")
    pair.add_argument("--port")
    pair.add_argument("--id", help="device id used in devices.json")
    pair.add_argument("--pin", help="PIN used to unlock the device")
    pair.add_argument("--new-pin", help=f"PIN to set after pairing "
                                        f"({PIN_LEN} digits, default: random)")
    pair.add_argument("--force", action="store_true",
                      help="overwrite an existing device id")
    pair.add_argument("--debug", type=int, choices=[0, 1], default=None,
                      help="device debug flag after pairing")
    pair.add_argument("--adv-ms", type=int,
                      help="advertisement period in ms (200..60000)")
    pair.add_argument("--rot-sec", type=int, help="key rotation period (s)")
    pair.add_argument("--dbg-sec", type=int, help="console countdown (s)")

    sync = sub.add_parser("sync", parents=[verbose],
                          help="sync the slot counter over USB")
    sync.add_argument("--port")
    sync.add_argument("--id")
    sync.add_argument("--pin")

    sync_ble = sub.add_parser("sync-ble", parents=[verbose],
                              help="sync the slot counter from BLE")
    sync_ble.add_argument("--device", default=DEFAULT_DEVICE)
    sync_ble.add_argument("--window", type=float, default=DEFAULT_SCAN_SECS,
                          help="scan window in seconds")
    sync_ble.add_argument("--max-slots", type=int, default=None, metavar="N")

    sub.add_parser("devices", parents=[verbose], help="list paired devices")

    test = sub.add_parser("test", parents=[verbose],
                          help="console protocol test suite")
    test.add_argument("--port")
    test.add_argument("--id")
    test.add_argument("--pin")
    test.add_argument("--no-reset", action="store_true",
                      help="assume the device is already in its console")

    power = sub.add_parser("power", parents=[verbose],
                           help="awake/sleep duty cycle")
    power.add_argument("--port")
    power.add_argument("--id")
    power.add_argument("--pin")
    power.add_argument("--seconds", type=float, default=DEFAULT_SECS,
                       help="how long to listen in the steady state")
    power.add_argument("--dbg-sec", type=int, default=None,
                       help="console countdown to measure with (default 60)")
    power.add_argument("--adv-ms", type=int, default=2000)
    power.add_argument("--rot-sec", type=int, default=120)
    power.add_argument("--ma-awake", type=float, default=None)
    power.add_argument("--ma-sleep", type=float, default=None)
    power.add_argument("--json", type=Path, default=None)

    retrieve = sub.add_parser("retrieve", parents=[verbose],
                              help="fetch location reports")
    retrieve.add_argument("apple_id", nargs="?", default=None)
    retrieve.add_argument("--device", default=DEFAULT_DEVICE)
    retrieve.add_argument("--back", type=int, default=MAX_BACKTRACK_SLOTS,
                          metavar="N", help="slots to query into the past")
    retrieve.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                          help="keep polling every N seconds")
    retrieve.add_argument("--bg", action="store_true",
                          help="run a background worker")
    retrieve.add_argument("--status", action="store_true",
                          help="report the worker state")
    retrieve.add_argument("--follow", action="store_true",
                          help="tail the retrieval log")
    retrieve.add_argument("--stop", action="store_true",
                          help="stop the background worker")
    retrieve.add_argument("--lines", type=int, default=50,
                          help="log lines to show with --follow")

    watch = sub.add_parser("watch", parents=[verbose],
                           help="retrieve and keep polling")
    watch.add_argument("--device", default=DEFAULT_DEVICE)
    watch.add_argument("--interval", type=int, default=DEFAULT_WATCH,
                       help="seconds between fetches")

    monitor = sub.add_parser("monitor", parents=[verbose],
                             help="live dashboard")
    monitor.add_argument("--interval", type=float, default=5.0)
    monitor.add_argument("--device")
    monitor.add_argument("--once", action="store_true")

    verify = sub.add_parser("verify", parents=[verbose],
                            help="check the advertisement on air")
    verify.add_argument("seconds", nargs="?", type=float, default=15.0)

    scan = sub.add_parser("scan", parents=[verbose],
                          help="raw BLE scan for Find My packets")
    scan.add_argument("seconds", nargs="?", type=float,
                      default=float(DEFAULT_SCAN_SECS))

    for name, help_text in (("pin", "set a new console PIN"),
                            ("unlock", "unlock the console"),
                            ("lock", "lock the console"),
                            ("wipe", "factory reset: erase keys + PIN")):
        sub.add_parser(name, parents=[verbose], help=help_text) \
           .add_argument("--port")
    pin_parser = sub.choices["pin"]
    pin_parser.add_argument("--id")
    pin_parser.add_argument("--pin", help="current PIN (default: stored)")
    pin_parser.add_argument("--new-pin", help=f"{PIN_LEN} new digits")
    for name in ("unlock", "lock", "wipe"):
        sub.choices[name].add_argument("--id")
        sub.choices[name].add_argument("--pin")
    sub.choices["wipe"].add_argument(
        "--yes", "-y", action="store_true",
        help="skip the interactive confirmation (required without a TTY)")

    log_parser = sub.add_parser("log", parents=[verbose],
                                help="show or follow toolbox.log")
    log_parser.add_argument("--lines", type=int, default=50)
    log_parser.add_argument("--follow", action="store_true")

    for name in ("pair", "sync", "power", "pin", "unlock", "lock", "wipe"):
        sub.choices[name].add_argument(
            "--reset", action="store_true",
            help="pulse the reset line first (device asleep / no console)")

    sub.add_parser("help", parents=[verbose], help="show this overview")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))

    if not args.command:
        return menu()

    func = COMMANDS.get(args.command)
    if func is None:
        parser.print_help()
        return 2

    channel = COMMAND_CHANNEL.get(args.command, "MAIN")
    try:
        return func(args)
    except (UserError, BeaconError, OSError) as exc:
        log_error(channel, str(exc))
        return 1
    except KeyboardInterrupt:
        log_error(channel, "interrupted")
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())




