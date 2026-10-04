#!/usr/bin/env python3
"""
Find My Dashboard backend.

Two jobs:
  1. Periodically fetch new decrypted Offline-Finding reports from Apple's
     (free) Find My servers for every paired device, exactly like the
     toolbox `watch` module does, and write them into PostgreSQL.
  2. Serve a small read-only JSON API that the nginx-served frontend uses to
     draw the map and status ledger.

Apple login is done with the `findmy` library (currently in use by
Scripts/findmy-toolbox.py). A saved session is restored from account_state.json;
if that is missing/expired the process exits with a clear message asking for a
one-time login (run the toolbox `apple-id connect` to create the session file).
"""

import asyncio
import base64
import glob
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import psycopg2
import psycopg2.extras
from findmy import AsyncAppleAccount, LocalAnisetteProvider
from findmy.accessory import FindMyAccessory


class _JsonEncoder(json.JSONEncoder):
    """Make any row from psycopg2/RealDict serialisable to JSON."""
    def default(self, obj):
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if isinstance(obj, (date,)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

STATE_DIR = os.environ.get("STATE_DIR", "/data")
SESSION_FILE = os.path.join(STATE_DIR, "account_state.json")
DEVICES_FILE = os.path.join(STATE_DIR, "devices.json")

DATABASE_URL = os.environ.get("DATABASE_URL")
FETCH_INTERVAL = float(os.environ.get("FETCH_INTERVAL", "900"))   # s
BACKTRACK = int(os.environ.get("BACKTRACK", "180"))               # slots
API_PORT = int(os.environ.get("API_PORT", "8090"))
VERBOSE = os.environ.get("VERBOSE", "0") == "1"

log = logging.getLogger("dashboard")


# --------------------------------------------------------------------------
# Config loading helpers (mirror the toolbox)
# --------------------------------------------------------------------------

def accessory_class():
    class RotatingAccessory(FindMyAccessory):
        def __init__(self, *, slot_seconds: int, **kwargs):
            super().__init__(sks=b"\x00" * 32, **kwargs)
            self._slot_interval = timedelta(seconds=slot_seconds)

        @property
        def interval(self) -> timedelta:
            return self._slot_interval

    return RotatingAccessory


def make_accessory(dev: dict):
    paired_at = datetime.fromisoformat(dev["paired_at"])
    alignment_date = paired_at
    alignment_index = 0
    if dev.get("slot_synced_at") and dev.get("last_known_slot") is not None:
        alignment_date = datetime.fromisoformat(dev["slot_synced_at"])
        alignment_index = int(dev["last_known_slot"])
    cls = accessory_class()
    return cls(
        slot_seconds=int(dev.get("slot_seconds", 120)),
        master_key=base64.b64decode(dev["master_key"]),
        skn=base64.b64decode(dev["skn"]),
        paired_at=paired_at,
        alignment_date=alignment_date,
        alignment_index=alignment_index,
    )


def load_devices():
    with open(DEVICES_FILE) as f:
        data = json.load(f)
    return data.get("devices", [])


async def load_account():
    """Restore the Apple session. Raise if unavailable."""
    if not os.path.exists(SESSION_FILE):
        raise RuntimeError(
            "No saved Apple session. Run the toolbox once:\n"
            "  python3 Scripts/findmy-toolbox.py apple-id connect\n"
            "(this writes account_state.json, which is mounted into the container)"
        )
    account = AsyncAppleAccount.from_json(str(SESSION_FILE))
    from findmy.reports import LoginState
    if account.login_state != LoginState.LOGGED_IN:
        raise RuntimeError(f"Apple session not logged in (state={account.login_state}). "
                           "Re-run 'apple-id connect' in the toolbox.")
    return account


# --------------------------------------------------------------------------
# Report model
# --------------------------------------------------------------------------

STATUS_POWER = 0x08   # OS powered on -> dot FILL colour
STATUS_LOGIN = 0x10   # user logged in -> dot OUTLINE colour

REPORT_COLUMNS = ("slot", "type", "report_time", "latitude", "longitude",
                  "accuracy_m", "confidence", "status", "status_text",
                  "key_hash")


def report_to_dict(r, slot: int, key) -> dict:
    # Field names match the toolbox's report_to_dict() exactly (the de-facto
    # reference for the `findmy` library's decrypted report object).
    return {
        "slot": int(slot),
        "type": key.key_type.name.lower(),
        "report_time": r.timestamp.isoformat(),
        "latitude": float(r.latitude),
        "longitude": float(r.longitude),
        "accuracy_m": int(r.horizontal_accuracy),
        "confidence": int(r.confidence),
        "status": int(r.status),
        "status_text": "",
        "key_hash": str(getattr(key, "hashed_adv_key_b64", "")),
    }


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------

def db_conn():
    return psycopg2.connect(DATABASE_URL)


def upsert_device(conn, dev: dict):
    sql = """
        INSERT INTO devices (id, port, paired_at, master_key, skn,
                             slot_seconds, last_known_slot, adv_ms, rot_sec,
                             dbg_sec, pin, sync_method)
        VALUES (%(id)s, %(port)s, %(paired_at)s, %(master_key)s, %(skn)s,
                %(slot_seconds)s, %(last_known_slot)s, %(adv_ms)s, %(rot_sec)s,
                %(dbg_sec)s, %(pin)s, %(sync_method)s)
        ON CONFLICT (id) DO UPDATE SET
            port=EXCLUDED.port, master_key=EXCLUDED.master_key,
            skn=EXCLUDED.skn, slot_seconds=EXCLUDED.slot_seconds,
            adv_ms=EXCLUDED.adv_ms, rot_sec=EXCLUDED.rot_sec,
            dbg_sec=EXCLUDED.dbg_sec, pin=EXCLUDED.pin,
            sync_method=EXCLUDED.sync_method
    """
    row = dict(dev)
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()


def insert_report(conn, dev_id: str, d: dict) -> bool:
    sql = """
        INSERT INTO reports (device_id, slot, type, report_time, latitude,
                             longitude, accuracy_m, confidence, status,
                             status_text, key_hash, retrieved_at)
        VALUES (%(device_id)s, %(slot)s, %(type)s, %(report_time)s,
                %(latitude)s, %(longitude)s, %(accuracy_m)s,
                %(confidence)s, %(status)s, %(status_text)s, %(key_hash)s,
                now())
        ON CONFLICT (device_id, slot) DO NOTHING
        RETURNING id
    """
    row = dict(d)
    row["device_id"] = dev_id
    with conn.cursor() as cur:
        cur.execute(sql, row)
        inserted = cur.fetchone()
    conn.commit()
    if inserted:
        # keep devices.last_seen fresh
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET last_seen=%(t)s WHERE id=%(d)s",
                {"t": d["report_time"], "d": dev_id})
        conn.commit()
    return bool(inserted)


