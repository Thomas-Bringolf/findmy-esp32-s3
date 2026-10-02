#!/usr/bin/env python3
"""
Flask dashboard for ESP32 Find My beacon metadata.

Shows device info from devices.json, report stats from reports.json,
and pushed status from pushed_locations.json.
Listens on port 8080.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template_string, jsonify

SCRIPTS_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPTS_DIR / "state"
DEVICES_JSON = STATE_DIR / "devices.json"
REPORTS_JSON = STATE_DIR / "reports.json"
PUSHED_JSON = STATE_DIR / "pushed_locations.json"

app = Flask(__name__)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def get_device_status(dev: dict, reports: dict, pushed: dict) -> dict:
    dev_id = dev["id"]
    paired_at = datetime.fromisoformat(dev["paired_at"])
    slot_seconds = int(dev.get("slot_seconds", 120))
    slot_synced_at = dev.get("slot_synced_at")
    last_known_slot = dev.get("last_known_slot")

    # Current slot estimate
    now = datetime.now(timezone.utc)
    alignment_date = paired_at
    alignment_index = 0
    if slot_synced_at and last_known_slot is not None:
        alignment_date = datetime.fromisoformat(slot_synced_at)
        alignment_index = int(last_known_slot)
    elapsed = (now - alignment_date).total_seconds()
    current_slot = alignment_index + int(elapsed / slot_seconds)

    # Report stats
    dev_reports = reports.get(dev_id, {}).get("reports", [])
    report_count = len(dev_reports)
    last_fetched_slot = reports.get(dev_id, {}).get("last_fetched_slot")

    # Pushed stats
    dev_pushed = pushed.get(dev_id, [])
    pushed_count = len(dev_pushed)

    # Last report time
    last_report_time = None
    if dev_reports:
        last_report_time = max(d["time"] for d in dev_reports)

    # Last pushed time
    last_pushed_time = None
    if dev_pushed:
        last_pushed_time = max(d["time"] for d in dev_pushed)

    # Last known state byte (from most recent report if available)
    last_state_byte = None
    if dev_reports:
        # We don't store state byte in reports, but we know:
        # - Debug mode (first 10 min) = 0x01
        # - Sleep mode = 0x00
        # Estimate based on time since pairing vs debug window
        debug_window_s = 60  # 1 minute
        time_since_pair = (now - paired_at).total_seconds()
        if time_since_pair < debug_window_s:
            last_state_byte = 0x01
        else:
            last_state_byte = 0x00

    return {
        "id": dev_id,
        "paired_at": paired_at.isoformat(),
        "slot_seconds": slot_seconds,
        "current_slot_estimate": current_slot,
        "last_known_slot": last_known_slot,
        "last_synced_at": slot_synced_at,
        "report_count": report_count,
        "pushed_count": pushed_count,
        "last_fetched_slot": last_fetched_slot,
        "last_report_time": last_report_time,
        "last_pushed_time": last_pushed_time,
        "last_state_byte": f"0x{last_state_byte:02x}" if last_state_byte is not None else "unknown",
        "master_key_preview": dev["master_key"][:16] + "...",
        "debug_enabled": dev.get("debug", False),
    }


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>ESP32 Find My Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; margin-bottom: 10px; }
        .subtitle { color: #666; margin-bottom: 30px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 20px; }
        .card { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .card h2 { margin: 0 0 15px 0; padding-bottom: 10px; border-bottom: 1px solid #eee; color: #333; }
        .row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #f0f0f0; }
        .row:last-child { border-bottom: none; }
        .label { color: #666; font-weight: 500; }
        .value { font-family: monospace; color: #333; text-align: right; }
        .state-0x01 { color: #e67e22; font-weight: bold; }
        .state-0x00 { color: #27ae60; font-weight: bold; }
        .state-unknown { color: #999; }
        .timestamp { color: #666; font-size: 0.9em; }
        .refresh-btn { position: fixed; bottom: 20px; right: 20px; background: #3498db; color: white; border: none; padding: 12px 20px; border-radius: 50px; cursor: pointer; box-shadow: 0 2px 10px rgba(52,152,219,0.3); }
        .refresh-btn:hover { background: #2980b9; }
        .empty { color: #999; font-style: italic; }
    </style>
</head>
<body>
    <h1>ESP32 Find My Dashboard</h1>
    <div class="subtitle">Last updated: <span id="last-update">{{ last_update }}</span></div>

    <div class="grid">
        {% for device in devices %}
        <div class="card">
            <h2>{{ device.id }}</h2>
            <div class="row">
                <span class="label">Paired</span>
                <span class="value timestamp">{{ device.paired_at }}</span>
            </div>
            <div class="row">
                <span class="label">Slot interval</span>
                <span class="value">{{ device.slot_seconds }}s</span>
            </div>
            <div class="row">
                <span class="label">Current slot (est.)</span>
                <span class="value">{{ device.current_slot_estimate }}</span>
            </div>
            <div class="row">
                <span class="label">Last known slot (synced)</span>
                <span class="value">{{ device.last_known_slot if device.last_known_slot is not none else 'never' }}</span>
            </div>
            <div class="row">
                <span class="label">Last sync</span>
                <span class="value timestamp">{{ device.last_synced_at if device.last_synced_at else 'never' }}</span>
            </div>
            <div class="row">
                <span class="label">Reports in archive</span>
                <span class="value">{{ device.report_count }}</span>
            </div>
            <div class="row">
                <span class="label">Pushed to Dawarich</span>
                <span class="value">{{ device.pushed_count }}</span>
            </div>
            <div class="row">
                <span class="label">Last fetched slot</span>
                <span class="value">{{ device.last_fetched_slot if device.last_fetched_slot is not none else 'never' }}</span>
            </div>
            <div class="row">
                <span class="label">Last report time</span>
                <span class="value timestamp">{{ device.last_report_time if device.last_report_time else 'none' }}</span>
            </div>
            <div class="row">
                <span class="label">Last pushed time</span>
                <span class="value timestamp">{{ device.last_pushed_time if device.last_pushed_time else 'none' }}</span>
            </div>
            <div class="row">
                <span class="label">Last state byte</span>
                <span class="value {% if device.last_state_byte == '0x01' %}state-0x01{% elif device.last_state_byte == '0x00' %}state-0x00{% else %}state-unknown{% endif %}">{{ device.last_state_byte }}</span>
            </div>
            <div class="row">
                <span class="label">Debug enabled</span>
                <span class="value">{{ 'yes' if device.debug_enabled else 'no' }}</span>
            </div>
            <div class="row">
                <span class="label">Master key</span>
                <span class="value">{{ device.master_key_preview }}</span>
            </div>
        </div>
        {% endfor %}
    </div>

    {% if not devices %}
    <div class="card" style="text-align: center; padding: 60px;">
        <h2>No devices configured</h2>
        <p>Run <code>pair_device.py</code> to pair a beacon.</p>
    </div>
    {% endif %}

    <button class="refresh-btn" onclick="location.reload()">Refresh</button>

    <script>
        // Auto-refresh every 30 seconds
        setTimeout(() => location.reload(), 30000);
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    devices_data = load_json(DEVICES_JSON, {"devices": []})
    reports = load_json(REPORTS_JSON, {})
    pushed = load_json(PUSHED_JSON, {})

    devices = []
    for dev in devices_data.get("devices", []):
        devices.append(get_device_status(dev, reports, pushed))

    return render_template_string(HTML_TEMPLATE,
                                  devices=devices,
                                  last_update=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@app.route("/api/status")
def api_status():
    devices_data = load_json(DEVICES_JSON, {"devices": []})
    reports = load_json(REPORTS_JSON, {})
    pushed = load_json(PUSHED_JSON, {})

    devices = []
    for dev in devices_data.get("devices", []):
        devices.append(get_device_status(dev, reports, pushed))

    return jsonify({
        "last_update": datetime.now().isoformat(),
        "devices": devices
    })


if __name__ == "__main__":
    port = 8080
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    print(f"Starting dashboard on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)