# UART protocol

Line-based console on UART0 (GPIO 43/44, 115200 8N1), exposed by the board's
USB bridge as `/dev/ttyACM0`. Implemented in
`ESP32/main/uart_cmd.c`, mirrored by
`Scripts/findmy-toolbox.py` (`Beacon.cmd()`).

Protocol version: **`PONG fw=4`**.

`fw=4` adds OS-reported status bits (battery/power/user/net), a whole-byte
even-parity bit, the `OS?`/`OSSTATE`/`OSMODE` status commands, and the
ESP32-initiated `OS?` poll by which the beacon fetches OS state during the
debug session and at each active-slot boundary in the steady state. The old
toolbox-only `LOWBATT` command remains on the device but is no longer used by
the toolbox (low-battery is OS-driven).

## Console lock

The console boots **locked** (`s_unlocked = false`, volatile — every reset
starts locked again). While locked, the only commands that execute are
`UNLOCK <pin>` and `IDENT?` (plus `LOCK <pin>` on an already-unlocked
device); everything else — `PING` included — answers `LOCKED`. A locked
device can therefore be probed for "is anyone there" *and* for "who are
you", but nothing else can be driven.

| State | Behaviour |
|---|---|
| **locked** (boot default) | every command → `LOCKED` except `UNLOCK <pin>` and `IDENT?` |
| **unlocked** | full command set; `dbg_sec` countdown is paused (console never expires) |
| **locked again** (`LOCK <pin>`) | countdown restarts from `dbg_sec`; on expiry the device enters the normal sleep cycle |

`UNLOCK`/`LOCK` do **not** persist across a reset: a reboot always lands
locked.

### PIN

- 8 decimal digits, stored in NVS under `pin` (`FM_PIN_LEN`).
- Factory default `00000000` (`FM_PIN_DEFAULT`) — a device that still has it
  is in **config mode**: it never sleeps, so the console stays reachable
  until a real PIN is set with `PIN <new>`.
- Changing the PIN (`PIN`) is only possible when paired (a fresh device is
  still in config mode with the factory PIN).

### Brute-force protection

- Only a wrong **`UNLOCK`** is counted (a wrong `LOCK` is not: the caller is
  already authenticated).
- After `FM_PIN_FAIL_MAX` (5) consecutive wrong PINs the device answers
  `ERR LOCK <n>` for `n` seconds; the window doubles with every further
  failure (30 s → 60 s → …) and caps at `FM_PIN_LOCKOUT_MAX_S` (900 s).
- The failure counter persists in NVS (`pinfails`) and is cleared by a
  correct PIN. The *deadline* lives in RTC memory and is dropped by
  `uart_cmd_start()` on every boot, so a deep-sleep wake or power cycle
  re-arms the window from the persisted counter instead of inheriting a
  deadline from a previous `esp_timer` epoch (where "remaining" could
  otherwise read as days).
- During a lockout even a *malformed* `UNLOCK` answers `ERR LOCK <n>` —
  the window is checked before the argument.

## Framing

- One command per line; `\r`, `\n` or `\r\n` all terminate a line (the VFS
  is configured for CR line endings, a following `\n` yields an empty line).
- Tokens are separated by single or repeated spaces; leading/trailing
  spaces are ignored.
- Line buffer: 256 bytes. **Longer lines are dropped whole** — the partial
  content is discarded and nothing is executed.
- Empty / whitespace-only lines are ignored (no reply).
- Unknown commands answer `ERR CMD`. **Extra arguments on a command that
  does not take them are an error** — they are never silently ignored —
  except `PING`, which accepts and ignores them.

## Replies

Replies are single lines without a log prefix:

```
PONG fw=4 paired=1
OK UNLOCK
OK LOCK
OK KEYS
OK PIN
LOCKED
ERR PIN
ERR LOCK 30
STAT paired=1 slot=4 debug=0
STATUS paired=1 slot=4 debug=0 adv_ms=2000 rot_sec=120 dbg_sec=600
OS batt=0 power=1 user=1 net=0
OK OSSTATE batt=0 power=1 user=1 net=0
OK OSMODE poll
```

