# Beacon Scripts

Python tooling for the ESP32-S3 Find My beacon, as **one executable**:
`findmy-toolbox.py`. It replaces the old `Makefile` + one-script-per-task
layout; everything lives in a single file with subcommands and an
interactive menu. All paths resolve relative to the script's own location,
so it runs from anywhere.

```bash
cd Scripts
./findmy-toolbox.py help          # command overview
./findmy-toolbox.py               # interactive menu (no args)
./findmy-toolbox.py pair          # any subcommand, e.g. pair
./findmy-toolbox.py -v pair       # debug logging (before or after the command)
```

```
Scripts/
  findmy-toolbox.py      everything: pair, sync, test, retrieve, monitor, ...
  README.md
  old/                   archived one-off scripts + the old Makefile
    pair_device.py, sync_ble.py, verify_beacon.py, test_uart.py,
    measure_power.py, retrieve_rotating.py, retrieve_reports.py,
    monitor.py, scan_findmy.py, fm_beacon.py, retrieve_loop.sh,
    fetch_once.py, Makefile, KeyGen/    (kept as reference, not maintained)
  state/                 all persistent data (never commit this!)
    devices.json          paired beacons (keys, slot sync, timing, PIN)
    reports.json          incremental archive of location reports
    account_state.json    saved Apple session (password-free re-use)
    retrieve.lock         single-instance lock for `retrieve --bg`
    toolbox.log           rotating log of every command run
    power_run.json        last `power` capture
```

## The console lock

The ESP32 console boots **locked**; `unlock <pin>` / `lock <pin>` open and
close it, and a paired device only accepts commands while unlocked. The
toolbox opens a *session* (`Beacon`) that **auto-unlocks on open and
auto-locks on close**, so individual commands just work:

- the PIN comes from `--pin`, else `state/devices.json` (`pin`), else the
  factory default `00000000`;
- if the device refuses it (`ERR PIN`) the tool prompts once on a TTY
  (default `00000000`) and, if a real PIN was entered, stores it;
  non-interactive runs abort instead of prompting;
- `ERR LOCK <n>` (brute-force lockout) aborts and prints the remaining time.

`unlock` and `lock` are also commands in their own right: they leave the
device in that state instead of re-locking at the end.

A device still on the factory PIN is in **config mode** — it never sleeps
until `pin` sets a real one.

Commands that need the console (`pair`, `sync`, `power`, `pin`, `unlock`,
`lock`, `wipe`) accept `--reset`, which pulses the reset line first — a paired
device whose countdown has expired is in its sleep loop and answers
nothing until it is reset.

## Commands

| Command | What it does |
|---|---|
| `pair` | generate keys on the PC, provision them, set a fresh PIN + name |
| `connect` | find a beacon on the TTYs, identify it (`IDENT?`), unlock it |
| `disconnect` | lock the console of the connected beacon |
| `reset` | reboot a beacon over the UART control lines (wake it up) |
| `apple-id` | `status` / `connect [apple-id]` / `disconnect` the saved session |
| `sync` | read the current slot over USB (`SLOT?`/`KEY?`) |
| `sync-ble` | same, from the BLE advertisement alone (no USB) |
| `status` | print all device config settings returned by `STATUS?` (plus the OS status bits via `OS?`) |
| `status-debug` | interactive Debug > Status: toggle the OS status bits, push or OS?-poll them |
| `devices` | list paired devices |
| `test` | console protocol edge cases (resets the device) |
| `power` | awake/sleep duty cycle from the `PWR` telemetry |
| `retrieve` | fetch location reports (`--bg`/`--status`/`--doctor`/`--follow`/`--stop`/`--restart`) |
| `watch` | start the retrieval worker if needed, then follow its log |
| `monitor` | live dashboard over `reports.json` |
| `verify` | spec-check the advertisement on air (14 checks) |
| `scan` | raw BLE scan for Find My packets |
| `pin` | set a new console PIN |
| `unlock` / `lock` | leave the console unlocked / locked |
| `wipe` | factory reset: erase keys + PIN, unpair, re-pair with `pair` |
| `log` | show or follow `state/toolbox.log` |
| `help` | command overview |

### pair

```bash
./findmy-toolbox.py pair --port /dev/ttyACM0 --id esp32-s3-test
./findmy-toolbox.py pair --id kitchen-tag --port /dev/ttyACM1 --force
```