# --------------------------------------------------------------------------
# Fetch loop (mirrors toolbox `watch` logic, but writes to Postgres)
# --------------------------------------------------------------------------

async def fetch_devices(conn):
    accessories = {}
    for dev in load_devices():
        upsert_device(conn, dev)
        accessories[dev["id"]] = make_accessory(dev)
    return accessories


async def fetch_cycle(account, accessories, conn):
    fresh_total = 0
    for dev_id, acc in accessories.items():
        try:
            fresh = await fetch_reports(account, acc, dev_id, conn)
        except Exception as exc:              # never let one device kill the loop
            log.error("fetch %s failed: %s", dev_id, exc)
            continue
        fresh_total += len(fresh)
        for d in fresh:
            log.info("%s: new report slot=%d time=%s lat=%s lon=%s status=%d",
                     dev_id, d["slot"], d["report_time"], d["latitude"],
                     d["longitude"], d["status"])
    return fresh_total


async def fetch_reports(account, acc, dev_id, conn):
    now = datetime.now(timezone.utc)
    max_i = acc.get_max_index(now)
    floor = max(0, max_i - BACKTRACK)
    start = max(0, min(floor, max_i))

    known = set()
    with conn.cursor() as cur:
        cur.execute("SELECT slot FROM reports WHERE device_id=%s",
                    (dev_id,))
        known = {r[0] for r in cur.fetchall()}

    fresh = []
    for slot in range(max_i, start - 1, -1):
        if slot in known:
            continue
        keys = {}
        for key in acc.keys_at(slot):
            keys.setdefault(key.hashed_adv_key_b64, key)
        try:
            raw = await account.fetch_raw_reports([(list(keys.keys()), [])])
        except Exception:
            continue
        for r in raw:
            key = keys.get(base64.b64encode(r.hashed_adv_key_bytes).decode())
            if key is None:
                continue
            r.decrypt(key)
            d = report_to_dict(r, slot, key)
            if d and insert_report(conn, dev_id, d):
                fresh.append(d)
    return fresh