Log output (`I (1234) uart_cmd: ...`) is interleaved on the same stream, so
hosts must **match on the reply prefix and drop log lines**, e.g.:

```python
LOG_PREFIX = re.compile(r"^[IVDEW] \(\d+\) [^:]+: ")   # in findmy-toolbox.py

REPLY_PREFIXES = ("PONG", "OK ", "ERR", "LOCKED", "SLOT ", "KEY ",
                  "STAT ", "STATUS ")

def reply_of(raw: str) -> str | None:
    text = LOG_PREFIX.sub("", raw.strip())
    for m in REPLY_PREFIXES:
        if text.startswith(m):
            return text
    return None
```

A bare `in raw` check is not enough: log lines can contain words like
`BLE_ERR_CMD_DISALLOWED`.

### Timing

Most commands answer in 60–200 ms. Two exceptions depend on the P-224
public key derivation (~2.2 s of CPU):

- `KEYS` (fresh pairing → cold key chain)
- `KEY?` / `CONFIG` **right after a slot change** — the cache is invalidated
  by rotation/pairing, so the next call pays for the derivation once

`fm_current_pubkey()` caches the result per slot, so repeated `KEY?` and
any `CONFIG` in the same slot return in 60–130 ms.

Hosts must therefore use a **≥ 6 s timeout** (`Beacon.cmd()` default).
A shorter timeout turns a slow-but-successful command into a late reply
that the *next* command reads — the off-by-one failure mode that made the
suite flaky before the cache existed.

## Commands

### PING
```
PING [anything]   -> PONG fw=4 paired=<0|1> | LOCKED
```
Always answered when unlocked, even with trailing garbage. Used as the
liveness probe. `paired=` reports the key store, not the lock state.

### UNLOCK / LOCK
```
UNLOCK <8 digits>  -> OK UNLOCK | ERR PIN | ERR ARGS | ERR LOCK <n>
LOCK   <8 digits>  -> OK LOCK   | ERR PIN | ERR ARGS | LOCKED
```
`UNLOCK` clears the failure counter on success. `LOCK` restarts the
`dbg_sec` countdown; when it expires the device publishes one normal frame
and enters the sleep cycle (see [firmware.md](firmware.md)). Trailing
tokens are `ERR ARGS`.

### IDENT? / NAME
```
IDENT?            -> IDENT name=<token|-> paired=<0|1> | ERR ARGS
NAME  <token>     -> OK NAME <token> | ERR ARGS | ERR NVS
```
The device's own name: up to `FM_NAME_LEN` (16) characters of
`[A-Za-z0-9_-]`, `-` when it has none. It is what tells several beacons
apart, so **`IDENT?` is answered even while the console is locked** — it
carries no secret. `NAME` needs an unlocked console; a bad charset, an
empty value or a trailing token is `ERR ARGS`, and the previous name stays.

`NAME` is written to NVS (key `name`) and survives reboots and pairing;
`WIPE` erases it together with everything else. `pair` sets it to the
device id, which is why device ids are restricted to the same charset and
length.

### PIN
```
PIN <8 digits>  -> OK PIN | ERR UNPAIRED | ERR ARGS | ERR NVS
```
Rotates the console PIN. Requires paired keys (so a brand-new device must
leave the factory PIN *after* `KEYS`, which is what `pair` does). Leaving
the factory PIN also ends config mode — the status byte drops
`FM_STATUS_CONFIG` and the countdown starts running.

### KEYS
```
KEYS <master_b64> <skn_b64> [<adv_ms> <rot_sec> <dbg_sec>]
     -> OK KEYS | ERR ARGS | ERR B64 | ERR NVS
```
- Requires an **unlocked** console (the old `PAIR` + 60 s window was
  replaced by the lock itself).