Generates fresh master key + SKN (primary chain only), sends them with
`KEYS`, cross-checks the device's derived key against the `findmy` library,
tells the beacon its own name (`NAME <id>` — the id *is* the device name,
so it must be 1…16 characters of `[A-Za-z0-9_-]`), then rotates the PIN to
a fresh random 8-digit one (`--new-pin` to choose) and stores everything in
`state/devices.json`.

On a terminal the three timings are asked for before the device is touched;
just pressing Enter keeps the value shown in brackets, which are the same
defaults the firmware uses (`adv_ms=2000`, `rot_sec=120`, `dbg_sec=600`).
Passing the flag skips that question, and `--yes` or a non-interactive run
takes every default without asking.

| Argument | Meaning |
|---|---|
| `--port PATH` | serial device (default `/dev/ttyACM0`) |
| `--id NAME` | device id in `devices.json` (default `esp32-s3-test`) |
| `--pin PIN` | PIN used to unlock the device |
| `--new-pin PIN` | PIN to set after pairing (default: random) |
| `--adv-ms MS` | advertisement period, 200…60000 ms (prompted) |
| `--rot-sec S` | key rotation period, 1…86400 s (prompted) |
| `--dbg-sec S` | console countdown, 60…3600 s or 0 = never (prompted) |
| `--force` | overwrite an existing device id (asks to type `pair` on a TTY) |
| `--yes` / `-y` | no confirmation for `--force` |
| `--debug 0/1` | device debug flag right after pairing |
| `--adv-ms N` / `--rot-sec N` / `--dbg-sec N` | config sent with the keys |

**Sync after reboots/reflashes** so the retrieval's query window tracks the
device's actual slot counter.

See [../docs/uart-protocol.md](../docs/uart-protocol.md) for the console
protocol itself (lock, PIN, `WIPE`, …).

### connect / disconnect

```bash
./findmy-toolbox.py connect          # scan /dev/ttyACM* + /dev/ttyUSB*, ask IDENT?
./findmy-toolbox.py connect --port /dev/ttyACM1
./findmy-toolbox.py disconnect       # lock the console again
```

`connect` is how you attach to hardware you have not named yet:

1. every serial port is probed with `IDENT?` — which the firmware answers
   **even while the console is locked**; silent ports are woken with a reset
   pulse (unless `--no-reset`);
2. if more than one beacon answers, the named one is preferred, otherwise
   you pick;
3. the console is unlocked with the stored PIN (`--pin` overrides). For a
   beacon whose name matches nothing in `devices.json`, the stored PINs are
   tried — at most `4`, never enough to arm the firmware's 5-strike lockout —
   and the winner is confirmed by comparing `KEY?` against the derived key
   chain;
4. the name is (re)written so the next `connect` recognises it instantly;
5. an **unpaired** beacon is prompted for a name and offered a `pair`.

The connection (id, port, name, paired?) lives for the menu process; every
command still opens its own short session and locks the console on the way
out.

| Argument | Meaning |
|---|---|
| `--port PATH` | only this port (default: scan them all) |
| `--id NAME` | connect this entry, refuse anything else |
| `--pin PIN` | PIN to unlock with (default: stored) |
| `--no-reset` | do not wake a silent beacon |
| `--no-pair` | never offer to pair an unpaired beacon |

`disconnect` is `lock` plus dropping the connection state.

### reset

```bash
./findmy-toolbox.py reset             # reboot via DTR/RTS, wait for the console
```

Toggles the control lines (`pulse_reset()`), waits for the console to answer
again and reports who came back (`IDENT?`). Use it to wake a beacon that
sits in its sleep loop, or to restart a wedged one from the menu.

### sync / sync-ble

```bash
./findmy-toolbox.py sync --id esp32-s3-test          # USB
./findmy-toolbox.py sync-ble                          # advertisement only
./findmy-toolbox.py sync-ble --window 20 --max-slots 500
```

`sync-ble` scans for Apple Offline Finding advertisements (0x12 payload),
captures every broadcasting MAC with RSSI and last-seen time, then walks the
key chain from slot 0 until a computed MAC matches one on air. The matched
index is stored as `last_known_slot`. Fully offline-capable — only needs the
keys in `devices.json`.

The reported "estimated pair time" (now − slot × slot_seconds) can be later
than the stored pairing time — the difference is time the device spent
powered off, it does not affect sync correctness.

### status

```bash
./findmy-toolbox.py status --id Thinkpad   # print STATUS?
./findmy-toolbox.py status --reset         # wake a deep-sleeping beacon first
```

