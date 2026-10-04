#!/usr/bin/env python3
"""
findmy-toolbox.py - one tool for the ESP32-S3 Find My beacon.

Usage:
    python3 findmy-toolbox.py                 interactive menu
    python3 findmy-toolbox.py <command> [...] run one command
    python3 findmy-toolbox.py help            command overview

Commands:
    pair       pair a beacon over UART (keys + PIN, stores devices.json)
    connect    find a beacon on the TTYs, identify it, unlock its console
    disconnect lock the console of the connected beacon
    reset      reboot a beacon over the UART control lines (wake it up)
    apple-id   connect or disconnect the Apple ID session
    sync       sync the slot counter over USB
    sync-ble   sync the slot counter from the BLE advertisement
    devices    list paired devices
    test       console protocol test suite (resets the device)
    power      awake/sleep duty cycle from the PWR telemetry
    retrieve   fetch location reports  (--bg/--status/--doctor/--follow)
    watch      retrieve and keep polling
    monitor    live dashboard
    verify     spec-check the advertisement on air
    scan       raw BLE scan for Find My packets
    pin        set a new console PIN
    unlock     unlock the console (device left unlocked)
    lock       lock the console (device left locked)
    log        show or follow state/toolbox.log
    help       show this overview

State lives in state/ (devices.json, reports.json, account_state.json,
toolbox.log, retrieve.lock). Logging goes to stderr and state/toolbox.log.

Only Python packages are needed here (see requirements.txt); ESP-IDF is for
building and flashing the firmware in ESP32/ and is not used by this tool.
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
import shlex
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
DEFAULT_SCAN_SECS = 15
DEFAULT_SECS = 60
DEFAULT_DEVICE = ""
MAX_BACKTRACK_SLOTS = 30
MAX_WINDOW_SLOTS = 720       # never look further back than this (24 h of slots)
AUTOSYNC_MIN_REPORTS = 3
AUTOSYNC_MIN_SLOTS = 2
AUTOSYNC_MAX_DRIFT = MAX_BACKTRACK_SLOTS
RETRIEVE_SLEEP_S = 90
PIN_LEN = 8
PIN_FAIL_MAX = 5           # firmware FM_PIN_FAIL_MAX (before a lockout)
NAME_LEN = 16              # firmware FM_NAME_LEN

MARKERS = ("PONG", "OK ", "ERR", "SLOT ", "KEY ", "STAT ", "STATUS ",
           "IDENT ", "LOCKED", "OS batt=", "OSMODE ")

# Process-wide console connection state: what 'connect' identified, so the
# menu can offer the UART commands that need a known device. Every command
# still opens its own session; nothing is held open between menu steps.
CONNECTED: dict | None = None
LOG_PREFIX = re.compile(r"^[IVDEW] \(\d+\) [^:]+: ")

# The toolbox's fake-OS bit config: what it reports when the ESP32 sends an
# OS? poll (this is a test doubles the real daemon). Set via the Debug status
# submenu; 0/False for every bit until then. Mirrors findmy-toolbox's role as
# a stand-in that answers OS? polls with these values.
_FAKE_OS = {"batt": 0, "power": 0, "user": 0, "net": 0}

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
UI_WIDTH = 74
MENU_COLS = UI_WIDTH - 4

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


ANSI_RE = re.compile(r"\033\[[0-9;]*m")
BAR_PARTS = "▏▎▍▌▋▊▉"


def text_width(text: str) -> int:
    """Columns of a string that may carry ANSI colour codes."""
    return len(ANSI_RE.sub("", text))


def pad_right(text: str, width: int) -> str:
    return text + " " * max(0, width - text_width(text))


def truncate(text: str, width: int) -> str:
    """Cut a possibly coloured string to `width` columns, keeping its codes."""
    out: list[str] = []
    seen = 0
    coloured = False
    i = 0
    while i < len(text):
        code = ANSI_RE.match(text, i)
        if code:
            out.append(code.group(0))
            coloured = True
            i = code.end()
            continue
        if seen >= width:
            if coloured:
                out.append(RESET)
            return "".join(out)
        out.append(text[i])
        seen += 1
        i += 1
    return text


def ui_bar(fraction: float, width: int = 20, color: str = GREEN,
           track: str = "░") -> str:
    """Progress bar: whole blocks, then a partial block, then the track."""
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = fraction * width
    full = int(filled)
    part = ""
    if full < width:
        cell = int((filled - full) * len(BAR_PARTS))
        if cell:
            part = BAR_PARTS[cell]
    bar = paint(color, "█" * full + part, ui=True)
    return bar + paint(DIM, track * (width - full - len(part)), ui=True)


def ui_edge(left: str, right: str, label: str = "", right_label: str = "",
            core: int = 72) -> str:
    """One horizontal window border: label on the left, right_label on the
    right, dashes filling whatever is left."""
    head = f"─ {label} " if label else ""
    tail = f" {right_label} ─" if right_label else ""
    fill = core - text_width(head) - text_width(tail)
    if fill < 1:
        tail = ""
        fill = core - text_width(head)
    if fill < 1:
        head, fill = "", core
    return left + head + "─" * fill + tail + right


def ui_frame(title: str, body: list[str], *, right_label: str = "",
             footer: str = "", width: int = UI_WIDTH,
             color: str = CYAN) -> list[str]:
    """A window around `body`: title border on top, footer border below."""
    core = width - 2
    def edge(left: str, right: str, label: str, right_label: str) -> str:
        return paint(color + BOLD,
                     ui_edge(left, right, label, right_label, core), ui=True)
    bar = paint(color + BOLD, "│", ui=True)
    out = [edge("╭", "╮", title, right_label)]
    for line in body:
        out.append(bar + pad_right(" " + truncate(line, core - 2), core - 1)
                   + " " + bar)
    out.append(edge("╰", "╯", footer, ""))
    return out


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


def prompt_int(label: str, default: int, lo: int, hi: int,
               also: tuple[int, ...] = ()) -> int:
    """Ask for an integer in lo..hi (plus any extra values accepted).

    An empty line keeps the default, and a non-terminal stdin returns the
    default without asking, so scripts and pipes behave like before.
    """
    hint = f"{lo}..{hi}" + (f" or {'/'.join(str(v) for v in also)}"
                            if also else "")
    while True:
        raw = prompt(label, str(default))
        try:
            value = int(raw)
        except ValueError:
            print(paint(RED, f"  a whole number ({hint}), please", ui=True))
            continue
        if lo <= value <= hi or value in also:
            return value
        print(paint(RED, f"  {hint}, please", ui=True))


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


def valid_name(name: str) -> bool:
    """The device name the firmware accepts: 1..NAME_LEN of [A-Za-z0-9_-]."""
    return (bool(name) and len(name) <= NAME_LEN
            and re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None)


def sanitize_name(value: str) -> str:
    """Closest legal device name for `value` (may be empty if nothing fits)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", value.strip())[:NAME_LEN]
    return cleaned.strip("-")


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
    return ":".join(f"{x:02X}" for x in bytes([b[0] | 0xC0, *b[1:6]]))


OF_TYPE = 0x12
OF_LEN = 0x19
OF_HEADER = 2
APPLE_MFR = 0x004C
OF_MFR_LEN = OF_HEADER + OF_LEN
# Every defined status bit (0-6); bit7 is the unused spare. fm_status_with_parity
# keeps bit6 as even parity over the whole byte, so it is a defined bit too.
STATUS_BITS = 0x7F


def legacy_mac(key) -> str:
    """Address ordering of the pre-fix firmware, kept only as a fingerprint.

    The first NimBLE build put `key[0] | 0b11` into the *last* displayed
    octet instead of the first, so a beacon still running that firmware is
    recognised by this string - `verify` uses it to name the byte-order bug
    instead of just reporting "no beacon".
    """
    b = key.adv_key_bytes
    return ":".join(
        f"{x:02X}" for x in bytes([*reversed(b[:5]), (b[5] & 0x3F) | 0xC0]))


def reconstruct_key(mac: str, mfr: bytes) -> bytes | None:
    """Rebuild the 28-byte advertising key the way a Find My finder does.

    The address carries `key[0]` with its top two bits forced to `0b11`, so
    those two bits have to come back out of the payload's high-bits byte:
    `key = (mfr[25] << 6 | mac[0] & 0x3F) || mac[1..5] || mfr[3..24]`.
    Same construction as findmy's
    `SeparatedOfflineFindingDevice.from_payload` and paper Table 2.
    """
    if len(mfr) != OF_MFR_LEN or mac.count(":") != 5:
        return None
    try:
        octets = bytes(int(x, 16) for x in mac.split(":"))
    except ValueError:
        return None
    if len(octets) != 6:
        return None
    start = ((mfr[25] & 0x03) << 6) | (octets[0] & 0x3F)
    return bytes([start, *octets[1:6], *mfr[3:25]])


def norm_mac(mac: str) -> str:
    return mac.replace("-", ":").upper()


def payload_matches(mfr: bytes, key) -> bool:
    if mfr is None or len(mfr) < 4:
        return False
    if mfr[0] != OF_TYPE or mfr[1] != OF_LEN:
        return False
    expected = key.of_data(status=0, hint=0)
    if len(mfr) != len(expected):
        return False
    return mfr[:2] == expected[:2] and mfr[3:] == expected[3:]


def frame_spec_checks(mac: str, mfr: bytes) -> list[tuple[str, bool, str]]:
    """Static frame checks that do not depend on which key we hold."""
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, got: str = "") -> None:
        checks.append((name, ok, got))

    add("manufacturer data under Apple id 0x004C", True, "packet selected")
    add("OF type byte is 0x12",
        len(mfr) > 0 and mfr[0] == OF_TYPE,
        f"got {mfr[0]:#04x}" if mfr else "no data")
    add("OF length byte is 0x19 (25)",
        len(mfr) > 1 and mfr[1] == OF_LEN,
        f"got {mfr[1]:#04x}" if len(mfr) > 1 else "no data")
    add(f"mfr length is {OF_MFR_LEN} (2 + 25)",
        len(mfr) == OF_MFR_LEN, f"got {len(mfr)}")

    octets = mac.split(":")
    plausible = (len(octets) == 6
                 and all(len(o) == 2 and all(c in "0123456789ABCDEF"
                                             for c in o.upper())
                         for o in octets))
    add("MAC is 6 hex octets", plausible, mac)
    if plausible:
        first = int(octets[0], 16)
        add("random static address: top two bits are 0b11",
            first & 0xC0 == 0xC0, f"first octet {first:#04x}")
    if len(mfr) == OF_MFR_LEN:
        add("status byte uses only defined bits (0x7F)",
            mfr[2] & ~STATUS_BITS == 0, f"status {status_decode(mfr[2])}")
        add("status byte parity OK (even over the byte)",
            status_parity_ok(mfr[2]),
            f"status {status_decode(mfr[2])} parity bad")
        add("hint byte is 0x00", mfr[26] == 0, f"hint {mfr[26]:#04x}")
    return checks


