-- Find My Dashboard - PostgreSQL schema
-- Runs automatically on first container start (docker-entrypoint-initdb.d).

BEGIN;

-- Devices we track. Key fields mirror Scripts/state/devices.json so the
-- backend can seed the DB from it on first boot.
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,          -- e.g. 'Thinkpad-Yellow'
    port        TEXT,
    paired_at   TIMESTAMPTZ,
    master_key  TEXT,                      -- base64
    skn         TEXT,                      -- base64
    slot_seconds INTEGER,
    last_known_slot INTEGER,
    adv_ms      INTEGER,
    rot_sec     INTEGER,
    dbg_sec     INTEGER,
    pin         TEXT,
    sync_method TEXT,
    last_seen   TIMESTAMPTZ                -- most recent report time
);

-- One row per Apple Offline-Finding report slot for a device. This is the
-- "reports.json but in SQL" equivalent.
CREATE TABLE IF NOT EXISTS reports (
    id            BIGSERIAL PRIMARY KEY,
    device_id     TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    slot          INTEGER NOT NULL,
    type          TEXT,                    -- 'primary'
    report_time   TIMESTAMPTZ NOT NULL,    -- when the position was recorded
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    accuracy_m    INTEGER,
    confidence    INTEGER,                 -- Apple's confidence 0..10
    status        INTEGER,                 -- bitfield, see config.h FM_STATUS_*
    status_text   TEXT,
    key_hash      TEXT,
    retrieved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (device_id, slot)
);

CREATE INDEX IF NOT EXISTS idx_reports_device_time
    ON reports(device_id, report_time DESC);
CREATE INDEX IF NOT EXISTS idx_reports_time
    ON reports(report_time DESC);
CREATE INDEX IF NOT EXISTS idx_reports_device_status
    ON reports(device_id, status);

COMMIT;