Connects, unlocks and runs `STATUS?` (and `OS?`), then prints every setting
the firmware returns: `paired`, `slot`, `debug`, `adv_ms`, `rot_sec`, `dbg_sec`
and the four OS status bits (battery/power/user/net). A `--reset` pulses the
reset line first, which is needed when the beacon is deep in a low-battery
sleep.

### status-debug

```bash
./findmy-toolbox.py status-debug            # interactive Debug > Status submenu
```

The Debug submenu toggles the four OS status bits (`batt`, `power`, `user`,
`net`) as `[x]`/`[ ]` checkboxes — never the ESP32-owned bits `unlocked`/
`config` or the computed parity bit — then writes them to the beacon in one
of two ways:

- **push**: sends `OSSTATE <batt> <power> <user> <net>` immediately.
- **os-poll**: switches the beacon to poll mode and awaits its next `OS?`;
  the toolbox answers it with the selected bits (acting as a stand-in for the
  real OS daemon) and reads back the latched state to confirm.

The OS bits ride in the advertising status byte (bits 2–5) with whole-byte
even parity on bit 6. The battery bit is persisted and also drives the skip-
slot mechanism, so an OS-set low-battery flag makes the beacon deep-sleep
`FM_SKIP_SLOTS_DEFAULT` more slots per active slot. When poll mode is on, the
toolbox auto-answers every `OS?` it sees with its current set bits (all `0`
until the submenu sets them).

### test

```bash
./findmy-toolbox.py test
./findmy-toolbox.py test --port /dev/ttyACM0 --id esp32-s3-test --no-reset
```

Hardware-in-the-loop suite for the console protocol: the `LOCKED` gate,
malformed input (bad numbers, extra tokens, unknown commands, a
400-character line dropped whole), PIN handling (`ERR PIN`, `ERR ARGS`,
the 5-failure lockout and its clearance), `CONFIG` clamping, the countdown
expiring into sleep, a reset, and finally `WIPE` + a full re-pair whose key
is cross-checked against the `findmy` derivation. Exits non-zero if any
check fails. The default run resets the device first.

### power

```bash
./findmy-toolbox.py power --reset --dbg-sec 60 --seconds 60
./findmy-toolbox.py power --reset --dbg-sec 60 --seconds 60 \
        --ma-awake 45 --ma-sleep 1.6      # -> average current
./findmy-toolbox.py power --rot-sec 20     # watch rotations in the stats
```

Sets the debug window, reboots, waits it out, captures `PWR` lines and
reports medians, min/max and the awake duty cycle (see
[../docs/power.md](../docs/power.md)).

### retrieve / watch

```bash
./findmy-toolbox.py retrieve                # saved Apple session
./findmy-toolbox.py retrieve you@example.com        # first login (password + 2FA)
./findmy-toolbox.py retrieve --bg           # background worker, loop every 90 s
./findmy-toolbox.py retrieve --status       # is the worker running?
./findmy-toolbox.py retrieve --doctor       # do known reports still come back?
./findmy-toolbox.py retrieve --follow       # tail its log
./findmy-toolbox.py retrieve --stop         # stop it
./findmy-toolbox.py retrieve --restart      # stop it, then start it again
./findmy-toolbox.py watch                   # start it if needed, then tail it
./findmy-toolbox.py watch --lines 80        # how many log lines before the tail
```

`watch` does not poll on its own any more: it follows whatever the
background worker writes to `state/toolbox.log`, so the foreground and the
background show the same thing. It starts the worker if there is none (and
refuses when there is no saved Apple session, since such a worker could not
fetch anything — run `retrieve` once to log in). Ctrl-C only ends the tail,
the worker keeps running.

Incremental fetch for every beacon in `devices.json`: one request per slot
(Apple caps batched responses), starting at the newest slot that already has
archived reports and never more than `--back` (30) slots back. Results are
appended to `state/reports.json` (deduplicated), each keeping the hash of the
key it was fetched under.

If the worker was down it does **not** silently skip the slots nobody
queried: it remembers the highest slot it reached and resumes there (capped
at 720 slots / 24 h), so a laptop that slept through the night still catches
the reports uploaded during that gap.

Before the first fetch of a run the archive is also used to check the
stored slot alignment. A report's `time` is when a finder recorded the
beacon, so it pins down the moment the device broadcast the key stored as
that report's `slot` label, while the clock model in `state/devices.json`
claims a slot for the same moment — the two only differ once the model has
drifted away from the device counter, and then by the same offset for every
report. When at least 3 archived reports over at least 2 slots agree on one
offset, the alignment is rewritten on the spot (`sync_method: "report"`)
and the fetch runs against the corrected model; weaker or conflicting
evidence only logs a warning, and reports older than the last sync are
ignored because they may belong to an earlier pairing. `sync` (USB) and
`sync-ble` are the explicit versions of the same correction.