# --------------------------------------------------------------------------
# HTTP JSON API
# --------------------------------------------------------------------------

# Status‑change labels: when a bit flips on/off we map it to a short phrase.
STATUS_LABELS = {
    0x01: 'unlocked',
    0x02: 'config',
    0x04: 'low battery',
    0x08: 'power',
    0x10: 'login',
    0x20: 'network',
    0x40: 'parity',
}

# --------------------------------------------------------------------------
class ApiHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload):
        body = json.dumps(payload, cls=_JsonEncoder).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/devices":
                self.api_devices()
            elif u.path == "/api/status":
                self.api_statuses()
            elif u.path == "/api/reports":
                self.api_reports(u.query)
            elif u.path == "/api/history":
                self.api_history(u.query)
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            log.error("API error: %s", exc)
            self._send(500, {"error": str(exc)})

    def log_message(self, *a):
        pass  # keep the API quiet

    def api_devices(self):
        conn = db_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, slot_seconds, last_seen "
                            "FROM devices ORDER BY id")
                rows = cur.fetchall()
            self._send(200, {"devices": rows})
        finally:
            conn.close()

    def api_statuses(self):
        conn = db_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT DISTINCT ON (device_id)
                           device_id, slot, report_time, latitude, longitude,
                           confidence, status, status_text
                    FROM reports
                    ORDER BY device_id, report_time DESC
                """)
                rows = cur.fetchall()
            self._send(200, {"statuses": rows})
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # /api/history — per-device state‑change history.
    # Walks each device's reports chronologically and emits an event every
    # time one or more status bits flip.  Returns:
    #   { device_id: String,
    #     events: [ { time: ISO-string, changes: [String, ...] } ] }
    # ------------------------------------------------------------------
    def api_history(self, query):
        q = parse_qs(query)
        device_id = q.get("device", [None])[0]

        conn = db_conn()
        try:
            where = []
            params = []
            if device_id:
                where.append("device_id = %s")
                params.append(device_id)

            sql = (
                "SELECT device_id, report_time, status "
                "FROM reports "
            )
            if where:
                sql += "WHERE " + " AND ".join(where)
            sql += " ORDER BY device_id, report_time"

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                all_rows = cur.fetchall()

            # Group by device
            by_device = {}
            for r in all_rows:
                did = r['device_id']
                by_device.setdefault(did, []).append({
                    'time': r['report_time'],
                    'status': r['status'],
                })

            # Build events per device
            result = {}
            for did, rows in by_device.items():
                events = []
                prev_status = None
                for row in rows:
                    cur_status = row['status']
                    if prev_status is not None:
                        changes = []
                        for bit, label in STATUS_LABELS.items():
                            bit_prev = prev_status & bit
                            cur_bit = cur_status & bit
                            if bit_prev != cur_bit:  # flip
                                if cur_bit:  # now ON
                                    # create a descriptive phrase
                                    if bit == 0x01:
                                        changes.append('unlocked')
                                    elif bit == 0x02:
                                        changes.append('config mode on')
                                    elif bit == 0x04:
                                        changes.append('low battery')
                                    elif bit == 0x08:
                                        changes.append('power on')
                                    elif bit == 0x10:
                                        changes.append('user logged in')
                                    elif bit == 0x20:
                                        changes.append('network on')
                                    elif bit == 0x40:
                                        changes.append('parity')
                        if changes:
                            # format time as ISO string
                            t = row['report_time']
                            time_str = t.isoformat() if hasattr(t, 'isoformat') else str(t)
                            events.append({
                                'time': time_str,
                                'changes': changes,
                            })
                    prev_status = cur_status
                result[did] = {'events': events}
            self._send(200, {"history": result})
        except Exception as e:
            log.error("history error: %s", e)
            self._send(500, {"error": str(e)})