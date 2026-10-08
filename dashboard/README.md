# Find My Dashboard
A standalone Dockerized web dashboard for the ESP32 Find My tracker.

It shows a world map of past beacon positions, colour-coded by device status,
and fans out to multiple tracked devices.

## Architecture

```
  ┌────────────────────────  Docker Compose  ─────────────────────────┐
  │                                                                   │
  │  ┌─────────┐        ┌──────────────┐        ┌─────────────────┐  │
  │  │  nginx  │ ─────▶ │   backend    │ ─────▶ │ Postgresql 15   │  │
  │  │ (web /) │  /api/ │  (Python)    │   SQL  │  (reports DB)   │  │
  │  └─────────┘        └──────┬───────┘        └─────────────────┘  │
  │                            │ Apple Find My (free)                  │
  │                            ▼                                      │
  │                     apple + findmy lib                            │
  └──────────────────────────────────────────────────────────────────┘
```

- **nginx** serves the static frontend (Leaflet map + controls) and
  reverse-proxies `/api/*` to the backend.
- **backend** (Python, uses the free `findmy` library) logs in to Apple once,
  periodically fetches new Offline-Finding reports, decrypts them and stores
  them in PostgreSQL. It also exposes a small JSON API.
- **PostgreSQL** stores devices + reports. The schema lives in `init/schema.sql`
  and is applied automatically on first start.

## Prerequisites

1. `docker` and `docker compose` installed.
2. A saved Apple session for `findmy`. The backend restores it from
   `account_state.json`. Create it once from the repo root:
   ```bash
   python3 Scripts/findmy-toolbox.py apple-id connect   # enter Apple ID + 2FA
   ```
   This writes `Scripts/state/account_state.json`.
3. At least one paired device in `Scripts/state/devices.json` (the toolbox
   `pair` command creates this). The backend seeds the DB from it.

## Run

From inside `dashboard/`:

```bash
ESP32_TRACKER_DIR=/abs/path/to/ESP32-tracker/Scripts/state docker compose up --build
```

- Web UI: <http://localhost:8080>
- Only `127.0.0.1:5432` (DB) and `${WEB_PORT:-8080}` / :8080 are exposed.
  The backend does **not** expose a host port; nginx reverse-proxies its API.

> **Map API key:** the basemap tiles are served by Carto, which needs an API
> key. The key lives in `web/config.js` — **this file is gitignored and is not
> committed**. Copy `web/config.example.js` to `web/config.js` and paste your
> real key there before starting the stack:
> ```bash
> cp web/config.example.js web/config.js   # then edit web/config.js with your key
> ```

### Configuration (env vars)

| Variable                | Default | Meaning                        |
|-------------------------|---------|--------------------------------|
| `FETCH_INTERVAL`        | `900`   | seconds between Apple fetches  |
| `RECENT_SLOTS`          | `120`   | newest slots (4 h) probed every cycle |
| `FRONTIER_SLOTS`        | `120`   | oldest missing slots probed every cycle |
| `SPARSE_STRIDE`         | `4`     | probe every n-th slot of the gap between, rotating offset per cycle |
| `SPARSE_PROBE_CAP`      | `512`   | max sparse probes per cycle (request budget) |
| `VERBOSE`               | `0`     | set `1` for debug logging      |
| `WEB_PORT`              | `8080`  | host port for the web UI       |
| `ESP32_TRACKER_DIR`     | `../Scripts/state` | path to live state (Apple session + keys) |

## Data model

The map dot colouring:

- **fill** = OS running (`status & 0x08`)
- **outline** = user logged in (`status & 0x10`)
- **opacity** = from the slider (hours); dots newer than the slider window are
  100% opaque, older ones fade to transparent.

Reports are stored in the `reports` table (~ the toolbox's `reports.json` but
in SQL), one row per device + slot, so multiple devices are tracked and shown
on the same map.

## Components

| Path            | What it is                       |
|-----------------|----------------------------------|
| `docker-compose.yml` | orchestrates db / backend / web |
| `init/schema.sql`    | PostgreSQL schema              |
| `backend/app.py`     | Apple-fetch + SQL + JSON API   |
| `backend/Dockerfile` | backend image                  |
| `nginx.conf`         | static + `/api` reverse proxy  |
| `web/index.html`     | frontend markup                |
| `web/style.css`      | styling                        |
| `web/app.js`         | Leaflet map + controls         |