`--doctor` is the positive control for "is my Apple ID banned?": it re-asks
Apple for keys we already hold reports for. Those reports are fresh enough
to be on the server, so *any* answer back means the session still reads
reports, while "none of the known keys came back" is the signature of a
banned or throttled account (or of a beacon nobody has seen). It needs one
report fetched after this build to be armed, because older archive entries
have no key hash.

`--bg` double-forks into a daemon that holds an `flock` on
`state/retrieve.lock`, so only one worker ever runs. The first login asks
for password + 2FA and saves the session; afterwards runs are
credential-free until the session expires (delete
`state/account_state.json` to force a login). It replaces the archived
`old/retrieve_loop.sh`, which called a script that no longer exists.

### monitor / verify / scan

```bash
./findmy-toolbox.py monitor                     # auto-refreshing dashboard
./findmy-toolbox.py monitor --interval 2 --device esp32-s3-test
./findmy-toolbox.py monitor --once              # single render
./findmy-toolbox.py verify 15                   # is our key on air?
./findmy-toolbox.py scan 15                     # raw OF packet dump
```

`monitor` shows the accessory status byte of the newest report — `0x00
(locked+sleep-cycle)` green, `UNLOCKED` yellow, `CONFIG` red, with the OS
bits (`batt`/`power`/`user`/`net`) — plus a `PARITY ERROR` tag when the
byte's even-parity bit disagrees with the rest. Then a per-bit breakdown:
one line for each defined status bit (`unlocked`, `config`, `batt`, `power`,
`user`, `net`) with its current value and how long it has been in that state
(a `≥` prefix means the archive only gives a lower bound). Then
time since the last report (green ≤ 45 min / yellow ≤ 3 h / red), the
latest reports with slot, age, position and accuracy, and a per-slot
coverage strip. The screen is one window frame: title in the top border, the worker
state, device count and clock on the right of it, the key hints in the
bottom border. The freshness line carries a progress bar filled relative to
the 3 h warning threshold, with the percentage next to it. Every fetched
report archives that byte as `status`, a decoded `status_text`, and a
`status_parity` (`"ok"`/`"bad"`) marking whether the byte's even-parity bit
checked out on ingest, and the retriever summarises the newest one per device
under `status` in `state/reports.json`. Only devices that are in
`state/devices.json` are listed: archive entries left over from wiped
devices are collapsed into a single "hidden" count line.

`verify` rebuilds the 28-byte public key from the address *and* the payload
exactly the way a Find My finder does, matches it against every recent slot
key of every paired device, then checks the frame itself in two groups:

* **static specification** — Apple id `0x004C`, OF type `0x12`, OF length
  `0x19`, 27 bytes of manufacturer data, the random-static address really
  carries `0b11` in its top two bits, the status byte uses only the bits
  `config.h` defines, the hint byte is `0x00`;
* **public key against `devices.json`** — payload suffix `key[6:28]`, the
  high-bits byte `key[0] >> 6`, the rebuilt key equals the stored key,
  `beacon_mac()` equals the advertised MAC, and `beacon_mac()` equals
  findmy's own `mac_address` (so the helper can never drift away from the
  spec while still agreeing with the firmware).

Exit codes: `0` all checks pass, `1` no packet seen, `2` packets seen but
none is ours — a reversed address ordering is called out by name, `3` a
spec or key check failed.

For a packet `verify` cannot match, `old/fetch_once.py` asks Apple about
every candidate key rebuilt from that one capture (both address orders and
all four `key[5]` top-bit variants) in a single request — a hit names the
ordering the firmware was really using. That is how the byte-order bug was
pinned down.

### pin / unlock / lock

```bash
./findmy-toolbox.py pin                         # random new PIN, stored
./findmy-toolbox.py pin --new-pin 12345678
./findmy-toolbox.py unlock                      # device left unlocked
./findmy-toolbox.py lock                        # device left locked
```

`pin` needs the device paired (otherwise the factory PIN keeps config mode
alive); it prints and stores the new PIN.

### wipe

```bash
./findmy-toolbox.py wipe                  # asks you to type 'wipe' first
./findmy-toolbox.py wipe --yes            # non-interactive confirmation
./findmy-toolbox.py wipe --id bike-tag --reset
```