def key_spec_checks(mac: str, mfr: bytes, key, dev_id: str,
                    slot: int) -> list[tuple[str, bool, str]]:
    """Frame-vs-`devices.json` checks: the ones the byte order bug hides in."""
    checks: list[tuple[str, bool, str]] = []
    want = key.adv_key_bytes

    def add(name: str, ok: bool, got: str = "") -> None:
        checks.append((name, ok, got))

    if len(mfr) != OF_MFR_LEN:
        add("payload key suffix == devices.json key[6:28]", False,
            f"frame is {len(mfr)} bytes, not {OF_MFR_LEN}")
        return checks

    suffix = mfr[3:25]
    add(f"payload suffix == {dev_id} slot {slot} key[6:28]",
        suffix == want[6:28], suffix.hex())
    add("payload high-bits byte == key[0] >> 6",
        mfr[25] == want[0] >> 6,
        f"frame {mfr[25]:#04x}, key {want[0] >> 6:#04x}")

    rebuilt = reconstruct_key(mac, mfr)
    add("reconstructed public key == devices.json key",
        rebuilt == want,
        f"rebuilt {rebuilt.hex() if rebuilt else '?'}")

    add("beacon_mac() == advertised MAC", beacon_mac(key) == norm_mac(mac),
        f"{beacon_mac(key)} vs {norm_mac(mac)}")
    add("beacon_mac() == findmy mac_address (spec cross-check)",
        beacon_mac(key) == norm_mac(key.mac_address),
        f"{beacon_mac(key)} vs {key.mac_address}")
    add("findmy of_data() agrees with the frame body",
        payload_matches(mfr, key), "type/len/key/high-bits/hint")
    return checks


def status_parity_ok(status_byte: int) -> bool:
    """Even parity over the whole status byte (matches the firmware's bit 6)."""
    return (bin(status_byte).count("1") & 1) == 0


def status_decode(status_byte: int) -> str:
    bits = []
    bits.append("UNLOCKED" if status_byte & 0x01 else "locked")
    bits.append("CONFIG" if status_byte & 0x02 else "sleep-cycle")
    if status_byte & 0x04:
        bits.append("batt")
    if status_byte & 0x08:
        bits.append("power")
    if status_byte & 0x10:
        bits.append("user")
    if status_byte & 0x20:
        bits.append("net")
    bits.append("parity=" + ("ok" if status_parity_ok(status_byte) else "BAD"))
    return f"0x{status_byte:02x} ({'+'.join(bits)})"


def status_color(status_byte: int | None) -> str:
    """Green = normal field state, yellow = console open, red = config mode."""
    if status_byte is None:
        return DIM
    if status_byte & 0x02:
        return RED
    if status_byte & 0x01:
        return YELLOW
    return GREEN


def _bit_color(name: str, value: bool) -> str:
    """Per-status-bit colour for the monitor table."""
    if not value:
        return GREEN
    if name == "config":
        return RED
    if name == "batt":
        return MAGENTA
    if name == "unlocked":
        return YELLOW
    return CYAN  # power/user/net


def report_status(d: dict) -> tuple[int | None, str]:
    """(status byte, decoded text) of an archived report record."""
    raw = d.get("status")
    if not isinstance(raw, int):
        return None, ""
    return raw, d.get("status_text") or status_decode(raw)


def status_summary(reports: list[dict]) -> dict | None:
    """Newest advertisement status in an archive, for reports.json."""
    known = [r for r in reports if isinstance(r.get("status"), int)
             and r.get("time")]
    if not known:
        return None
    newest = max(known, key=lambda r: r["time"])
    raw, text = report_status(newest)
    return {"status": raw, "text": text, "slot": newest.get("slot"),
            "time": newest["time"]}


STATUS_BIT_NAMES = ((0x01, "unlocked"),
                    (0x02, "config"),
                    (0x04, "batt"),
                    (0x08, "power"),
                    (0x10, "user"),
                    (0x20, "net"))


def status_bit_history(reports: list[dict], now) -> list[dict]:
    """Per status bit: its current value and how long it has held it.

    Walks the time-ordered reports so each bit reports the last moment it
    changed. A bit with no observable change in the archive only has a
    lower bound (the newest report), so its duration is marked with
    ``certain=False``.
    """
    known = []
    for r in reports:
        if not isinstance(r.get("status"), int):
            continue
        stamp = report_time(r)
        if stamp is not None:
            known.append((stamp, r["status"]))
    by_time = sorted(known, key=lambda kv: kv[0])
    cur = 0
    last_change = {mask: by_time[0][0] if by_time else None for mask, _ in
                   STATUS_BIT_NAMES}
    changed = {mask: False for mask, _ in STATUS_BIT_NAMES}
    for stamp, byte in by_time:
        for mask, _ in STATUS_BIT_NAMES:
            val = bool(byte & mask)
            if val != bool(cur & mask):
                cur = (cur & ~mask) | (mask if val else 0)
                last_change[mask] = stamp
                changed[mask] = True
    rows = []
    for mask, name in STATUS_BIT_NAMES:
        val = bool(cur & mask)
        since = last_change[mask]
        if since is None:
            seconds = None
        else:
            seconds = (now - since).total_seconds()
        rows.append({"mask": mask, "name": name, "value": val,
                     "seconds": seconds, "certain": changed[mask]})
    return rows


def fmt_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    if s < 86400:
        h, m = divmod(s, 3600)
        return f"{h}h{m // 60:02d}min"
    d, rem = divmod(s, 86400)
    h, _m = divmod(rem, 3600)
    return f"{d}d{h:02d}h"


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


def report_time(report) -> datetime | None:
    """Timestamp of an archived report, or None if the entry is unusable."""
    if not isinstance(report, dict):
        return None
    try:
        return parse_time(report["time"])
    except (KeyError, TypeError, ValueError):
        return None


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
            if raw.strip() == "OS?":
                self.s.write(self._os_fake_reply())
                self.s.flush()
                log(self.channel, "< auto-answered OS? poll with fake bits",
                    logging.DEBUG)
                continue
            reply = reply_of(raw)
            if reply is not None:
                log(self.channel, f"< {reply}", logging.DEBUG)
                return reply
        log(self.channel,
            f"< (no reply to {line.strip() or '<empty>'} in {wait:g}s)",
            logging.DEBUG)
        return "TIMEOUT"

    def _os_fake_reply(self, reset: bool = False) -> bytes:
        f = _FAKE_OS
        payload = (f"OK OS batt={f['batt']} power={f['power']} "
                   f"user={f['user']} net={f['net']}")
        if reset:
            payload += " reset=1"
        return (payload + "\r\n").encode()

    def respond_to_next_poll(self, timeout: float = 12.0,
                             reset: bool = False) -> bool:
        """Block until the ESP32 sends an OS? poll and answer it. With
        ``reset`` the reply carries ``reset=1``, which makes the beacon reboot
        into its debug window (how you wake a sleeping device for flashing).
        False on timeout (device not polling, e.g. still in direct mode or
        asleep)."""
        prev = self.s.timeout
        self.s.timeout = 0.05
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = self.s.readline().decode(errors="replace")
                if "Guru Meditation" in raw or raw.startswith("Backtrace:"):
                    log(self.channel, f"device crashed: {raw.strip()}",
                        logging.ERROR)
                if raw.strip() == "OS?":
                    self.s.write(self._os_fake_reply(reset=reset))
                    self.s.flush()
                    log(self.channel,
                        "< auto-answered OS? poll with " +
                        ("reset=1" if reset else "fake bits"),
                        logging.DEBUG)
                    return True
        finally:
            self.s.timeout = prev
        return False

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
           f"{str(d.get('dbg_sec', '?')):>5}  "
           f"{d.get('paired_at', '?'):<20} {device_pin(d)}")
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
    if existing and args.force and sys.stdin.isatty() and not args.yes:
        ui(f"'{dev_id}' already has keys - pairing again replaces them on the "
           f"device and in {DEVICES_JSON.name}", YELLOW)
        if prompt(f"type 'pair' to re-key '{dev_id}'", "n").lower() != "pair":
            ui("cancelled - nothing was changed", DIM)
            log(channel, "re-pair cancelled")
            return 0

    port = resolve_port(args.port, existing)
    pin = unlock_pin_for(existing, args.pin)
    if not valid_name(dev_id):
        raise UserError(f"--id must be a device name: 1..{NAME_LEN} "
                        f"characters from [A-Za-z0-9_-], got '{dev_id}'")

    ask = sys.stdin.isatty() and not args.yes
    if args.adv_ms:
        adv_ms = args.adv_ms
    else:
        adv_ms = (prompt_int("advertisement period in ms", 2000, 200, 60000)
                  if ask else 2000)
    if args.rot_sec:
        rot_sec = args.rot_sec
    else:
        rot_sec = (prompt_int("key rotation period in s", 120, 1, 86400)
                   if ask else 120)
    if args.dbg_sec is not None:
        dbg_sec = args.dbg_sec
    else:
        # 0 is not allowed: a boot with no console window is rejected.
        dbg_sec = (prompt_int("console countdown in s", 86400, 60, 86400)
                   if ask else 86400)

    # From a connection the console stays open (the menu keeps working on
    # it); a plain CLI run locks the device again when it is done.
    keep_open = CONNECTED is not None and CONNECTED.get("port") == port
    log(channel, f"pairing '{dev_id}' on {port}")
    master = secrets.token_bytes(28)
    skn = secrets.token_bytes(32)
    b64 = lambda x: base64.b64encode(x).decode()

    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    auto_lock=not keep_open,
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

        # The device learns its own name here: IDENT? then tells every host
        # which beacon it is, locked console or not.
        reply = beacon.cmd(f"NAME {dev_id}")
        if not reply.startswith("OK NAME"):
            raise UserError(f"the console refused the device name: {reply} - "
                            f"is the firmware up to date? (idf.py flash)")

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
        if CONNECTED is not None and CONNECTED.get("port") == port:
            CONNECTED.update({"id": dev_id, "name": dev_id, "paired": True,
                              "pin": new_pin})
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


def _os_channel() -> str:
    return "STATUS-DEBUG"


def cmd_status_debug(args) -> int:
    """Interactive Debug > Status submenu: toggle the 4 OS status bits
    (batt/power/user/net) with [x] checkboxes, then write them to the ESP32
    either by push (OSSTATE, immediate) or by letting the ESP32 poll them
    (we answer its OS? with the selected bits). Bits 0/1 (unlocked/config)
    and bit6 (parity) are ESP32-owned and are never offered here."""
    p = lambda color, text: paint(color, text, ui=True)
    channel = _os_channel()
    data = load_devices()
    dev_id = pick_device(data, args.id)
    device = find_device(data, dev_id)
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    bits = {"batt": 0, "power": 0, "user": 0, "net": 0}
    order = ("batt", "power", "user", "net")

    def show() -> None:
        print("\n  Status bit debug (ESP32-owned bits 0/1 and parity are not "
              "exposed)")
        for i, name in enumerate(order, 1):
            mark = "[x]" if bits[name] else "[ ]"
            print(f"   {i}) {mark} {name}")
        print("   w) write to the beacon")
        print("   q) cancel / back")

    show()
    try:
        while True:
            raw = input(p(BOLD, "> ")).strip().lower()
            if raw in ("q", "quit", "back", "0"):
                print(p(DIM, "  (cancelled - nothing written)"))
                return 0
            if raw in ("w", "write"):
                break
            if raw.isdigit():
                n = int(raw)
                if 1 <= n <= len(order):
                    name = order[n - 1]
                    bits[name] = 1 - bits[name]
                    show()
                    continue
            for name in order:
                if raw == name:
                    bits[name] = 1 - bits[name]
                    show()
                    break
            else:
                print(p(YELLOW, "  pick a bit (1-4), 'w' to write, 'q' to quit"))
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write(RESET + "\n")
        return 0

    try:
        write = input(p(BOLD, "  write method: [p]ush, [o]s-poll, [r]eset? "))
        write = write.strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write(RESET + "\n")
        return 0
    want_poll = write in ("o", "os", "poll", "os-poll")
    want_reset = write in ("r", "reset", "reboot")

    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        if want_reset:
            # Answer the beacon's next OS? poll with reset=1 so it reboots
            # into its debug window, making it flash-able again after sleep.
            beacon.cmd("OSMODE poll")
            ui("  waiting for the ESP32's next OS? poll to answer with reset=1...")
            if not beacon.respond_to_next_poll(timeout=12.0, reset=True):
                raise UserError("no OS? poll within 12 s - is the firmware in "
                                "poll mode and paired?")
            ui("  reset requested - the beacon should reboot into its debug "
               "window now")
            return 0
        if want_poll:
            # The ESP32 latches these bits from its OS? polls, so we take over
            # the fake-daemon role and answer the next poll with them.
            _FAKE_OS.update(bits)
            beacon.cmd("OSMODE poll")
            ui("  waiting for the ESP32's next OS? poll to answer with the "
               "selected bits...")
            if not beacon.respond_to_next_poll(timeout=12.0):
                raise UserError("no OS? poll within 12 s - is the firmware in "
                                "poll mode and paired?")
            got = beacon.cmd("OS?")
            m = re.search(r"batt=(\d+) power=(\d+) user=(\d+) net=(\d+)", got)
            if not m:
                raise UserError(f"could not read the latched OS state: {got}")
            latched = {"batt": int(m.group(1)), "power": int(m.group(2)),
                       "user": int(m.group(3)), "net": int(m.group(4))}
            log(channel, f"poll-latched OS bits: {latched}")
            ui(f"  ESP32 latched OS bits: {latched}")
            return 0
        # push: send OSSTATE directly to the ESP32
        beacon.cmd("OSMODE direct")
        reply = beacon.cmd(f"OSSTATE {bits['batt']} {bits['power']} "
                           f"{bits['user']} {bits['net']}")
        if not reply.startswith("OK OSSTATE"):
            raise UserError(f"OSSTATE refused: {reply}")
        _FAKE_OS.update(bits)
        log(channel, f"pushed OS bits: {bits} -> {reply}")
        ui(f"  pushed OS bits to the beacon: {bits}")
    finally:
        beacon.close()
    return 0