- Base64 blobs must decode to exactly 28 (master) and 32 (SKN) bytes.
- Optional numeric arguments are validated *before* anything is touched, so
  a malformed line never leaves a half-paired state behind.
- On success: keys stored, slot reset to 0, config applied (if given),
  advertising restarted, status byte updated (paired + PIN state).
- Trailing tokens beyond the five are `ERR ARGS`.

### WIPE
```
WIPE  -> OK WIPE | ERR ARGS | ERR NVS      (then reboot)
```
**Factory reset**: erases the whole `fmkeys` namespace — key chain, config,
**PIN and failure counter** — resets the in-RAM config to the compile-time
defaults and reboots. The device comes back **unpaired with the factory
PIN**, i.e. in config mode: the escape hatch when a PIN is forgotten. It
takes **no arguments** (the old `WIPE <pin>` form now answers `ERR ARGS`)
and needs an unlocked console.

### SLOT? / KEY?
```
SLOT?  -> SLOT <n> | ERR UNPAIRED | LOCKED
KEY?   -> KEY <x_hex56> | ERR UNPAIRED | ERR CRYPTO | LOCKED
```
`KEY?` returns the 28-byte advertising public key of the current slot as
hex — the value `findmy-toolbox.py sync-ble` / `verify` recompute with the
`findmy` library.

### DEBUG
```
DEBUG <0|1>  -> OK DEBUG <0|1> | ERR ARGS | ERR NVS
```
Persisted. `0` (default): boot messages, errors and the `PWR` telemetry.
`1`: adds per-advertisement dumps and rotation chatter.

### CONFIG
```
CONFIG <adv_ms> <rot_sec> <dbg_sec>
      -> OK CONFIG adv_ms=.. rot_sec=.. dbg_sec=.. | ERR ARGS | ERR NVS
```
Exactly three integers, no extra tokens. Numbers are parsed strictly
(rejects `abc`, `2000x`, `-1`, `+2000`, `4294967296`, empty). Well-formed
but out-of-range values are **clamped**, and the reply echoes the values
actually stored:

| Field | Allowed | Clamped to |
|---|---|---|
| `adv_ms` | 200 … 60000 | nearest bound |
| `rot_sec` | 1 … 86400 | nearest bound |
| `dbg_sec` | 60 … 86400 | 0 → default (86400); below 60 → 60; above 86400 → 86400 |

A zero/`dbg_sec` is **not** allowed: a boot with no console window would make
the beacon impossible to flash again, so `0` is pushed up to the default.

Side effects: persisted to NVS, and advertising + the rotation timer are
restarted. **A `dbg_sec` change only affects the session that starts next**
(current window length is already fixed at session start).

### LOWBATT
```
LOWBATT on [slots]
LOWBATT off        -> OK LOWBATT on loslots=.. | OK LOWBATT off | ERR ARGS |
                       ERR UNPAIRED | ERR NVS
```
Toggles low-battery mode. `on` without a count defaults to 1 (skip every
other slot); an explicit count is clamped to `1 … FM_SKIP_SLOTS_MAX` (48).
`off` disables it. Needs key material (`ERR UNPAIRED` otherwise).

In low-battery mode the beacon runs one normal active slot (light-sleep
advertising every `adv_ms`, one key rotation per `rot_sec`), then **deep
sleeps `slots` more slot-lengths** instead of deriving them. On wake it
batch-advances the one-way SK chain by the skipped slots (SHA-256 only, no
P-224) and derives the current slot's P-224 public key exactly once, so
skipped slots never cost a key derivation. The status byte reflects the
mode (`FM_STATUS_LOWBATT`). Takes effect at the end of the current slot.

### OS? / OSSTATE / OSMODE (fw=4)

