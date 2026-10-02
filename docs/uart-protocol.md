# UART protocol

Line-based console on UART0 (GPIO 43/44, 115200 8N1), exposed by the board's
USB bridge as `/dev/ttyACM0`. Implemented in
`ESP32/main/uart_cmd.c`, mirrored by
`Scripts/findmy-toolbox.py` (`Beacon.cmd()`).

Protocol version: **`PONG fw=3`**.

## Console lock

The console boots **locked** (`s_unlocked = false`, volatile — every reset
starts locked again). While locked, the only commands that execute are
`UNLOCK <pin>` and, on an already-unlocked device, `LOCK <pin>`; everything
else — `PING` included — answers `LOCKED`. A locked device can therefore be
probed for "is anyone there", but nothing else can be driven.

| State | Behaviour |
|---|---|
| **locked** (boot default) | every command → `LOCKED`; `UNLOCK <pin>` is the way out |
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
PONG fw=3 paired=1
OK UNLOCK
OK LOCK
OK KEYS
OK PIN
LOCKED
ERR PIN
ERR LOCK 30
STAT paired=1 slot=4 debug=0
STATUS paired=1 slot=4 debug=0 adv_ms=2000 rot_sec=120 dbg_sec=600
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
PING [anything]   -> PONG fw=3 paired=<0|1> | LOCKED
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
| `dbg_sec` | 60 … 3600 | 600 (the default) |

Side effects: persisted to NVS, and advertising + the rotation timer are
restarted. **A `dbg_sec` change only affects the session that starts next**
(current window length is already fixed at session start).

### STAT? / STATUS?
```
STAT?    -> STAT paired=<0|1> slot=<n> debug=<0|1>
STATUS?  -> STATUS paired=.. slot=.. debug=.. adv_ms=.. rot_sec=.. dbg_sec=..
```
Both are answered even when unpaired (`slot=0`).

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
| 0 | `FM_STATUS_UNLOCKED` | console unlocked |
| 1 | `FM_STATUS_CONFIG` | config mode (unpaired **or** factory PIN) |

The byte itself is still written at `adv_data[6]`.

## Example session (first pairing)

```
> PING
LOCKED
> UNLOCK 00000000
OK UNLOCK
> PING
PONG fw=3 paired=0
> KEYS dGVzdC1tYXN0ZXIta2V5AAAAAAA= dGVzdC1za24AAAA... 2000 120 600
OK KEYS
> PIN 12345678
OK PIN
> SLOT?
SLOT 0
> LOCK 12345678
OK LOCK
> PING
LOCKED
> UNLOCK 12345678
OK UNLOCK
> WIPE
OK WIPE                            # reboots unpaired, factory PIN
```

## Host-side usage

```bash
cd Scripts
./findmy-toolbox.py pair                        # pair (keys + fresh PIN)
./findmy-toolbox.py sync --id esp32-s3-test     # read slot
./findmy-toolbox.py unlock                      # leave the console unlocked
./findmy-toolbox.py test                        # edge cases (resets device)
```

`Beacon.cmd()` drains any late reply of the previous command first (0.05 s),
so an out-of-order reply can never be mistaken for the current one. A
session object auto-unlocks on open and auto-locks on close.

## Covered edge cases (`findmy-toolbox.py test`)

- `LOCKED` gate: every command answered `LOCKED` before `UNLOCK`
- malformed input: bad numbers, extra tokens, unknown commands, empty lines
- 400-character line dropped whole, device still responsive
- wrong PIN (`ERR PIN`), malformed PIN (`ERR ARGS`), lockout (`ERR LOCK n`)
  after 5 wrong PINs, counter cleared by the correct PIN
- `CONFIG` clamp behaviour for all three fields
- PIN rotation → config mode ends, new PIN required for the next unlock
- `WIPE` without arguments → factory reset → re-pair with a fresh PIN
- unpaired state after `WIPE` (`SLOT?`/`KEY?` → `ERR UNPAIRED`)
- deep-sleep countdown expiry → sleep → console unreachable until reset