def cmd_reset_poll(args) -> int:
    """Wake a sleeping beacon for flashing.

    A beacon that entered its sleep cycle has no console and its on-chip
    USB-Serial-JTAG drops off USB, so it can't be reached or flashed. This
    puts it in poll mode and answers its next OS? status poll with ``reset=1``,
    which makes it reboot into its debug window (min 60 s) where the console
    is back and the correct flash reset works again.
    """
    channel = _os_channel()
    data = load_devices()
    dev_id = pick_device(data, args.id)
    device = find_device(data, dev_id)
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    log(channel, f"'{dev_id}': waiting for an OS? poll to answer with reset=1")
    ui(f"  waiting for the ESP32's next OS? poll on {port} to answer with "
       f"reset=1...")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        beacon.cmd("OSMODE poll")
        if not beacon.respond_to_next_poll(timeout=15.0, reset=True):
            raise UserError("no OS? poll within 15 s - is the beacon awake and "
                            "paired? (it must be in a console window to poll)")
        ui("  reset=1 delivered - the beacon is rebooting into its debug "
           "window")
    finally:
        beacon.close()
    return 0


def cmd_status(args) -> int:
    """Print every config setting the firmware reports via STATUS?."""
    channel = "STATUS"
    data = load_devices()
    dev_id = pick_device(data, args.id)
    device = find_device(data, dev_id)
    port = resolve_port(args.port, device)
    pin = unlock_pin_for(device, args.pin)

    log(channel, f"'{dev_id}': asking for STATUS? on {port}")
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    pin_from_flag=args.pin is not None)
    try:
        beacon.after_open(reset=args.reset)
        reply = beacon.cmd("STATUS?")
        if not reply.startswith("STATUS "):
            raise UserError(reply)
        os_reply = beacon.cmd("OS?")
    finally:
        beacon.close()

    kv: dict[str, str] = {}
    for token in reply.split()[1:]:
        if "=" in token:
            key, _, val = token.partition("=")
            kv[key] = val

    ui(f"\n{'setting':<34} value", DIM)
    ui(f"  {'paired':<32} {'yes' if kv.get('paired') == '1' else 'no'}")
    ui(f"  {'slot index':<32} {kv.get('slot', '?')}")
    ui(f"  {'debug logging':<32} {'on' if kv.get('debug') == '1' else 'off'}")
    ui(f"  {'advertisement period (adv_ms)':<32} {kv.get('adv_ms', '?')} ms")
    ui(f"  {'key rotation period (rot_sec)':<32} {kv.get('rot_sec', '?')} s")
    ui(f"  {'console countdown (dbg_sec)':<32} {kv.get('dbg_sec', '?')} s")
    ui("")
    om = re.search(r"batt=(\d) power=(\d) user=(\d) net=(\d)", os_reply)
    if om:
        ui("  OS status bits (latched / reported):", DIM)
        for name, val in (("battery<20%", om.group(1)), ("powered on",
                          om.group(2)), ("user logged in", om.group(3)),
                          ("internet up", om.group(4))):
            ui(f"  {'  ' + name:<32} {'on' if val == '1' else 'off'}")
    else:
        ui(f"  OS status bits: (no OS? reply: {os_reply})", DIM)
    ui("")
    return 0