Sends `WIPE`: the device drops its key chain, its console PIN and the
brute-force failure counter, then **reboots into config mode** (factory PIN
`00000000`, console always reachable, device never sleeps). Because the keys
no longer exist anywhere, the matching entry is removed from
`state/devices.json` — run [`pair`](#pair) afterwards to provision fresh
ones.

| Argument | Meaning |
|---|---|
| `--yes` / `-y` | skip the interactive confirmation (mandatory without a TTY) |
| `--id NAME` | which device to wipe (default: the only one, or a prompt) |
| `--pin PIN` | PIN used to unlock before wiping (default: stored) |
| `--port PATH` | serial device (default: the stored port) |
| `--reset` | pulse the reset line first |

### apple-id

```bash
./findmy-toolbox.py apple-id status              # who is saved?
./findmy-toolbox.py apple-id connect you@example.com
./findmy-toolbox.py apple-id disconnect          # delete the session file
./findmy-toolbox.py apple-id disconnect --yes    # non-interactive
```

`connect` logs in once (password + 2FA on the terminal) and stores the
resulting session in `state/account_state.json`, so `retrieve`, `watch` and
`monitor` never ask again. An existing session is reused as-is; passing a
*different* address switches accounts (confirmed on a TTY, `--yes` to skip).

`disconnect` deletes `state/account_state.json` — the next retrieval asks
for a login again. That only forgets the session on this machine; revoke
the app-specific password at apple.com if you want it gone everywhere.

### log

```bash
./findmy-toolbox.py log --lines 50
./findmy-toolbox.py log --follow
```

Every command logs `HH:MM:SS [CHANNEL] -> message` to stderr (coloured when
a TTY) and to `state/toolbox.log` (rotated at 1 MB × 3, mode 600), with
secrets redacted (keys, passwords, PINs).

## Interactive menu

```bash
./findmy-toolbox.py          # no arguments: numbered menu, prompt-driven
```

The menu is one window: the title in the top border, the worker state and
the clock on its right, the key hints in the bottom border. The status
block at the top always shows the two connections and the archive:

```
console : connected to 'esp32-s3-test' on /dev/ttyACM0 (paired)
apple   : connected as you@example.com
devices : 1 (esp32-s3-test)
reports : 42, newest 2min ago  ▏░░░░░░░░░░░░░░░░░
worker  : running (pid 195776)
log     : state/toolbox.log
```

Every entry is always listed, grouped by type and coloured by group:

| group | colour | entries |
|---|---|---|
| `MONITORING` | cyan | `devices`, `retrieve`, `doctor`, `watch`, `restart`, `monitor`, `log` |
| `RADIO (BLE)` | blue | `verify`, `scan`, `sync-ble` |
| `CONSOLE (UART)` | green | `connect`, `disconnect`, `reset`, `sync`, `status`, `unlock`, `lock` |
| `PROVISIONING` | yellow | `pair`, `pin`, `wipe` |
| `DEBUG` | magenta | `doctor`, `status-debug`, `test`, `power` |
| `APPLE ID` | white | `apple connect`, `apple status`, `apple disconnect` |
| `SYSTEM` | white | `help` |

Entries that cannot run right now stay visible and greyed out, with the
reason at the right of the line (for the long ones the reason takes the
place of the description): `needs
'connect'` for the UART ones until `connect` identified a beacon (then they
are pre-filled with that device's `--id`/`--port`), `no saved Apple session`
for `apple disconnect`, `logged in as …` for `apple connect`. Picking one
tells you why and returns to the menu.

The retrieval worker belongs to the menu: it is started when the menu opens
(only if there is a saved Apple session and no worker already running) and
stopped again when the menu closes, so no worker outlives the console.
`restart` (menu entry for `retrieve --restart`) stops and starts it while
you watch, `watch` shows its log, and the status block says which state it
is in.

Pick a number, or type a command name by hand. `connect` sets the
connection, `disconnect` and `wipe` clear it (a wiped beacon has to be
identified and paired again). An empty line just refreshes the screen.
`-v`/`--verbose` is a CLI option (before or after the command).

## Dependencies

```bash
pip install -r ../requirements.txt          # findmy, bleak, pyserial
# or: pip install --break-system-packages findmy bleak pyserial
```

No ESP-IDF needed here — that toolchain is only for building and flashing
the firmware in `ESP32/`.

## Archived scripts

`old/` keeps the previous toolchain for reference (Makefile, `KeyGen/`,
`pair_device.py`, `test_uart.py`, …). Nothing there is maintained or
referenced by the firmware build any more.
