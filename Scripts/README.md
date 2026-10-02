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
    Makefile, KeyGen/            (kept as reference, not maintained)
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
| `pair` | generate keys on the PC, provision them, set a fresh PIN |
| `sync` | read the current slot over USB (`SLOT?`/`KEY?`) |
| `sync-ble` | same, from the BLE advertisement alone (no USB) |
| `devices` | list paired devices |
| `test` | console protocol edge cases (resets the device) |
| `power` | awake/sleep duty cycle from the `PWR` telemetry |
| `retrieve` | fetch location reports (`--bg`/`--status`/`--follow`/`--stop`) |
| `watch` | retrieve and keep polling |
| `monitor` | live dashboard over `reports.json` |
| `verify` | is our key on air? |
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
then rotates the PIN to a fresh random 8-digit one (`--new-pin` to choose)
and stores everything in `state/devices.json`.

| Argument | Meaning |
|---|---|
| `--port PATH` | serial device (default `/dev/ttyACM0`) |
| `--id NAME` | device id in `devices.json` (default `esp32-s3-test`) |
| `--pin PIN` | PIN used to unlock the device |
| `--new-pin PIN` | PIN to set after pairing (default: random) |
| `--force` | overwrite an existing device id |
| `--debug 0/1` | device debug flag right after pairing |
| `--adv-ms N` / `--rot-sec N` / `--dbg-sec N` | config sent with the keys |

**Sync after reboots/reflashes** so the retrieval's query window tracks the
device's actual slot counter.

See [../docs/uart-protocol.md](../docs/uart-protocol.md) for the console
protocol itself (lock, PIN, `WIPE`, …).

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
./findmy-toolbox.py retrieve --follow       # tail its log
./findmy-toolbox.py retrieve --stop         # stop it
./findmy-toolbox.py watch --interval 120    # foreground polling
```

Incremental fetch for every beacon in `devices.json`: one request per slot
(Apple caps batched responses), starting at the newest slot that already has
archived reports and never more than `--back` (30) slots back. Results are
appended to `state/reports.json` (deduplicated).

`--bg` double-forks into a daemon that holds an `flock` on
`state/retrieve.lock`, so only one worker ever runs. The first login asks
for password + 2FA and saves the session; afterwards runs are
credential-free until the session expires (delete
`state/account_state.json` to force a login).

### monitor / verify / scan

```bash
./findmy-toolbox.py monitor                     # auto-refreshing dashboard
./findmy-toolbox.py monitor --interval 2 --device esp32-s3-test
./findmy-toolbox.py monitor --once              # single render
./findmy-toolbox.py verify 15                   # is our key on air?
./findmy-toolbox.py scan 15                     # raw OF packet dump
```

`monitor` shows time since the last report (green ≤ 45 min / yellow ≤ 3 h /
red), the latest reports with slot, age, position and accuracy, and a
per-slot coverage strip. `verify` matches the advertisement on air against
every recent slot key of every paired device.

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

The menu runs the same subcommands and reports the same status line;
`-v`/`--verbose` is a CLI option (before or after the command).

## Dependencies

```bash
pip install --break-system-packages findmy bleak pyserial
```

## Archived scripts

`old/` keeps the previous toolchain for reference (Makefile, `KeyGen/`,
`pair_device.py`, `test_uart.py`, …). Nothing there is maintained or
referenced by the firmware build any more.