```
OS?                  -> OS batt=<0|1> power=<0|1> user=<0|1> net=<0|1>
OSSTATE <b> <p> <u> <n> -> OK OSSTATE batt=.. power=.. user=.. net=..
OSMODE [direct|poll] -> OSMODE <direct|poll> | OK OSMODE <direct|poll>
```

`OS?` reports the currently latched OS status bits and is answered **even
while the console is locked** (like `IDENT?` — it carries no secret).
`OSSTATE` sets all four OS bits directly (battery is persisted to NVS, the
rest are RAM-only) and needs an unlocked console. Each argument is an
individual `0`/`1`; a fifth token or any out-of-range value is `ERR ARGS`.

`OSMODE` selects how the OS bits are fed:
- `direct` — the bits come only from `OSSTATE` (no firmware-initiated poll).
- `poll` (default) — the beacon sends `OS?\n` and latches the `OK OS …`
  reply it gets back.

### The OS? poll (beacon → OS daemon)

The beacon is the *client* here: to keep the OS status fresh it writes a bare
`OS?\n` to the UART and expects a single-line reply from the host:

```
OS?\n        (beacon -> host)
OK OS batt=0 power=1 user=1 net=0\n    (host -> beacon)
```

The reply carries only the OS-owned bits (2–5). The beacon keeps bits 0/1
(its own lock/config state) and bit 6 (parity), so the OS can never spoof
them. A missing/timed-out reply clears power/login/net (the OS is off or the
daemon is gone); battery is left as persisted so a gone daemon cannot
silently re-arm cycle-skip.

**Reset via OS?** If the reply also carries `reset=1`, the beacon reboots
into its debug window instead of just latching the bits:

```
OS?\n
OK OS batt=0 power=1 user=1 net=1 reset=1\n   -> reboot
```

This is how a sleeping beacon is woken again for flashing: the on-chip
USB-Serial-JTAG drops off USB in deep sleep, and once the beacon is in light
sleep / its console the OS? poll is the only live line to it. The toolbox
`reset-poll` command does exactly this (it answers the next `OS?` with
`reset=1`).

**Timing.** In the *debug session* the poll runs on an idle timer
(`FM_DEBUG_POLL_MS` = 5 s by default) so a host can watch it live. In the
*steady state* it fires once per **active** slot boundary. On a normal slot
end the `OS?` is sent before the ~2.2 s P-224 key derivation and read when
the derivation finishes, so the round-trip adds **no extra awake time**; in
low-battery skip mode there is no derivation to overlap, so the beacon sends
`OS?` and waits the short reply before deep-sleeping the skipped slots.
Skipped (deep-sleep) slots never poll, which gives the OS daemon a whole
slot to notice `/dev/ttyACM0` has re-appeared and reconnect.

**Daemon contract.** The OS-side responder is `daemon/findmy-os-daemon.c`
(built with `make` in `daemon/`, installed as a systemd service). It owns
`/dev/ttyACM0` in the steady state, answers `OS?\n`, and re-opens the port with
backoff when it drops off USB during deep sleep. It ignores every line that is
not `OS?` so it never steps on the human console. Ownership: the toolbox pauses
the daemon while a console session is open.

### STAT? / STATUS?
```
STAT?    -> STAT paired=<0|1> slot=<n> debug=<0|1>
STATUS?  -> STATUS paired=.. slot=.. debug=.. adv_ms=.. rot_sec=.. dbg_sec=..
                 lomode=<0|1> loslots=..
```
Both are answered even when unpaired (`slot=0`). `lomode`/`loslots` reflect
the OS-set battery flag (bit 2) and its skip count.

## Error replies

| Reply | Meaning |
|---|---|
| `LOCKED` | console locked — run `UNLOCK <pin>` |
| `ERR CMD` | unknown command |
| `ERR ARGS` | missing / extra / non-numeric / malformed argument |
| `ERR PIN` | wrong PIN for `UNLOCK`, `LOCK` |
| `ERR LOCK <n>` | brute-force lockout active for `n` seconds |
| `ERR UNPAIRED` | `SLOT?`/`KEY?`/`PIN` with no keys stored |
| `ERR B64` | base64 that does not decode to the required length |
| `ERR NVS` | NVS commit failed |
| `ERR CRYPTO` | key derivation failed |
| `TIMEOUT` | *(host side)* no reply within the deadline |