async def ble_scan(window: float) -> dict[str, dict]:
    from bleak import BleakScanner
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
        total = len(self.results)
        passed = total - len(failed)
        ui("")
        ui(f"{passed}/{total} checks passed  "
           + ui_bar(passed / total if total else 0.0, 24,
                    GREEN if not failed else YELLOW),
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
        ident = beacon.cmd("IDENT?")
        suite.check("IDENT? is answered while locked",
                    ident.startswith("IDENT name="), ident)
        reply = beacon.cmd("IDENT? extra")
        suite.check("IDENT? with extra args -> ERR ARGS",
                    reply == "ERR ARGS", reply)
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
        # Set a long debug-window so the suite does not race the 60s countdown.
        # This changes the active dbg_sec timer to 86400s (~24h), keeping the
        # beacon awake for the entire test.  Without this the 60s window often
        # expires between the unlock and the first PING, causing failures.
        reply = beacon.cmd("CONFIG 2000 120 86400")
        suite.check("CONFIG set dbg_sec=86400 -> OK CONFIG", reply.startswith("OK CONFIG"), reply)

        suite.step("P1: protocol and malformed input")
        reply = beacon.cmd("PING")
        suite.check("PING -> PONG fw=4", reply.startswith("PONG fw=4"), reply)
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
        suite.check("dbg_sec below minimum clamps up to 60",
                    "dbg_sec=60" in reply, reply)
        reply = beacon.cmd("CONFIG 2000 120 0")
        suite.check("dbg_sec 0 is not allowed (clamps to the default)",
                    "dbg_sec=86400" in reply, reply)
        reply = beacon.cmd("CONFIG 2000 120 600")
        suite.check("config restored to defaults",
                    reply.startswith("OK CONFIG adv_ms=2000 rot_sec=120 dbg_sec=600"),
                    reply)

        suite.step("P1b: OS status bits")
        reply = beacon.cmd("OS?")
        suite.check("OS? getter -> OS batt=.. power=.. user=.. net=..",
                    re.match(r"OS batt=\d power=\d user=\d net=\d", reply)
                    is not None, reply)
        reply = beacon.cmd("OSMODE junk")
        suite.check("OSMODE bad value -> ERR ARGS",
                    reply.startswith("ERR ARGS"), reply)
        reply = beacon.cmd("OSMODE")
        suite.check("OSMODE with no arg reports current mode",
                    reply.startswith("OSMODE "), reply)
        reply = beacon.cmd("OSMODE direct")
        suite.check("OSMODE direct -> OK OSMODE direct",
                    reply == "OK OSMODE direct", reply)
        reply = beacon.cmd("OSSTATE 1 0 1 0")
        suite.check("OSSTATE pushes OS bits",
                    reply.startswith("OK OSSTATE batt=1 power=0 user=1 net=0"),
                    reply)
        reply = beacon.cmd("OSSTATE 1 1 1 1")
        suite.check("OSSTATE all on", "batt=1 power=1 user=1 net=1" in reply,
                    reply)
        reply = beacon.cmd("OSSTATE 2 0 0 0")
        suite.check("OSSTATE batt out of range -> ERR ARGS",
                    reply.startswith("ERR ARGS"), reply)
        reply = beacon.cmd("OSSTATE 0 1")
        suite.check("OSSTATE too few args -> ERR ARGS",
                    reply.startswith("ERR ARGS"), reply)
        reply = beacon.cmd("OSSTATE 0 1 0 1 trailing")
        suite.check("OSSTATE extra arg -> ERR ARGS",
                    reply.startswith("ERR ARGS"), reply)
        reply = beacon.cmd("OSMODE poll")
        suite.check("OSMODE poll -> OK OSMODE poll",
                    reply == "OK OSMODE poll", reply)
        reply = beacon.cmd("OSSTATE 0 0 0 0")
        suite.check("OSSTATE back to all off",
                    reply.startswith("OK OSSTATE"), reply)

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
        # Set a long debug-window so the suite does not race the 60s countdown.
        # This changes the active dbg_sec timer to 86400s (~24h), keeping the
        # beacon awake for the entire test.  Without this the 60s window often
        # expires between the unlock and the first PING, causing failures.
        reply = beacon.cmd("CONFIG 2000 120 86400")
        suite.check("CONFIG set dbg_sec=86400 -> OK CONFIG", reply.startswith("OK CONFIG"), reply)

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
        # Set a long debug-window so the suite does not race the 60s countdown.
        # This changes the active dbg_sec timer to 86400s (~24h), keeping the
        # beacon awake for the entire test.  Without this the 60s window often
        # expires between the unlock and the first PING, causing failures.
        reply = beacon.cmd("CONFIG 2000 120 86400")
        suite.check("CONFIG set dbg_sec=86400 -> OK CONFIG", reply.startswith("OK CONFIG"), reply)
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

        suite.step("P3b: the device name (IDENT?/NAME)")
        reply = beacon.cmd("IDENT?")
        suite.check("name erased by WIPE -> IDENT name=- paired=0",
                    reply == "IDENT name=- paired=0", reply)
        name = dev_id if valid_name(dev_id) else (
            sanitize_name(dev_id) or "test-beacon")
        if name != dev_id:
            log(channel, f"'{dev_id}' is no device name - using '{name}' on "
                         f"the console", logging.WARNING)
        reply = beacon.cmd(f"NAME {name}")
        suite.check(f"NAME {name} -> OK NAME", reply == f"OK NAME {name}", reply)
        reply = beacon.cmd("IDENT?")
        suite.check("IDENT? reports the new name",
                    reply == f"IDENT name={name} paired=0", reply)
        reply = beacon.cmd("NAME")
        suite.check("NAME without a value -> ERR ARGS",
                    reply == "ERR ARGS", reply)
        reply = beacon.cmd("NAME two tokens")
        suite.check("NAME with extra args -> ERR ARGS",
                    reply == "ERR ARGS", reply)
        reply = beacon.cmd("NAME has!invalid")
        suite.check("NAME outside [A-Za-z0-9_-] -> ERR ARGS",
                    reply == "ERR ARGS", reply)
        reply = beacon.cmd("NAME " + "x" * (NAME_LEN + 1))
        suite.check(f"NAME longer than {NAME_LEN} -> ERR ARGS",
                    reply == "ERR ARGS", reply)
        reply = beacon.cmd("IDENT?")
        suite.check("a refused NAME leaves the name alone",
                    reply == f"IDENT name={name} paired=0", reply)

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
        # Set a long debug-window so the suite does not race the 60s countdown.
        # This changes the active dbg_sec timer to 86400s (~24h), keeping the
        # beacon awake for the entire test.  Without this the 60s window often
        # expires between the unlock and the first PING, causing failures.
        reply = beacon.cmd("CONFIG 2000 120 86400")
        suite.check("CONFIG set dbg_sec=86400 -> OK CONFIG", reply.startswith("OK CONFIG"), reply)
        reply = beacon.cmd("STATUS?")
        suite.check("countdown setting survived", "dbg_sec=60" in reply, reply)
        reply = beacon.cmd("OSSTATE 1 0 0 0")
        suite.check("OSSTATE battery bit can be set",
                    reply.startswith("OK OSSTATE batt=1"), reply)
        reply = beacon.cmd("OS?")
        suite.check("latched OS state reports battery after being set",
                    "batt=1" in reply, reply)
        resetb = open_beacon(port, pin_final, unlock=False, reset=True,
                             ready_timeout=30, dev_id=dev_id)
        try:
            # OS? is the one status getter answered while locked.
            bbit = resetb.cmd("OS?")
            suite.check("OS battery bit persisted past a reset",
                        "batt=1" in bbit, bbit)
            resetb.unlock(pin_final)  # OSSTATE needs an unlocked console
            resetb.cmd("OSSTATE 0 0 0 0")
            suite.check("OS battery bit cleared again",
                        "batt=0" in resetb.cmd("OS?"), resetb.cmd("OS?"))
        finally:
            resetb.close()
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
    ui(f"awake duty      : {duty * 100:.2f} %  "
       + ui_bar(min(1.0, duty / 0.05), 24, GREEN)
       + paint(DIM, "   0..5% full-scale", ui=True), GREEN)

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
    tmp = REPORTS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(REPORTS_JSON)


def report_to_dict(r, slot: int, key) -> dict:
    return {
        "slot": slot,
        "type": key.key_type.name.lower(),
        "time": r.timestamp.isoformat(),
        "latitude": r.latitude,
        "longitude": r.longitude,
        "accuracy_m": r.horizontal_accuracy,
        "confidence": r.confidence,
        "status": r.status,
        "status_text": status_decode(r.status),
        "status_parity": "ok" if status_parity_ok(r.status) else "bad",
        "key_hash": key.hashed_adv_key_b64,
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

    resume = state.get("fetched_upto")
    if resume is not None:
        if start > resume:
            log(channel, f"{dev_id}: worker gap - fetching the slots nobody "
                         f"has queried since slot {resume}")
        start = min(start, resume)
    start = max(start, max_i - MAX_WINDOW_SLOTS)
    start = max(0, min(start, max_i))

    log(channel, f"{dev_id}: query window slots {start}..{max_i} "
                 f"({max_i - start + 1} request(s), archive has "
                 f"{len(state['reports'])} report(s))")

    fresh = []
    failed = []
    for slot in range(max_i, start - 1, -1):
        keys = {}
        for key in acc.keys_at(slot):
            keys.setdefault(key.hashed_adv_key_b64, key)
        try:
            raw = await account.fetch_raw_reports([(list(keys.keys()), [])])
        except EmptyResponseError:
            failed.append(slot)
            log(channel, f"{dev_id}: slot {slot}: Apple answered with an "
                         f"empty body - retrying this slot next cycle",
                 logging.WARNING)
            raw = []

        added = 0
        for r in raw:
            key = keys.get(base64.b64encode(r.hashed_adv_key_bytes).decode())
            if key is None:
                continue
            r.decrypt(key)
            d = report_to_dict(r, slot, key)
            k = report_key(d)
            if k not in known:
                known.add(k)
                state["reports"].append(d)
                fresh.append(d)
                added += 1

        if raw:
            log(channel, f"{dev_id}: slot {slot}: {len(raw)} report(s) on "
                         f"server, {added} new")
        prev = state.get("last_fetched_slot")
        state["last_fetched_slot"] = slot if prev is None else max(prev, slot)
        save_results(results)
        await asyncio.sleep(0.2)

    state["fetched_upto"] = min(failed) if failed else max_i
    state["reports"].sort(key=lambda d: d["time"])
    state["status"] = status_summary(state["reports"])
    save_results(results)
    return fresh


async def retrieve_doctor(apple_id: str | None, channel: str) -> int:
    """Re-ask Apple for report keys we already hold.

    A working session hands those reports back; a banned or throttled one
    answers with nothing, which is otherwise indistinguishable from "no
    beacon was ever seen".
    """
    from findmy.errors import EmptyResponseError

    results = load_results()
    wanted = {}
    times = []
    for dev_id, state in results.items():
        if not isinstance(state, dict):
            continue
        for rep in state.get("reports") or []:
            if not isinstance(rep, dict):
                continue
            h = rep.get("key_hash")
            if h:
                wanted.setdefault(h, set()).add(dev_id)
                if isinstance(rep.get("time"), str) and rep["time"]:
                    times.append(rep["time"])
    if not wanted:
        log_error(channel, "no hashed keys in the archive - fetch one report "
                           "first, then --doctor can act as a positive control")
        ui("no hashed keys archived yet (reports fetched before this build).")
        ui("Fetch one report first, then re-run retrieve --doctor.")
        return 1

    times.sort()
    if times:
        log(channel, f"positive control over {len(wanted)} known report key(s), "
                     f"archive spans {times[0]} .. {times[-1]}")
    account = await apple_login(apple_id, channel)
    if account is None:
        return 1

    seen = set()
    empty = False
    try:
        hashes = sorted(wanted)
        for i in range(0, len(hashes), 50):
            try:
                raw = await account.fetch_raw_reports([(hashes[i:i + 50], [])])
            except EmptyResponseError:
                empty = True
                raw = []
            for r in raw:
                seen.add(base64.b64encode(r.hashed_adv_key_bytes).decode())
            await asyncio.sleep(0.2)
    finally:
        try:
            await account.close()
        except Exception:
            pass

    devices = sorted({d for devs in wanted.values() for d in devs})
    if seen:
        ui(f"PASS: Apple returned {len(seen)} of {len(wanted)} known report "
           f"key(s) for {', '.join(devices)}")
        ui("  the session can read reports - the account is not banned")
        return 0
    if empty:
        ui("INCONCLUSIVE: Apple answered with an empty body (known server "
           "side hiccup) - run this again")
        return 1
    stamps = []
    for stamp in times:
        try:
            stamps.append(parse_time(stamp))
        except ValueError:
            continue
    if stamps and datetime.now(timezone.utc) - max(stamps) > timedelta(days=7):
        ui("INCONCLUSIVE: every archived report is older than the 7 day "
           "server window - fetch a fresh report first")
        return 1
    ui(f"FAIL: Apple returned none of the {len(wanted)} known report key(s)")
    ui("  reports this fresh should still be on the server, so either the "
       "account")
    ui("  cannot read reports any more, or the beacon stopped being seen")
    return 1


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


def session_account_name() -> str | None:
    """Apple ID of the saved session (None when there is no session)."""
    if not SESSION_FILE.exists():
        return None
    try:
        account = json.loads(SESSION_FILE.read_text()).get("account", {})
    except (OSError, json.JSONDecodeError):
        return None
    return account.get("info", {}).get("account_name") or account.get(
        "username")


def print_new_report(dev_id: str, d: dict) -> None:
    ui(f"  NEW  [slot {d['slot']} ({d['type']})] {dev_id}")
    ui(f"    Time:       {d['time']}")
    ui(f"    Latitude:   {d['latitude']}")
    ui(f"    Longitude:  {d['longitude']}")
    ui(f"    Accuracy:   {d['accuracy_m']} m")
    ui(f"    Confidence: {d['confidence']}")
    _raw, status_text = report_status(d)
    if status_text:
        ui(f"    Status:     {status_text}")


def model_slot_index(dev: dict, when: datetime) -> int:
    """Chain index the clock model in devices.json gives to a moment.

    The same rule findmy's `get_max_index` uses, which is what every query
    window is built from.
    """
    if dev.get("slot_synced_at") and dev.get("last_known_slot") is not None:
        aligned_at = parse_time(dev["slot_synced_at"])
        aligned_index = int(dev["last_known_slot"])
    else:
        aligned_at = parse_time(dev["paired_at"])
        aligned_index = 0
    if when <= aligned_at:
        return aligned_index
    ss = int(dev.get("slot_seconds", 120))
    return aligned_index + int((when - aligned_at) // timedelta(seconds=ss))


def auto_slot_sync(devices: list[dict], results: dict, channel: str) -> int:
    """Fix a device's stored slot alignment from its own archived reports.

    A report's `time` is when a finder recorded the beacon, so it is ground
    truth for the moment the device advertised the key stored as the report's
    `slot` label, while the clock model claims a chain index for that same
    moment. The two only differ when the model has drifted away from the
    device counter, and then by the same offset for every report. The stored
    alignment is rewritten (`sync_method: "report"`) once enough reports over
    enough slots agree on that offset; anything weaker is only warned about.

    Device entries are mutated in place - the caller saves them. Returns the
    number of devices corrected.
    """
    now = datetime.now(timezone.utc)
    corrected = 0
    for dev in devices:
        dev_id = dev.get("id", "?")
        state = results.get(dev_id)
        if not isinstance(state, dict):
            continue
        reports = state.get("reports")
        if not isinstance(reports, list):
            continue
        if len(reports) < AUTOSYNC_MIN_REPORTS:
            continue
        try:
            aligned_at = (parse_time(dev["slot_synced_at"])
                          if dev.get("slot_synced_at")
                          else parse_time(dev["paired_at"]))
        except (KeyError, TypeError, ValueError):
            log(channel, f"{dev_id}: no usable alignment time - auto sync "
                         f"skipped", logging.WARNING)
            continue

        votes: dict[int, list[int]] = {}
        too_far = 0
        for r in reports:
            if not isinstance(r, dict):
                continue
            if r.get("type", "primary") != "primary":
                continue
            stamp = report_time(r)
            label = r.get("slot")
            if stamp is None or not isinstance(label, int):
                continue
            if stamp < aligned_at:
                continue
            delta = model_slot_index(dev, stamp) - label
            if abs(delta) > AUTOSYNC_MAX_DRIFT:
                too_far += 1
                continue
            votes.setdefault(delta, []).append(label)

        if too_far:
            log(channel, f"{dev_id}: {too_far} archived report(s) sit more "
                         f"than {AUTOSYNC_MAX_DRIFT} slot(s) from the slot "
                         f"model - run 'sync' or 'sync-ble'", logging.WARNING)
        if not votes:
            continue
        total = sum(len(v) for v in votes.values())
        delta, hits = max(votes.items(), key=lambda kv: len(kv[1]))
        slots = set(hits)
        if delta == 0:
            continue
        if (len(hits) < AUTOSYNC_MIN_REPORTS or len(slots) < AUTOSYNC_MIN_SLOTS
                or len(hits) * 2 <= total):
            log(channel, f"{dev_id}: slot model is {delta:+d} in {len(hits)}/"
                         f"{total} archived report(s) over {len(slots)} "
                         f"slot(s) - too little agreement to correct it",
                logging.WARNING)
            continue
        current = model_slot_index(dev, now)
        target = current - delta
        if target < 0:
            log(channel, f"{dev_id}: auto sync would move slot {current} "
                         f"back by {delta} below zero - ignored",
                logging.WARNING)
            continue
        dev["slot_synced_at"] = now.isoformat()
        dev["last_known_slot"] = target
        dev["sync_method"] = "report"
        corrected += 1
        where = "behind" if delta > 0 else "ahead of"
        log(channel, f"{dev_id}: auto slot sync - device counter {abs(delta)} "
                     f"slot(s) {where} the clock model ({len(hits)} report(s) "
                     f"over {len(slots)} slot(s)), slot {current} -> {target}",
            logging.WARNING)
        ui(f"'{dev_id}': auto slot sync {delta:+d} - now at slot {target}",
           YELLOW)
    return corrected


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

    results = load_results()
    if auto_slot_sync(devices, results, channel):
        save_devices(data)

    accessories = {d["id"]: make_accessory(d) for d in devices}
    for dev_id, acc in accessories.items():
        log(channel, f"'{dev_id}': paired {acc.paired_at}, "
                     f"slot {acc._alignment_index} at last sync, "
                     f"{int(acc.interval.total_seconds())}s slots")

    account = await apple_login(apple_id, channel)
    if account is None:
        return 1

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


def retrieve_start_bg(quiet: bool = False) -> int:
    channel = "WORKER"
    STATE_DIR.mkdir(exist_ok=True)
    existing = retrieve_running_info()
    if existing:
        log(channel, f"retrieval worker already running (pid {existing['pid']})")
        if not quiet:
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
        if not quiet:
            ui(f"retrieval worker started (pid {worker}), "
               f"logs in {LOG_FILE.name}")
            ui("  use 'retrieve --status', 'retrieve --follow' "
               "or 'retrieve --stop'")
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


def retrieve_follow(lines: int = 50, filtered: bool = True,
                    device: str | None = None) -> int:
    channel = "RETRIEVE"
    if not LOG_FILE.exists():
        log_error(channel, f"{LOG_FILE} does not exist yet")
        return 1

    def keep(line: str) -> bool:
        if not filtered:
            return True
        if "[RETRIEVE" not in line and "[WORKER " not in line:
            return False
        return not device or device in line

    content = LOG_FILE.read_text(errors="replace").splitlines()
    shown = [l for l in content if keep(l)][-lines:]
    for line in shown:
        print(line, flush=True)
    print(paint(DIM, f"following {LOG_FILE.name} ({len(shown)} line(s), "
                     "Ctrl-C to stop)", ui=True), flush=True)
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
    if args.restart:
        rc = retrieve_stop()
        if rc:
            return rc
        return retrieve_start_bg()
    if args.follow:
        return retrieve_follow(args.lines, device=args.device)
    if args.bg:
        return retrieve_start_bg()
    if args.doctor:
        log(channel, "checking that known reports still come back")
        return asyncio.run(retrieve_doctor(args.apple_id, channel))

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
    info = retrieve_running_info()
    if not info or not info["alive"]:
        if not session_account_name():
            log_error(channel, "no saved Apple session - run 'retrieve' once "
                               "to log in, then 'watch' again")
            return 1
        log(channel, "watch: no retrieval worker, starting one")
        ui("starting the retrieval worker ...")
        if retrieve_start_bg(quiet=True) != 0:
            info = retrieve_running_info()
            if not info or not info["alive"]:
                log_error(channel, "could not start the retrieval worker")
                return 1
    log(channel, f"watch: following the worker log "
                 f"(last {args.lines} line(s))")
    ui("following the retrieval worker - Ctrl-C to stop", DIM)
    return retrieve_follow(args.lines, filtered=True, device=args.device)


def render_monitor(device_id: str | None) -> str:
    now = datetime.now(timezone.utc)
    devices = {d["id"]: d for d in load_json(DEVICES_JSON).get("devices", [])}
    results = load_json(REPORTS_JSON)
    p = lambda color, text: paint(color, text, ui=True)

    ids = list(devices)
    body = None
    if not devices:
        body = p(DIM, "no devices - run 'findmy-toolbox.py pair' first")
    elif device_id:
        if device_id in devices:
            ids = [device_id]
        else:
            body = p(RED, f"device '{device_id}' is not in {DEVICES_JSON.name}"
                          f" - run 'pair' first")
            ids = []
    hidden = sorted(k for k in results if k not in devices)

    out = []
    for i, dev_id in enumerate(ids):
        state = results.get(dev_id, {})
        reports = state.get("reports", [])
        dev = devices.get(dev_id, {})
        if i:
            out.append("")
        out.append(p(WHITE + BOLD, f"▍ {dev_id}") + "  " +
                   p(DIM, f"slots: {dev.get('slot_seconds', '?')}s  "
                          f"paired: {dev.get('paired_at', '?')}"))
        st = state.get("status") or status_summary(reports)
        if st:
            age_str = "?"
            if st.get("time"):
                try:
                    age_str = fmt_age((now - parse_time(st["time"])
                                       ).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
            raw_status = st.get("status")
            _ok = raw_status is None or status_parity_ok(raw_status)
            out.append("  " + p(BOLD, "STATUS") + "  " +
                       p(status_color(raw_status) + BOLD,
                         st.get("text") or "?") + "  " +
                       p(DIM, f"slot {st.get('slot', '?')}, "
                              f"report {age_str} ago") +
                       ("  " + p(RED + BOLD, "PARITY ERROR") if not _ok else ""))
            for row in status_bit_history(reports, now):
                col = _bit_color(row["name"], row["value"])
                label = ("ON " if row["value"] else "off") + f" {row['name']}"
                if row["seconds"] is None:
                    hold = "no data"
                else:
                    mark = "" if row["certain"] else "≥ "
                    hold = f"{mark}{fmt_age(row['seconds'])}"
                out.append(f"    · {p(col, label):<24}"
                           f"{p(DIM, '(' + hold + ')')}")
        if not reports:
            out.append("  " + p(DIM, "no reports yet"))
            continue
        usable = [r for r in reports if report_time(r) is not None]
        times = [report_time(r) for r in usable]
        if not times:
            out.append("  " + p(DIM, "no usable reports yet"))
            continue

        newest = max(times)
        age = (now - newest).total_seconds()
        col = freshness_color(age)
        out.append("  " + p(BOLD, "FRESHNESS") + "  " +
                   p(col + BOLD, fmt_age(age)) + p(col, " since last report") +
                   "  " + p(DIM, f"(report from "
                                 f"{newest.astimezone().strftime('%H:%M:%S')}, "
                                 f"slot {max(r['slot'] for r in usable)})"))
        pct = f"{min(100.0, age / FRESH_WARN * 100):.0f}%"
        out.append("  " + ui_bar(age / FRESH_WARN, 44, col)
                   + " " + p(col, pct))

        out.append("  " + p(MAGENTA + BOLD, "── latest reports " + "─" * 36))
        out.append("  " + p(DIM, f"{'slot':>5}  {'time (local)':<8}  {'age':>8}  "
                                 f"{'lat':>10}  {'lon':>10}  {'acc':>6}"))
        for r in sorted(usable, key=lambda x: x["time"])[-8:]:
            stamp = report_time(r)
            t = stamp.astimezone()
            r_age = fmt_age((now - stamp).total_seconds())
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

    if body and not ids:
        out.append(body)
    if hidden and ids:
        out.append(p(DIM, f"{len(hidden)} archived device(s) hidden - not in "
                          f"{DEVICES_JSON.name}"))
    right: list[str] = []
    worker = retrieve_running_info()
    right.append(p(GREEN, "● worker") if worker and worker["alive"]
                 else p(DIM, "○ worker"))
    right.append(f"{len(ids)} device(s)")
    if hidden:
        right.append(f"{len(hidden)} archived")
    right.append(datetime.now().astimezone().strftime("%H:%M:%S"))
    return "\n".join(ui_frame("ESP32 FIND MY · BEACON MONITOR", out,
                              right_label="   ".join(right),
                              footer="refresh every few seconds - Ctrl-C to "
                                     "quit"))


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


async def verify_scan(duration: float) -> list[dict]:
    """Collect every Apple Offline Finding advertisement in the window.

    Deliberately unfiltered: a packet that fails to match a known key is
    evidence too (wrong byte order, wrong slot, foreign accessory), so the
    caller decides what a packet means rather than dropping it here.
    """
    from bleak import BleakScanner

    packets: list[dict] = []

    def cb(device, adv):
        mfr = adv.manufacturer_data.get(APPLE_MFR)
        if mfr is None:
            return
        packets.append({"mac": norm_mac(device.address), "mfr": bytes(mfr),
                        "rssi": adv.rssi, "name": device.name})

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()
    return packets


def slot_keys(channel: str) -> dict[str, tuple[str, int, object]]:
    """Recent slot keys of every paired device, indexed by key hex.

    Indexed by the *key* rather than by its MAC on purpose: matching a packet
    to a key must not go through `beacon_mac`, otherwise a wrong address
    ordering would only ever be compared against itself.
    """
    data = load_devices()
    devices = data["devices"]
    if not devices:
        raise UserError(f"no devices in {DEVICES_JSON.name} - run 'pair' first")
    now = datetime.now(timezone.utc)
    out: dict[str, tuple[str, int, object]] = {}
    for dev in devices:
        acc = make_accessory(dev)
        max_i = acc.get_max_index(now)
        min_i = max(0, max_i - 96)
        log(channel, f"{dev['id']}: accepting slots {min_i}..{max_i}")
        for ind in range(min_i, max_i + 1):
            key = acc._primary_key_at(ind)
            out[key.adv_key_bytes.hex()] = (dev["id"], ind, key)
    return out


def cmd_verify(args) -> int:
    channel = "VERIFY"
    try:
        index = slot_keys(channel)
    except UserError as exc:
        log_error(channel, str(exc))
        return 1
    by_mac = {norm_mac(beacon_mac(entry[2])): entry
              for entry in index.values()}
    by_legacy = {norm_mac(legacy_mac(entry[2])): entry
                 for entry in index.values()}
    devices = {entry[0] for entry in index.values()}
    log(channel, f"expecting {len(index)} known key(s) over "
                 f"{len(devices)} device(s), scanning {args.seconds:g}s")
    ui(f"expecting {len(index)} known key(s), scanning {args.seconds:g}s ...")
    try:
        packets = asyncio.run(verify_scan(args.seconds))
    except PermissionError:
        log_error(channel, "BLE scan needs permissions - run with sudo")
        return 1

    ui("")
    if not packets:
        log_error(channel, "no Apple Offline Finding advertisement seen at all")
        ui("FAIL: no Offline Finding advertisement seen (beacon off, "
           "adapter blind, or too far)", RED + BOLD)
        return 1

    match = None
    for packet in packets:
        rebuilt = reconstruct_key(packet["mac"], packet["mfr"])
        entry = index.get(rebuilt.hex()) if rebuilt else None
        if entry is not None:
            match = (packet, entry)
            break

    if match is None:
        return report_verify_miss(channel, packets, by_mac, by_legacy)

    packet, (dev_id, slot, key) = match
    status = packet["mfr"][2] if len(packet["mfr"]) > 2 else None
    log(channel, f"packet {packet['mac']} rssi={packet['rssi']} dBm "
                 f"status={status_decode(status) if status is not None else '?'} "
                 f"-> {dev_id} slot {slot}")
    ui(f"packet : {packet['mac']}  rssi={packet['rssi']} dBm  "
       f"status={status_decode(status) if status is not None else '?'}")
    ui(f"key    : {key.adv_key_bytes.hex()}")
    ui(f"device : {dev_id}  slot {slot}")
    ui("")

    suite = Suite(channel)
    suite.step("frame: static specification (paper Tab. 2, config.h)")
    for name, ok, detail in frame_spec_checks(packet["mac"], packet["mfr"]):
        suite.check(name, ok, detail)
    suite.step("frame: public key against devices.json")
    for name, ok, detail in key_spec_checks(packet["mac"], packet["mfr"],
                                             key, dev_id, slot):
        suite.check(name, ok, detail)

    ui("")
    if suite.failed:
        log_error(channel, f"{len(suite.failed)} check(s) failed")
        ui("FAIL: advertisement on air does not match the spec / "
           "devices.json", RED + BOLD)
        return 3
    log(channel, f"PASS: {len(suite.results)} checks, {dev_id} slot {slot}")
    ui("PASS: advertisement matches the spec and devices.json",
       GREEN + BOLD)
    return 0


def report_verify_miss(channel: str, packets: list[dict], by_mac: dict,
                       by_legacy: dict) -> int:
    """Explain a packet that did not reconstruct to a key we hold."""
    for packet in packets:
        entry = by_legacy.get(packet["mac"])
        if entry:
            dev_id, slot, _ = entry
            log_error(channel, f"{packet['mac']} matches {dev_id} slot {slot} "
                               f"in the reversed byte order")
            ui("FAIL: our key is on air in the WRONG byte order",
               RED + BOLD)
            ui(f"  MAC    : {packet['mac']}")
            ui(f"  device : {dev_id}  slot {slot}")
            ui("  the address must be (key[0]|0b11) || key[1..5] in display "
               "order (paper Tab. 2); NimBLE stores it little-endian",
               DIM)
            return 2
    for packet in packets:
        entry = by_mac.get(packet["mac"])
        if entry:
            dev_id, slot, key = entry
            rebuilt = reconstruct_key(packet["mac"], packet["mfr"])
            status = packet["mfr"][2] if len(packet["mfr"]) > 2 else 0
            log_error(channel, f"{packet['mac']} is {dev_id} slot {slot} but "
                               f"payload differs (rebuilt "
                               f"{rebuilt.hex() if rebuilt else '?'})")
            ui(f"FAIL: MAC {packet['mac']} seen but payload differs!",
               RED + BOLD)
            ui(f"  device : {dev_id}  slot {slot}")
            ui(f"  on air : {packet['mfr'].hex()}")
            ui(f"  want   : {key.of_data(status=status, hint=0).hex()}")
            ui(f"  key    : {key.adv_key_bytes.hex()}", DIM)
            return 2
    log_error(channel, f"{len(packets)} Offline Finding packet(s) seen, none "
                       f"of them ours - unpaired, powered off, or slot too "
                       f"old? run 'sync' or 'sync-ble'")
    ui(f"FAIL: saw {len(packets)} OF packet(s) but none belongs to a key we "
       "hold (unpaired? slot too old? run 'sync')", RED + BOLD)
    for packet in packets[:5]:
        rebuilt = reconstruct_key(packet["mac"], packet["mfr"])
        ui(f"  {packet['mac']}  rssi={packet['rssi']:>4}  "
           f"rebuild={rebuilt.hex() if rebuilt else '?'}", DIM)
    return 2


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


def parse_ident(reply: str) -> dict | None:
    """'IDENT name=<tok|-> paired=<0|1>' -> {'name': str|None, 'paired': bool}."""
    if not reply.startswith("IDENT "):
        return None
    fields = {}
    for token in reply.split()[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    name = fields.get("name", "-")
    return {"name": None if name == "-" else name,
            "paired": fields.get("paired") == "1"}


def expected_key_at(dev: dict, slot: int) -> str:
    """Advertising key X of the stored device `dev` at key slot `slot`."""
    from findmy.accessory import FindMyAccessory

    acc = FindMyAccessory(
        master_key=base64.b64decode(dev["master_key"]),
        skn=base64.b64decode(dev["skn"]),
        sks=b"\x00" * 32,
        paired_at=parse_time(dev["paired_at"]),
        name="t", identifier="t",
    )
    return acc._primary_key_at(slot).adv_key_bytes.hex()


def scan_ports(prefer: str | None = None) -> list[str]:
    """Serial ports that may hold a beacon, the preferred one first."""
    import glob

    found = {p for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*")
             for p in glob.glob(pattern)}
    ordered: list[str] = []
    if prefer:
        ordered.append(prefer)
        found.discard(prefer)
    return ordered + sorted(found)


def probe_ident(port: str, *, channel: str = "CONNECT",
                reset: bool = False) -> dict | None:
    """IDENT? on the beacon at `port`. None when it stays silent (asleep,
    not flashed, or not a console). Never unlocks anything."""
    try:
        beacon = Beacon(port, None, channel=channel, auto_unlock=False,
                        auto_lock=False, timeout=1.0)
        try:
            beacon.after_open(reset=reset,
                              ready_timeout=5.0 if reset else 2.5)
            reply = beacon.cmd("IDENT?", wait=1.0)
        finally:
            beacon.close()
    except (BeaconError, OSError) as exc:
        log(channel, f"{port}: no console ({exc})", logging.DEBUG)
        return None
    ident = parse_ident(reply)
    if ident is None:
        log(channel, f"{port}: unexpected answer to IDENT? ({reply})",
            logging.DEBUG)
    else:
        log(channel, f"{port} answers {reply}", logging.DEBUG)
    return ident


def choose_beacon(found: list[tuple[str, dict]]) -> tuple[str, dict]:
    """One beacon from the ones that answered (or a UserError)."""
    if len(found) == 1:
        return found[0]
    named = [(p, i) for p, i in found if i["name"]]
    if len(named) == 1:
        return named[0]
    if not sys.stdin.isatty():
        raise UserError(f"several beacons answer ({', '.join(p for p, _ in found)}) "
                        f"- pick one with --port")
    print("several beacons answer:")
    for number, (candidate, ident) in enumerate(found, 1):
        print(f"  {number}) {(ident['name'] or 'unnamed'):<16} {candidate}")
    while True:
        raw = prompt("beacon", "1")
        if raw.isdigit() and 1 <= int(raw) <= len(found):
            return found[int(raw) - 1]
        for candidate, ident in found:
            if raw in (candidate, ident["name"] or ""):
                return candidate, ident
        print(paint(RED, "  a number from the list, a port or a name", ui=True))


def match_paired_device(port: str, *, channel: str = "CONNECT") -> dict | None:
    """A paired beacon without a usable name: try the stored PINs and keep
    the entry whose key chain answers. Stops at PIN_FAIL_MAX - 1 attempts so
    the trial can never arm the console's lockout on its own."""
    data = load_devices()
    entries = sorted(data["devices"], key=lambda d: d.get("port") != port)
    for index, dev in enumerate(entries):
        if index >= PIN_FAIL_MAX - 1:
            log(channel, f"stopped after {index} PIN attempt(s) - the console "
                         f"locks out after {PIN_FAIL_MAX} wrong PINs in a row",
                logging.WARNING)
            break
        pin = device_pin(dev)
        try:
            beacon = Beacon(port, pin, dev_id=dev["id"], channel=channel,
                            auto_unlock=False, auto_lock=False,
                            pin_from_flag=True)
            try:
                beacon.after_open(reset=False, ready_timeout=3.0)
                beacon.unlock(pin)
                slot_reply = beacon.cmd("SLOT?")
                key_reply = beacon.cmd("KEY?", wait=8)
            finally:
                beacon.close()
        except (BeaconError, OSError) as exc:
            log(channel, f"PIN trial '{dev['id']}': {exc}", logging.DEBUG)
            continue
        if not slot_reply.startswith("SLOT ") or not key_reply.startswith("KEY "):
            continue
        try:
            slot = int(slot_reply.split()[1])
            got = key_reply.split()[1]
            if got == expected_key_at(dev, slot):
                log(channel, f"the beacon on {port} is '{dev['id']}' (slot {slot})")
                return dev
        except (IndexError, ValueError) as exc:
            log(channel, f"unreadable key state: {exc}", logging.DEBUG)
    return None


def open_console(port: str, pin: str, dev_id: str | None, *,
                 channel: str, pin_from_flag: bool) -> Beacon:
    """Unlocked console session, left unlocked when it is closed."""
    beacon = Beacon(port, pin, dev_id=dev_id, channel=channel,
                    auto_unlock=False, auto_lock=False,
                    pin_from_flag=pin_from_flag)
    try:
        beacon.after_open(reset=False, ready_timeout=5.0)
        beacon.unlock(pin)
    except Exception:
        beacon.close()
        raise
    return beacon


def console_key_matches(beacon: Beacon, entry: dict, port: str) -> bool:
    """True when the console's key chain is the one stored for `entry`."""
    slot_reply = beacon.cmd("SLOT?")
    key_reply = beacon.cmd("KEY?", wait=8)
    if not slot_reply.startswith("SLOT ") or not key_reply.startswith("KEY "):
        raise UserError(f"the console on {port} has no key chain to check "
                        f"({slot_reply} / {key_reply})")
    try:
        slot = int(slot_reply.split()[1])
        got = key_reply.split()[1]
        want = expected_key_at(entry, slot)
    except (IndexError, ValueError) as exc:
        raise UserError(f"unreadable key state on {port}: "
                        f"{slot_reply} / {key_reply}") from exc
    if got != want:
        log("CONNECT", f"{port} slot {slot}: {got} != the stored chain {want}",
            logging.DEBUG)
        return False
    return True


def set_device_name(beacon: Beacon, name: str) -> None:
    reply = beacon.cmd(f"NAME {name}")
    if not reply.startswith("OK NAME"):
        raise UserError(f"the console refused the name '{name}': {reply}")


def connect_uart(*, port: str | None = None, dev_id: str | None = None,
                 pin: str | None = None, reset: bool = True,
                 ask_pair: bool = True, channel: str = "CONNECT") -> dict:
    """Find a beacon, work out which device it is and unlock its console.

    Returns {id, port, name, paired, pin}; the module-level CONNECTED is set
    as soon as the console is open (so a pairing started from here stays
    unlocked) and cleared when no beacon can be identified. Raises
    UserError when nothing usable answers.
    """
    global CONNECTED
    CONNECTED = None
    data = load_devices()
    entry = find_device(data, dev_id) if dev_id else None
    if dev_id and entry is None:
        raise UserError(f"'{dev_id}' is not in {DEVICES_JSON.name} - run "
                        f"'pair' first, or drop --id to identify the beacon")
    ports = [port] if port else scan_ports(port or (entry or {}).get("port"))
    if not ports:
        raise UserError("no serial ports found (looked for /dev/ttyACM* and "
                        "/dev/ttyUSB*)")

    found: list[tuple[str, dict]] = []
    for candidate in ports:
        ident = probe_ident(candidate, channel=channel, reset=False)
        if ident:
            found.append((candidate, ident))
    if not found and reset:
        for candidate in ports:
            ident = probe_ident(candidate, channel=channel, reset=True)
            if ident:
                found.append((candidate, ident))
    if not found:
        raise UserError(
            f"no beacon answered on {', '.join(ports)} - flashed, powered and "
            f"cabled? ('reset' wakes one up, --no-reset skips the wake-up)")

    chosen_port, ident = choose_beacon(found)
    log(channel, f"beacon on {chosen_port}: "
                 f"name={ident['name'] or '-'} paired={int(ident['paired'])}")
    if dev_id and ident["name"] and ident["name"] != dev_id:
        raise UserError(f"{chosen_port} identifies as '{ident['name']}', not "
                        f"'{dev_id}' - drop --id or check the cable")

    if ident["paired"]:
        by_name = find_device(data, ident["name"]) if ident["name"] else None
        if by_name:
            entry = by_name
        if entry is None:
            entry = match_paired_device(chosen_port, channel=channel)
            if entry is None:
                raise UserError(
                    f"the beacon on {chosen_port} is paired, but nothing in "
                    f"{DEVICES_JSON.name} matches it - re-key it with 'pair "
                    f"--force --id <name> --port {chosen_port}' or start over "
                    f"with 'wipe'")
        beacon = open_console(chosen_port, unlock_pin_for(entry, pin),
                              entry["id"], channel=channel,
                              pin_from_flag=pin is not None)
        try:
            if not console_key_matches(beacon, entry, chosen_port):
                beacon.close()
                other = match_paired_device(chosen_port, channel=channel)
                if other is None:
                    raise UserError(
                        f"{chosen_port} opened with '{entry['id']}'s PIN but "
                        f"advertises another key chain - check 'devices'")
                entry = other
                beacon = open_console(chosen_port, unlock_pin_for(entry, pin),
                                      entry["id"], channel=channel,
                                      pin_from_flag=pin is not None)
            if valid_name(entry["id"]) and ident["name"] != entry["id"]:
                set_device_name(beacon, entry["id"])
                log(channel, f"device name healed to '{entry['id']}'")
        finally:
            beacon.close()
        conn = {"id": entry["id"], "port": chosen_port, "name": entry["id"],
                "paired": True, "pin": device_pin(entry)}
        CONNECTED = conn
        return conn

    # Unpaired beacon: config mode, factory PIN (unless --pin says otherwise).
    unlock_pin = pin or DEFAULT_PIN
    beacon = open_console(chosen_port, unlock_pin, dev_id, channel=channel,
                          pin_from_flag=pin is not None)
    try:
        if ident["name"] and valid_name(ident["name"]):
            name = ident["name"]
        else:
            suggested = dev_id or ident["name"] or (
                DEFAULT_ID if not data["devices"] else "")
            name = sanitize_name(
                prompt(f"device name for the beacon on {chosen_port}",
                       suggested or None))
            if not valid_name(name):
                raise UserError(f"the name must be 1..{NAME_LEN} characters "
                                f"from [A-Za-z0-9_-], got '{name}'")
            set_device_name(beacon, name)
        conn = {"id": name, "port": chosen_port, "name": name,
                "paired": False, "pin": unlock_pin}
        CONNECTED = conn
    finally:
        beacon.close()

    ui(f"\n'{name}' on {chosen_port} is unpaired (config mode)", YELLOW + BOLD)
    ui(f"the console is unlocked with {unlock_pin}", DIM)
    if ask_pair and sys.stdin.isatty():
        if prompt(f"pair '{name}' now?", "y").lower() in ("y", "yes"):
            pair_args = build_parser().parse_args(
                ["pair", "--id", name, "--port", chosen_port, "--reset"])
            if pin:
                pair_args.pin = pin
            if find_device(load_devices(), name) is not None:
                pair_args.force = True
                pair_args.yes = True       # "pair now?" answered the confirm
            if COMMANDS["pair"](pair_args) == 0:
                conn = CONNECTED or conn
                conn.update({"paired": True, "name": name,
                             "pin": find_device(load_devices(), name)
                             ["pin"]})
            else:
                ui("pairing failed - the console stays unlocked, run 'pair' "
                   "to retry", YELLOW)
        else:
            ui(f"not paired - 'pair --id {name} --port {chosen_port}' when "
               f"you are ready", DIM)
    elif ask_pair:
        ui(f"unpaired: run 'pair --id {name} --port {chosen_port}'", DIM)
    return conn


def cmd_connect(args) -> int:
    channel = "CONNECT"
    log(channel, "looking for a beacon")
    conn = connect_uart(port=args.port, dev_id=args.id, pin=args.pin,
                        reset=not args.no_reset, ask_pair=not args.no_pair,
                        channel=channel)
    state = "unpaired (config mode)" if not conn["paired"] else "paired"
    ui(f"\nconnected to '{conn['id']}' on {conn['port']} - {state}",
       GREEN + BOLD)
    ui(f"  console   : unlocked; it locks itself again after the countdown",
       DIM)
    ui(f"  menu      : run the toolbox without a command for the console menu",
       DIM)
    return 0


def cmd_disconnect(args) -> int:
    global CONNECTED
    if CONNECTED and not args.id:
        args.id = CONNECTED["id"]
        args.port = args.port or CONNECTED["port"]
    rc = cmd_lock(args)
    if rc == 0:
        CONNECTED = None
        ui("disconnected - the beacon locks itself after the countdown", DIM)
        ui("run 'connect' to identify it again", DIM)
    return rc


def cmd_reset(args) -> int:
    channel = "MAIN"
    data = load_devices()
    device = None
    if args.id:
        device = find_device(data, args.id)
    elif data["devices"]:
        device = find_device(data, pick_device(data, None, required=False) or "")
    port = resolve_port(args.port, device)

    log(channel, f"rebooting the device on {port} over the control lines")
    beacon = Beacon(port, None, channel=channel, auto_unlock=False,
                    auto_lock=False, timeout=1.0)
    try:
        beacon.pulse_reset()
        if not beacon.wait_ready(20.0):
            raise UserError(f"{port} did not come back after the reset - "
                            f"cable, power or the wrong port?")
        ident = parse_ident(beacon.cmd("IDENT?", wait=1.0))
    finally:
        beacon.close()

    who = (ident or {}).get("name") or "unnamed"
    paired = "paired" if (ident or {}).get("paired") else "unpaired"
    ui(f"{port} rebooted ({who}, {paired})", GREEN + BOLD)
    ui("it boots locked - 'connect' opens the console again", DIM)
    if CONNECTED is not None and CONNECTED.get("port") == port:
        ui("the connection was cleared, run 'connect' to return", YELLOW)
    return 0


def cmd_apple(args) -> int:
    channel = "APPLE"
    existing = session_account_name()

    if args.action == "status":
        if existing:
            ui(f"Apple ID: connected as {existing}", GREEN + BOLD)
            ui(f"  session : {SESSION_FILE.relative_to(SCRIPTS_DIR)}", DIM)
        else:
            ui("Apple ID: not connected (no saved session)", YELLOW)
            ui("  run 'apple-id connect' (or the menu) to log in", DIM)
        return 0

    if args.action == "disconnect":
        if existing is None and not SESSION_FILE.exists():
            ui("Apple ID: not connected - nothing to disconnect")
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                raise UserError("deleting the Apple ID session needs --yes")
            if prompt(f"delete the saved session for "
                      f"{existing or 'this Apple ID'}", "n").lower() not in (
                          "y", "yes"):
                ui("cancelled - the session is unchanged", DIM)
                return 0
        SESSION_FILE.unlink()
        log(channel, f"Apple ID session for {existing or 'unknown'} deleted")
        ui("Apple ID disconnected: the saved session is gone", GREEN + BOLD)
        ui(f"  {SESSION_FILE.relative_to(SCRIPTS_DIR)} deleted; retrieve/watch "
           f"will ask for a login again", DIM)
        ui("  this only forgets the session locally - use apple.com to "
           "revoke access elsewhere", DIM)
        return 0

    if existing and args.email and args.email != existing:
        if not args.yes:
            if not sys.stdin.isatty():
                raise UserError("switching accounts needs --yes "
                                "(it deletes the saved session)")
            if prompt(f"forget the session saved for {existing} and log in as "
                      f"{args.email}", "n").lower() not in ("y", "yes"):
                ui("cancelled - the session is unchanged", DIM)
                return 0
        SESSION_FILE.unlink()
        log(channel, f"forgetting the session for {existing}")
        existing = None

    account = asyncio.run(apple_login(args.email, channel))
    if account is None:
        log_error(channel, "no Apple ID session - login did not finish")
        return 1
    name = getattr(account, "account_name", None) or args.email or existing
    ui(f"Apple ID connected as {name or 'you'}", GREEN + BOLD)
    ui(f"  session : {SESSION_FILE.relative_to(SCRIPTS_DIR)}", DIM)
    ui("  retrieve/watch/monitor can fetch reports now", DIM)
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
  connect    find a beacon on the TTYs, identify it (IDENT?), unlock it
  disconnect lock the console of the beacon 'connect' opened
  reset      reboot a beacon over the UART control lines (wake it up)
  apple-id   status | connect [apple-id] | disconnect  (saved session)
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
               --restart  stop the worker, then start it again
               --doctor   do known reports still come back?
  watch      follow the retrieval worker's log (starts one if needed)
  monitor    live dashboard (Ctrl-C to quit)
  verify     spec-check the advertisement on air
  scan       raw BLE scan for Find My packets
  pin        set a new console PIN
  unlock     unlock the console (device left unlocked)
  lock       lock the console (device left locked)
  wipe       factory reset: erase keys + PIN (--yes to confirm)
  log        show or follow state/toolbox.log
  help       show this overview

Run it without a command for the interactive menu: one window, entries
grouped by monitoring / radio / console / provisioning / debug / apple id,
with the current console, Apple ID and worker state on top. Entries that
cannot run right now (no console connection, no Apple session) stay listed
and say why. The retrieval worker is started when the menu opens and
stopped again when it closes.

Options before or after the command: -v/--verbose (debug logging).

Needs Python packages only (pip install -r requirements.txt): bleak,
pyserial, findmy. ESP-IDF is for building/flashing the firmware in ESP32/,
not for this tool.

State: state/{devices.json,reports.json,account_state.json,toolbox.log,
retrieve.lock}. Logs go to stderr and state/toolbox.log (secrets redacted).
"""


def cmd_help(args) -> int:
    print(HELP_TEXT, end="")
    return 0


COMMANDS = {
    "pair": cmd_pair,
    "connect": cmd_connect,
    "disconnect": cmd_disconnect,
    "reset": cmd_reset,
    "apple-id": cmd_apple,
    "sync": cmd_sync,
    "sync-ble": cmd_sync_ble,
    "status-debug": cmd_status_debug,
    "reset-poll": cmd_reset_poll,
    "status": cmd_status,
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
    "pair": "PAIR", "connect": "CONNECT", "disconnect": "MAIN",
    "reset": "MAIN", "apple-id": "APPLE", "sync": "SYNC", "sync-ble": "SYNC-BLE",
    "status-debug": "STATUS-DEBUG", "reset-poll": "STATUS-DEBUG", "status": "MAIN",
    "devices": "MAIN", "test": "TEST", "power": "POWER", "retrieve": "RETRIEVE",
    "watch": "RETRIEVE", "monitor": "MONITOR", "verify": "VERIFY", "scan": "SCAN",
    "pin": "MAIN", "unlock": "MAIN", "lock": "MAIN", "wipe": "MAIN",
    "log": "MAIN", "help": "UI",
}

# The menu is one window with groups. monitoring/radio/apple/system work on
# their own, the uart groups are greyed out until 'connect' identified a
# beacon. Every entry's argv is complete: [command, ...its own flags], the
# 4th field is the availability rule (None = always usable).
MENU_GROUPS = [
    ("monitoring", CYAN, [
        ("devices", "list paired devices", ["devices"], None),
        ("retrieve", "fetch location reports now", ["retrieve"], None),
        ("watch", "follow the retrieval worker's log", ["watch"], None),
        ("restart", "stop + start the retrieval worker",
         ["retrieve", "--restart"], None),
        ("monitor", "live dashboard", ["monitor"], None),
        ("log", "show or follow toolbox.log", ["log"], None),
    ]),
    ("radio (ble)", BLUE, [
        ("verify", "spec-check the advertisement on air", ["verify"], None),
        ("scan", "raw BLE scan", ["scan"], None),
        ("sync-ble", "sync the slot counter from BLE", ["sync-ble"], None),
    ]),
    ("console (uart)", GREEN, [
        ("connect", "find, identify and unlock a beacon", ["connect"], None),
        ("disconnect", "lock the console, drop the connection",
         ["disconnect"], "console"),
        ("reset", "reboot a beacon (wake it up)", ["reset"], None),
        ("sync", "sync the slot counter (USB)", ["sync"], "console"),
        ("status", "show the device's config settings", ["status"], "console"),
        ("unlock", "unlock the console", ["unlock"], "console"),
        ("lock", "lock the console", ["lock"], "console"),
    ]),
    ("provisioning", YELLOW, [
        ("pair", "re-pair the connected beacon (new keys)",
         ["pair", "--force"], "console"),
        ("pin", "set a new console PIN", ["pin"], "console"),
        ("wipe", "factory reset: erase keys + PIN", ["wipe"], "console"),
    ]),
    ("debug", MAGENTA, [
        ("doctor", "account check: known reports return",
         ["retrieve", "--doctor"], None),
        ("status-debug", "set/poll the OS status bits",
         ["status-debug"], "console"),
        ("reset-poll", "wake a sleeping beacon for flashing (OS? reset)",
         ["reset-poll"], None),
        ("test", "console protocol test suite", ["test"], "console"),
        ("power", "awake/sleep duty cycle", ["power"], "console"),
    ]),
    ("apple id", WHITE, [
        ("apple connect", "log in with an Apple ID",
         ["apple-id", "connect"], "no-session"),
        ("apple status", "who is logged in", ["apple-id", "status"], None),
        ("apple disconnect", "forget the saved session",
         ["apple-id", "disconnect"], "session"),
    ]),
    ("system", WHITE, [
        ("help", "show the command overview", ["help"], None),
    ]),
]


def entry_reason(needs: str | None, conn: dict | None,
                 apple: str | None) -> str | None:
    """Why an entry is greyed out right now (None when it is usable)."""
    if needs == "console" and not conn:
        return "needs 'connect'"
    if needs == "session" and not apple:
        return "no saved Apple session"
    if needs == "no-session" and apple:
        return f"logged in as {apple}"
    return None


def entry_argv(argv: list[str], needs: str | None,
               conn: dict | None) -> list[str]:
    """Complete an entry's argv with the connected console, if there is one."""
    if not conn:
        return list(argv)
    if needs == "console":
        return argv + ["--id", conn["id"], "--port", conn["port"]]
    if argv[0] == "reset":
        return argv + ["--port", conn["port"]]
    return list(argv)


def status_lines(conn: dict | None = None) -> list[str]:
    p = lambda color, text: paint(color, text, ui=True)
    try:
        devices = load_devices().get("devices", [])
    except UserError as exc:
        return [p(RED, f"devices.json: {exc}")]
    results = load_json(REPORTS_JSON)
    if not isinstance(results, dict):
        results = {}
    info = retrieve_running_info()

    newest_age = None
    report_count = 0
    now = datetime.now(timezone.utc)
    for dev in devices:
        state = results.get(dev.get("id"))
        if not isinstance(state, dict):
            continue
        reports = state.get("reports")
        if not isinstance(reports, list):
            continue
        report_count += len(reports)
        times = [t for t in (report_time(r) for r in reports) if t is not None]
        if times:
            age = (now - max(times)).total_seconds()
            newest_age = age if newest_age is None else min(newest_age, age)

    console = p(YELLOW, "not connected")
    if conn:
        state = "paired" if conn.get("paired") else "unpaired"
        console = p(GREEN, f"connected to '{conn['id']}' on {conn['port']} "
                           f"({state})")
    apple = session_account_name()
    apple_line = (p(GREEN, f"connected as {apple}") if apple
                  else p(YELLOW, "not connected"))

    worker = p(DIM, "not running")
    if info and info["alive"]:
        worker = p(GREEN, f"running (pid {info['pid']})")
    elif info:
        worker = p(YELLOW, "stale lock file")
    elif not apple:
        worker = p(DIM, "not started (no Apple session)")

    reports_line = p(DIM, f"reports : {report_count}, nothing fetched yet")
    if newest_age is not None:
        reports_line = p(DIM, f"reports : {report_count}, "
                              f"newest {fmt_age(newest_age)} ago  ") + \
            ui_bar(newest_age / FRESH_WARN, 18, freshness_color(newest_age))

    return [
        p(DIM, f"console : {console}"),
        p(DIM, f"apple   : {apple_line}"),
        p(DIM, f"devices : {len(devices)}"
               + (f" ({', '.join(d['id'] for d in devices)})" if devices else "")),
        reports_line,
        p(DIM, f"worker  : {worker}"),
        p(DIM, f"log     : {LOG_FILE.relative_to(SCRIPTS_DIR)}"),
    ]


def menu() -> int:
    p = lambda color, text: paint(color, text, ui=True)
    global CONNECTED
    running = retrieve_running_info()
    had_worker = bool(running and running["alive"])
    try:
        if not had_worker and session_account_name():
            retrieve_start_bg(quiet=True)
    except OSError as exc:
        log_error("WORKER", f"could not start the retrieval worker: {exc}")

    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            apple = session_account_name()
            entries: list[tuple[str, str, list[str], str | None]] = []
            body = status_lines(CONNECTED)
            body.append("")
            for title, color, group in MENU_GROUPS:
                body.append(p(color + BOLD, f"  {title.upper()}"))
                for name, description, argv, needs in group:
                    reason = entry_reason(needs, CONNECTED, apple)
                    done = entry_argv(argv, needs, CONNECTED)
                    entries.append((name, description, done, reason))
                    left = f"   {len(entries):>2}) {name:<16} {description}"
                    if not reason:
                        body.append(left)
                    elif text_width(left) + len(reason) + 2 <= MENU_COLS:
                        gap = MENU_COLS - text_width(left) - len(reason)
                        body.append(p(DIM, left) + " " * gap
                                    + p(YELLOW, reason))
                    else:
                        head = f"   {len(entries):>2}) {name:<16} "
                        short = truncate(reason, MENU_COLS - text_width(head))
                        body.append(p(DIM, head) + p(YELLOW, short))
                body.append("")
            body.append("    0) quit")

            info = retrieve_running_info()
            if info and info["alive"]:
                worker_tag = p(GREEN, "● worker running")
            elif info:
                worker_tag = p(YELLOW, "● worker stale")
            else:
                worker_tag = p(DIM, "○ worker stopped")
            clock = datetime.now().astimezone().strftime("%H:%M:%S")
            for line in ui_frame("ESP32 FIND MY · TOOLBOX", body,
                                 right_label=f"{worker_tag}  {clock}",
                                 footer="0 quit   a number, a name or any "
                                        "command"):
                print(line)

            raw = input(p(BOLD, "> ")).strip()
            if not raw:
                continue
            choice = raw.lower()
            if choice in ("0", "q", "quit", "exit"):
                log("UI", "menu closed")
                return 0
            picked = None
            if choice.isdigit() and 1 <= int(choice) <= len(entries):
                picked = entries[int(choice) - 1]
            else:
                for item in entries:
                    if choice == item[0]:
                        picked = item
                        break
            if picked is None and raw:
                try:
                    typed = shlex.split(raw)
                except ValueError:
                    typed = raw.split()
                if typed and typed[0] not in COMMANDS:
                    typed[0] = typed[0].lower()
                if typed and typed[0] in COMMANDS:
                    picked = (typed[0], "", typed, None)  # typed by hand
            if picked is None:
                print(p(RED, f"unknown choice '{choice}'"))
                time.sleep(1.0)
                continue
            if picked[3]:
                print(p(YELLOW, f"\n'{picked[0]}' is not available: "
                                f"{picked[3]}"))
                time.sleep(1.5)
                continue

            name, _description, argv, _reason = picked
            log("UI", f"menu: {' '.join(argv)}")
            args = build_parser().parse_args(argv)
            try:
                rc = COMMANDS[argv[0]](args)
            except (UserError, BeaconError, OSError) as exc:
                print(p(RED, f"\n{exc}"))
                log_error(COMMAND_CHANNEL.get(argv[0], "MAIN"), str(exc))
                rc = 1
            if argv[0] == "wipe" and rc == 0:
                CONNECTED = None     # the beacon is unnamed and unpaired now
            elif argv[0] == "reset" and CONNECTED:
                CONNECTED = None     # it rebooted, the console was closed
            if rc:
                print(p(YELLOW, f"\n{name} exited with status {rc}"))
            input(p(DIM, "\npress Enter to return to the menu "))
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write(RESET + "\n")
        log("UI", "menu closed")
        return 0
    finally:
        info = retrieve_running_info()
        if not had_worker and info and info["alive"]:
            log("UI", "menu closed: stopping the worker it started")
            retrieve_stop()


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
                      help="overwrite an existing device id (confirms on a TTY)")
    pair.add_argument("--yes", "-y", action="store_true",
                      help="no confirmation and no prompts on a terminal "
                           "(timings keep their defaults)")
    pair.add_argument("--debug", type=int, choices=[0, 1], default=None,
                      help="device debug flag after pairing")
    pair.add_argument("--adv-ms", type=int,
                      help="advertisement period in ms (200..60000, "
                           "prompted when omitted)")
    pair.add_argument("--rot-sec", type=int, help="key rotation period (s, "
                                                  "prompted when omitted)")
    pair.add_argument("--dbg-sec", type=int, help="console countdown (s, "
                                                   "prompted when omitted)")

    connect = sub.add_parser("connect", parents=[verbose],
                             help="identify + unlock a beacon over UART")
    connect.add_argument("--port")
    connect.add_argument("--id", help="device id to connect to (default: identify)")
    connect.add_argument("--pin", help="PIN to unlock with (default: stored)")
    connect.add_argument("--no-reset", action="store_true",
                         help="do not pulse reset to wake a silent beacon")
    connect.add_argument("--no-pair", action="store_true",
                         help="never offer to pair an unpaired beacon")

    disconnect = sub.add_parser("disconnect", parents=[verbose],
                                help="lock the console of the connected beacon")
    disconnect.add_argument("--port")
    disconnect.add_argument("--id")
    disconnect.add_argument("--pin")

    reset = sub.add_parser("reset", parents=[verbose],
                           help="reboot a beacon over the UART control lines")
    reset.add_argument("--port")
    reset.add_argument("--id")

    apple = sub.add_parser("apple-id", parents=[verbose],
                           help="connect or disconnect the Apple ID session")
    apple.add_argument("action", nargs="?", default="status",
                       choices=("status", "connect", "disconnect"))
    apple.add_argument("email", nargs="?", default=None,
                       help="Apple ID to log in as (with 'connect')")
    apple.add_argument("--yes", "-y", action="store_true",
                       help="no confirmation for disconnect/account switch")

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

    sdbg = sub.add_parser(
        "status-debug", parents=[verbose],
        help="interactive Debug > Status: toggle the OS status bits and "
             "push or OS?-poll them to the beacon")
    sdbg.add_argument("--port")
    sdbg.add_argument("--id")
    sdbg.add_argument("--pin")

    rspol = sub.add_parser(
        "reset-poll", parents=[verbose],
        help="answer the beacon's next OS? poll with reset=1 so it reboots "
             "into its debug window (wake a sleeping device for flashing)")
    rspol.add_argument("--port")
    rspol.add_argument("--id")
    rspol.add_argument("--pin")

    status = sub.add_parser(
        "status", parents=[verbose],
        help="print the device's config settings (STATUS?)")
    status.add_argument("--port")
    status.add_argument("--id")
    status.add_argument("--pin")

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
    retrieve.add_argument("--doctor", action="store_true",
                          help="re-fetch known reports (account check)")
    retrieve.add_argument("--follow", action="store_true",
                          help="tail the retrieval log")
    retrieve.add_argument("--stop", action="store_true",
                          help="stop the background worker")
    retrieve.add_argument("--restart", action="store_true",
                          help="stop the worker and start it again")
    retrieve.add_argument("--lines", type=int, default=50,
                          help="log lines to show with --follow")

    watch = sub.add_parser("watch", parents=[verbose],
                           help="start the worker and follow its log")
    watch.add_argument("--device", default=DEFAULT_DEVICE,
                       help="only log lines mentioning this device")
    watch.add_argument("--lines", type=int, default=40,
                       help="log lines to show before following")

    monitor = sub.add_parser("monitor", parents=[verbose],
                             help="live dashboard")
    monitor.add_argument("--interval", type=float, default=5.0)
    monitor.add_argument("--device")
    monitor.add_argument("--once", action="store_true")

    verify = sub.add_parser(
        "verify", parents=[verbose],
        help="spec-check the advertisement on air",
        description="Scan for Apple Offline Finding advertisements, rebuild "
                    "the public key from address + payload the way a finder "
                    "does, and check the frame against the spec (paper Tab. "
                    "2, config.h) and against devices.json. "
                    "Exit codes: 0 all checks pass; 1 no packet seen; 2 "
                    "packets seen but none is ours - a reversed address "
                    "ordering is named explicitly; 3 a spec or key check "
                    "failed.")
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

    for name in ("pair", "sync", "power", "pin", "unlock", "lock", "wipe",
                 "disconnect", "status-debug", "reset-poll", "status"):
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




