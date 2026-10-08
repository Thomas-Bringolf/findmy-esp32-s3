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
# Fetch-window strategy (no hard backtrack limit). Each cycle requests:
#   1. the last RECENT_SLOTS slots below the wall-clock estimate (always),
#   2. the first FRONTIER_SLOTS still-missing slots above the watermark
#      (always) - where a beacon resumes advertising after being powered off,
#   3. in between, every SPARSE_STRIDE-th missing slot, rotating the offset
#      by one each cycle so every middle slot is probed once per
#      SPARSE_STRIDE cycles while keeping the per-cycle request count low.
# The watermark is the newest confirmed report slot. When a report is found
# at slot S, every still-missing slot in (watermark, S) gets one final full
# scan and nothing below S is ever requested again.
RECENT_SLOTS = int(os.environ.get("RECENT_SLOTS", "120"))        # 4 h of slots
FRONTIER_SLOTS = int(os.environ.get("FRONTIER_SLOTS", "120"))
SPARSE_STRIDE = int(os.environ.get("SPARSE_STRIDE", "4"))
SPARSE_PROBE_CAP = int(os.environ.get("SPARSE_PROBE_CAP", "512"))

# Rotation phase of the sparse middle sweep, per device; advances by one on
# every fetch cycle. Resets on restart, which only re-orders the sweep.
_SPARSE_PHASE: dict[str, int] = {}
API_PORT = int(os.environ.get("API_PORT", "8090"))
VERBOSE = os.environ.get("VERBOSE", "0") == "1"

log = logging.getLogger("dashboard")

# Manual slot overrides live in Postgres (devices.json is mounted read-only).
# A wake event lets an API write interrupt the fetch sleep so the new slot
# takes effect within seconds instead of after FETCH_INTERVAL.
FETCH_WAKE = threading.Event()


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