## Status byte

`config.h` defines the advertising status byte as a small bitfield, pushed to
the advertisement by `app_update_status()` → `ble_adv_set_status()` (stop →
rebuild payload → start; a no-op when advertising is not running):

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `FM_STATUS_UNLOCKED` | console unlocked (ESP32) |
| 1 | `FM_STATUS_CONFIG` | config mode, never sleeps (ESP32) |
| 2 | `FM_STATUS_LOWBATT` | OS reports battery < 20% (persisted; skips slots) |
| 3 | `FM_STATUS_POWER` | OS powered on |
| 4 | `FM_STATUS_LOGIN` | a user is logged in |
| 5 | `FM_STATUS_NET` | OS has internet access |
| 6 | `FM_STATUS_PARITY` | **even parity over the whole byte** |
| 7 | — | spare (always 0) |

Bits 3–5 are latched from `OS?` poll replies (poll mode) or set directly with
`OSSTATE` (direct mode); bit 2 is persisted in NVS and also drives the
skip-slot mechanism. The parity bit (6) is computed so the byte has an even
number of set bits; a receiver that recomputes parity can detect a corrupted
or mis-trancsmitted byte. The byte itself is still written at `adv_data[6]`.

## Example session (first pairing)

```
> PING
LOCKED
> UNLOCK 00000000
OK UNLOCK
> PING
PONG fw=4 paired=0
> KEYS dGVzdC1tYXN0ZXIta2V5AAAAAAA= dGVzdC1za24AAAA... 2000 120 600
OK KEYS
> PIN 12345678
OK PIN
> IDENT?
IDENT name=esp32-s3-test paired=1
> SLOT?
SLOT 0
> LOCK 12345678
OK LOCK
> IDENT?
IDENT name=esp32-s3-test paired=1     # still answered while locked
> UNLOCK 12345678
OK UNLOCK
> WIPE
OK WIPE                            # reboots unpaired, factory PIN
```

## Host-side usage

```bash
cd Scripts
./findmy-toolbox.py connect                     # find, identify (IDENT?), unlock
./findmy-toolbox.py disconnect                  # lock the console again
./findmy-toolbox.py reset                       # reboot over the control lines
./findmy-toolbox.py pair --force                # pair/re-key (writes NAME too)
./findmy-toolbox.py sync --id esp32-s3-test     # read slot
./findmy-toolbox.py test                        # edge cases (resets device)
```

`Beacon.cmd()` drains any late reply of the previous command first (0.05 s),
so an out-of-order reply can never be mistaken for the current one. A
session object auto-unlocks on open and auto-locks on close.

## Covered edge cases (`findmy-toolbox.py test`)

- `LOCKED` gate: every command answered `LOCKED` before `UNLOCK`
- `IDENT?` answered while locked, `IDENT? <extra>` → `ERR ARGS`
- the device name: `NAME` set/get, empty/extra/oversized/bad-charset values
  rejected, `WIPE` erases it
- malformed input: bad numbers, extra tokens, unknown commands, empty lines
- 400-character line dropped whole, device still responsive
- wrong PIN (`ERR PIN`), malformed PIN (`ERR ARGS`), lockout (`ERR LOCK n`)
  after 5 wrong PINs, counter cleared by the correct PIN
- `CONFIG` clamp behaviour for all three fields
- PIN rotation → config mode ends, new PIN required for the next unlock
- `WIPE` without arguments → factory reset → re-pair with a fresh PIN
- unpaired state after `WIPE` (`SLOT?`/`KEY?` → `ERR UNPAIRED`)
- deep-sleep countdown expiry → sleep → console unreachable until reset