def ensure_slot_tables(conn):
    """Create the manual-slot-override table and slot_synced_at column.

    Idempotent; also covers databases created before this feature existed
    (schema.sql only runs on first container start).
    """
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS slot_overrides (
                device_id  TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                slot       INTEGER NOT NULL,
                synced_at  TIMESTAMPTZ NOT NULL,
                set_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS slot_synced_at TIMESTAMPTZ")
    conn.commit()


def apply_slot_overrides(conn, devices):
    """Overlay manual slot overrides (slot_overrides table) onto devices.json."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT device_id, slot, synced_at FROM slot_overrides")
            rows = {r["device_id"]: r for r in cur.fetchall()}
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return devices
    out = []
    for dev in devices:
        o = rows.get(dev["id"])
        if o:
            dev = dict(dev)
            dev["last_known_slot"] = int(o["slot"])
            synced = o["synced_at"]
            dev["slot_synced_at"] = (synced.isoformat()
                                     if hasattr(synced, "isoformat") else synced)
        out.append(dev)
    return out


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
                             slot_seconds, slot_synced_at, last_known_slot,
                             adv_ms, rot_sec, dbg_sec, pin, sync_method)
        VALUES (%(id)s, %(port)s, %(paired_at)s, %(master_key)s, %(skn)s,
                %(slot_seconds)s, %(slot_synced_at)s, %(last_known_slot)s,
                %(adv_ms)s, %(rot_sec)s, %(dbg_sec)s, %(pin)s, %(sync_method)s)
        ON CONFLICT (id) DO UPDATE SET
            port=EXCLUDED.port, master_key=EXCLUDED.master_key,
            skn=EXCLUDED.skn, slot_seconds=EXCLUDED.slot_seconds,
            slot_synced_at=EXCLUDED.slot_synced_at,
            adv_ms=EXCLUDED.adv_ms, rot_sec=EXCLUDED.rot_sec,
            dbg_sec=EXCLUDED.dbg_sec, pin=EXCLUDED.pin,
            sync_method=EXCLUDED.sync_method
    """
    row = dict(dev)
    row.setdefault("slot_synced_at", None)
    row.setdefault("last_known_slot", None)
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
    for dev in apply_slot_overrides(conn, load_devices()):
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

    with conn.cursor() as cur:
        cur.execute("SELECT slot FROM reports WHERE device_id=%s",
                    (dev_id,))
        known = {r[0] for r in cur.fetchall()}

    # Watermark: everything up to the newest confirmed report has had its
    # final scan and is never requested again.
    watermark = max(known) if known else -1
    if max_i <= watermark:
        log.info("%s: slot estimate %d behind newest report %d; "
                 "nothing to fetch", dev_id, max_i, watermark)
        return []

    recent_lo = max(0, max_i - RECENT_SLOTS + 1)
    missing = [s for s in range(watermark + 1, max_i + 1) if s not in known]
    recent = [s for s in range(max(recent_lo, watermark + 1), max_i + 1)
              if s not in known]
    frontier = [s for s in missing[:FRONTIER_SLOTS] if s < recent_lo]
    middle = [s for s in missing[FRONTIER_SLOTS:] if s < recent_lo]
    phase = _SPARSE_PHASE.get(dev_id, 0)
    _SPARSE_PHASE[dev_id] = (phase + 1) % SPARSE_STRIDE
    sparse = middle[phase::SPARSE_STRIDE][:SPARSE_PROBE_CAP]

    log.info("%s: watermark=%d estimate=%d plan: recent=%d frontier=%d "
             "sparse=%d (phase %d/%d, %d middle slot(s))",
             dev_id, watermark, max_i, len(recent), len(frontier),
             len(sparse), phase, SPARSE_STRIDE, len(middle))

    fresh = []

    async def request(slot: int) -> bool:
        keys = {}
        for key in acc.keys_at(slot):
            keys.setdefault(key.hashed_adv_key_b64, key)
        try:
            raw = await account.fetch_raw_reports([(list(keys.keys()), [])])
        except Exception:
            await asyncio.sleep(0.2)
            return False
        await asyncio.sleep(0.2)
        got = False
        for r in raw:
            key = keys.get(base64.b64encode(r.hashed_adv_key_bytes).decode())
            if key is None:
                continue
            r.decrypt(key)
            d = report_to_dict(r, slot, key)
            if d and insert_report(conn, dev_id, d):
                fresh.append(d)
                known.add(slot)
                got = True
        return got

    async def final_gap_scan(stop: int) -> None:
        """Last full scan of every still-unknown slot in (watermark, stop);
        afterwards nothing below `stop` is ever requested again."""
        nonlocal watermark
        todo = [s for s in range(watermark + 1, stop) if s not in known]
        if todo:
            log.info("%s: final gap scan slots %d..%d (%d slot(s)), "
                     "then they are retired",
                     dev_id, todo[0], todo[-1], len(todo))
        for slot in todo:
            await request(slot)
        watermark = stop

    # 1. Recent window, top-down. A report found here advances the watermark
    #    and retires everything below it via the one-time gap scan.
    for slot in sorted(recent, reverse=True):
        if slot <= watermark:
            continue
        if await request(slot) and slot > watermark:
            await final_gap_scan(slot)

    # 2. Frontier: the oldest still-missing slots, ascending. The sweep
    #    itself is the final scan of everything it passes; each hit only
    #    advances the watermark.
    for slot in frontier:
        if slot <= watermark:
            continue
        if await request(slot) and slot > watermark:
            watermark = slot

    # 3. Sparse middle sweep with rotating offset: this cycle probes the
    #    missing middle slots whose position n in the gap satisfies
    #    n = k*SPARSE_STRIDE + phase.
    for slot in sparse:
        if slot <= watermark:
            continue
        if await request(slot) and slot > watermark:
            await final_gap_scan(slot)

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

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/slot":
                self.api_set_slot()
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            log.error("API error: %s", exc)
            self._send(500, {"error": str(exc)})

    def do_DELETE(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/slot":
                self.api_clear_slot(parse_qs(u.query))
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            log.error("API error: %s", exc)
            self._send(500, {"error": str(exc)})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    def log_message(self, *a):
        pass  # keep the API quiet

    def api_devices(self):
        conn = db_conn()
        try:
            rows = self._query_devices(conn)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (device_id) device_id, slot, report_time
                    FROM reports
                    ORDER BY device_id, report_time DESC
                """)
                newest_reports = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            now = datetime.now(timezone.utc)
            devices = []
            for row in rows:
                ss = row["slot_seconds"] or 120
                slot = (row["override_slot"]
                        if row["override_slot"] is not None
                        else row["last_known_slot"])
                synced = (row["override_synced_at"]
                          or row["slot_synced_at"]
                          or row["paired_at"])
                # The beacon's slot counter freezes while it is unpowered
                # (e.g. the laptop it is attached to is off), so extrapolating
                # wall time from the last sync alone runs ahead of the real
                # counter. Reports are ground truth: anchor the estimate on
                # the freshest of the newest report and the last sync.
                anchor_slot = None
                anchor_time = None
                anchor_is_report = False
                if slot is not None and synced is not None:
                    if synced.tzinfo is None:
                        synced = synced.replace(tzinfo=timezone.utc)
                    anchor_slot, anchor_time = slot, synced
                rep = newest_reports.get(row["id"])
                if rep is not None:
                    rep_slot, rep_time = rep
                    if rep_time.tzinfo is None:
                        rep_time = rep_time.replace(tzinfo=timezone.utc)
                    if anchor_time is None or rep_time > anchor_time:
                        anchor_slot, anchor_time = rep_slot, rep_time
                        anchor_is_report = True
                estimate = None
                estimate_source = None
                if anchor_slot is not None and anchor_time is not None:
                    elapsed = max(0, (now - anchor_time).total_seconds())
                    estimate = anchor_slot + int(elapsed // ss)
                    estimate_source = "report" if anchor_is_report else "sync"
                devices.append({
                    "id": row["id"],
                    "slot_seconds": ss,
                    "last_seen": row["last_seen"],
                    "slot": slot,
                    "slot_synced_at": synced,
                    "estimated_slot": estimate,
                    "estimate_source": estimate_source,
                    "manual_override": row["override_slot"] is not None,
                    "override_set_at": row["override_set_at"],
                })
            self._send(200, {"devices": devices})
        finally:
            conn.close()

    @staticmethod
    def _query_devices(conn):
        sql = """
            SELECT d.id, d.slot_seconds, d.last_seen, d.last_known_slot,
                   d.slot_synced_at, d.paired_at,
                   o.slot AS override_slot,
                   o.synced_at AS override_synced_at,
                   o.set_at AS override_set_at
            FROM devices d
            LEFT JOIN slot_overrides o ON o.device_id = d.id
            ORDER BY d.id
        """
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return cur.fetchall()
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            ensure_slot_tables(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return cur.fetchall()

    def api_set_slot(self):
        """Manually set a device's current slot (server has no BLE for sync)."""
        body = self._read_json_body()
        device_id = body.get("device")
        raw_slot = body.get("slot")
        if not device_id or raw_slot is None:
            self._send(400, {"error": "need 'device' and 'slot'"})
            return
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError):
            self._send(400, {"error": "'slot' must be an integer"})
            return
        if slot < 0:
            self._send(400, {"error": "'slot' must be >= 0"})
            return
        raw_synced = body.get("synced_at")
        if raw_synced:
            try:
                synced = datetime.fromisoformat(str(raw_synced))
            except ValueError:
                self._send(400, {"error": "bad 'synced_at' ISO timestamp"})
                return
        else:
            synced = datetime.now(timezone.utc)

        conn = db_conn()
        try:
            ensure_slot_tables(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM devices WHERE id=%s", (device_id,))
                if cur.fetchone() is None:
                    self._send(404, {"error": f"unknown device {device_id!r}"})
                    return
                cur.execute("""
                    INSERT INTO slot_overrides (device_id, slot, synced_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (device_id) DO UPDATE SET
                        slot=EXCLUDED.slot,
                        synced_at=EXCLUDED.synced_at,
                        set_at=now()
                """, (device_id, slot, synced))
                cur.execute("UPDATE devices SET last_known_slot=%s WHERE id=%s",
                            (slot, device_id))
            conn.commit()
        finally:
            conn.close()
        FETCH_WAKE.set()
        log.info("manual slot override: %s -> slot %d (synced %s)",
                 device_id, slot, synced.isoformat())
        self._send(200, {"ok": True, "device": device_id, "slot": slot,
                         "synced_at": synced.isoformat()})

    def api_clear_slot(self, q):
        device_id = q.get("device", [None])[0]
        if not device_id:
            self._send(400, {"error": "need ?device=<id>"})
            return
        conn = db_conn()
        try:
            ensure_slot_tables(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM slot_overrides WHERE device_id=%s",
                            (device_id,))
                deleted = cur.rowcount
                if deleted:
                    # restore last_known_slot to the devices.json value
                    for dev in load_devices():
                        if dev["id"] == device_id:
                            cur.execute(
                                "UPDATE devices SET last_known_slot=%s "
                                "WHERE id=%s",
                                (dev.get("last_known_slot"), device_id))
                            break
            conn.commit()
        finally:
            conn.close()
        FETCH_WAKE.set()
        log.info("manual slot override cleared: %s (%d row(s))",
                 device_id, deleted)
        self._send(200, {"ok": True, "device": device_id,
                         "cleared": int(deleted)})

    def api_reports(self, query):
        q = parse_qs(query)
        device_id = q.get("device", [None])[0]
        conn = db_conn()
        try:
            sql = ("SELECT device_id, slot, type, report_time, latitude, "
                   "longitude, accuracy_m, confidence, status, status_text, "
                   "key_hash FROM reports")
            params = []
            if device_id:
                sql += " WHERE device_id = %s"
                params.append(device_id)
            sql += " ORDER BY report_time DESC LIMIT 500"
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            self._send(200, {"reports": rows})
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


# --------------------------------------------------------------------------
# Boot / entrypoint
# --------------------------------------------------------------------------

async def run_fetch_loop(account, conn):
    """Continuously fetch Apple reports for every device and write them to DB."""
    while True:
        try:
            accessories = await fetch_devices(conn)
            fresh = await fetch_cycle(account, accessories, conn)
            log.info("fetch cycle complete (new=%d devices=%d)",
                     fresh, len(accessories))
        except Exception as exc:
            log.error("fetch cycle error: %s", exc)
        if FETCH_WAKE.is_set():
            FETCH_WAKE.clear()
            continue          # slot changed mid-cycle: reload immediately
        for _ in range(max(1, int(FETCH_INTERVAL))):
            if FETCH_WAKE.is_set():
                break         # manual slot change: refetch right away
            await asyncio.sleep(1)
        FETCH_WAKE.clear()


def _fetch_thread():
    """Background Apple fetch. Does not block the JSON API."""
    try:
        account = asyncio.run(load_account())
    except Exception as exc:
        log.error("Apple session unavailable, skipping fetch loop: %s", exc)
        return
    try:
        conn = db_conn()
    except Exception as exc:
        log.error("fetch thread DB error: %s", exc)
        return
    try:
        asyncio.run(run_fetch_loop(account, conn))
    finally:
        conn.close()


def main():
    logging.basicConfig(
        level=logging.DEBUG if VERBOSE else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Seed devices from devices.json so the API lists them even before the
    # first successful Apple fetch.
    try:
        conn = db_conn()
        try:
            ensure_slot_tables(conn)
            asyncio.run(fetch_devices(conn))
            log.info("seeded %d device(s) from %s", len(load_devices()),
                     DEVICES_FILE)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("could not seed devices from %s: %s", DEVICES_FILE, exc)

    # Background Apple fetch thread (does not block the API).
    threading.Thread(target=_fetch_thread, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), ApiHandler)
    log.info("dashboard API listening on 0.0.0.0:%d", API_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()