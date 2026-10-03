# Firmware

`ESP32/` — ESP-IDF 6.1 + NimBLE, built with
`idf.py build`, flashed with `idf.py -p /dev/ttyACM0 flash`.

## Files

| File | Responsibility |
|---|---|
| `main.c` | boot / debug-session / deep-sleep / light-sleep state machine, power telemetry, lock + config mode |
| `ble_adv.c` | NimBLE host setup, advertising payload + random address, key rotation, publish-one-burst |
| `uart_cmd.c` | line reader + command dispatch (`uart_cmd.h` exposes `uart_cmd_start` and the `app_*` hooks) |
| `findmy_keys.c` | X963 KDF, P-224 arithmetic, NVS store, config clamping, console PIN (`findmy_keys.h`) |
| `beacon.h` | what `main.c` and `ble_adv.c` share: `adv_data`, `rtc_state`, the `ble_adv_*` entry points |
| `config.h` | timing constants, `fm_rtc_state_t`, `ADV_DATA_LEN`, PIN constants, `FM_STATUS_*` |

There is no compile-time pairing PIN any more: it used to be injected by the
root `CMakeLists.txt` from `KeyGen/keys/pairing_pin.txt`, which is gone — the
console PIN lives in NVS and the lock model below replaces the old 60 s
pairing window.

## Console lock & config mode

Two volatile flags in `main.c`, both reported in the advertising status byte:

```c
static bool s_unlocked;    // console lock, false on every boot
static bool s_countdown;   // dbg_sec countdown running?
static int64_t s_deadline_us;
```

- **`app_set_unlocked(bool)`** — flips `s_unlocked`, updates the status
  byte and (when locking) (re)arms `s_deadline_us`.
- **`config_mode()`** = `!fm_is_paired() || strcmp(fm_get_pin(),
  FM_PIN_DEFAULT)==0`. In config mode the session never counts down and the
  device never sleeps: the console is reachable until a real PIN is set.
- **`app_update_status()`** — recomputes the status byte
  (`FM_STATUS_UNLOCKED`, `FM_STATUS_CONFIG`) after `KEYS`/`PIN`/`WIPE`.
- `uart_cmd.c` refuses every command except `UNLOCK` and `IDENT?` while
  locked — `IDENT?` is how a host tells two beacons apart without a PIN
  ([uart-protocol.md](uart-protocol.md)).

## State machine

```c
app_main()
  ├─ woke from a timer deep-sleep?  ──> run_light_sleep_cycle()   // never returns
  └─ otherwise                      ──> run_session()
```

**`run_session()`** (formerly `end_debug_session`): restores log levels,
initialises NVS and the key store, starts `uart_cmd` (which drops any stale
lockout deadline), forces `app_set_unlocked(false)`, applies the current key,
sets the status byte, starts NimBLE and the advertising chain, then blocks
until the countdown expires — unless config mode is active, in which case it
blocks forever. Unlocking pauses the countdown; `LOCK` restarts it.

**`enter_sleep_cycle()`**: publishes one *normal* frame (`adv_data[6]=0x00`,
the one that outlives the window), clears `rtc_state.paired` when there are
no keys, shuts NimBLE down and deep-sleeps for `adv_ms`.

**Wake**: `app_main` only re-enters the light-sleep loop when
`rtc_state.magic`, `rtc_state.sleep_bit` and a timer wakeup all match;
otherwise it is a fresh boot into a new session (locked again — the lock is
RAM-only). Inside `run_light_sleep_cycle()` an unpaired device returns
immediately, which lands it in `run_session()` — that is the config-mode
fallback after a wake (it also overrides `dbg_sec=0` while unpaired, so the
console can never become unreachable).

**Steady state** (`run_light_sleep_cycle`, paired):

```c
t_wake = esp_timer_get_time();
ble_adv_publish_once();                 // one burst, ~7 ms
if (++rtc_state.i >= cycles_per_slot()) fm_advance_slot();   // key rotation
awake = esp_timer_get_time() - t_wake;
sleep = adv_ms - awake - FM_SLEEP_LATENCY_US;               // floor 20 ms
enter_light_sleep(sleep);
```

`cycles_per_slot()` = `rot_sec*1000 / adv_ms` (60 cycles per slot at the
defaults). The cycle counter lives in RTC memory, so deep sleep does not
reset the slot schedule.

**Low-battery mode** (`LOWBATT on|off`, persisted). When on, one active slot
of the loop above is followed by a deep sleep of `fm_skip_slots()` more
slot-lengths instead of continuing to light-sleep the next slot. The
boundary is checked in the slot roll-over branch of the loop:

```c
if (++rtc_state.i >= cycles_per_slot()) {
    if (low_battery && skip > 0) {
        rtc_state.lowbat_skip = 1;
        ble_adv_shutdown();
        enter_deep_sleep(skip * rot_sec);    // step over n slots
    } else {
        fm_advance_slot(); ble_adv_apply_current_key();   // normal rotation
    }
}
```

On the deep-sleep wake (`rtc_state.lowbat_skip`), `run_light_sleep_cycle()`
calls `fm_advance_slots(skip)` — advancing the one-way SK chain by the
skipped slots through the SHA-256 KDF only (no P-224 per slot) — then
`ble_adv_apply_current_key()` derives the current slot's public key exactly
once. Skipped slots therefore never cost a key derivation, and the slot
index stays aligned with the host (one advance per `rot_sec` of real time,
active or sleeping). The mode only affects steady state; the cold-boot debug
session and config mode are unchanged.

## OS status bits (fw=4)

`adv_data[6]` bits 2–5 carry OS-reported state. State lives in
`uart_cmd.c` (power/login/net in RAM, battery persisted in `findmy_keys` as
`fm_lowbatt`) and `main.c` composes the byte (`status_compute()`), folding in
whole-byte even parity on bit 6 (`fm_status_with_parity`). The OS bits are
fed two ways:

- **Direct** (`OSMODE direct`): `OSSTATE <b> <p> <u> <n>` sets them.
- **Poll** (`OSMODE poll`, default): the beacon sends `OS?\n` and latches the
  `OK OS batt=.. power=.. user=.. net=..` reply.

The write path is shared: `app_os_set()` (direct) and `app_os_poll_read()`
(poll) both persist the battery via `fm_set_os_battery()` and bump the
status byte through `app_update_status()`.

**Poll timing.** In the debug-session console loop (`uart_cmd_task`) the poll
runs on an idle timeout (`FM_DEBUG_POLL_MS`, 5 s) whenever paired and in poll
mode — only the `uart_cmd` task reads stdin, so its `OS?` reply is consumed by
the poll and never mistaken for a console command. In the steady-state
light-sleep loop, `slot_end()` polls once per active-slot boundary: the `OS?`
is sent before the ~2.2 s key derivation and read right after it (no added
awake time); in low-battery skip mode there is no derivation, so the beacon
sends `OS?` and waits the short reply before the deep-sleep skip. Skipped
(deep-sleep) slots never poll — the whole-slot gap lets an OS daemon notice
`/dev/ttyACM0` re-appeared and reconnect.

**Deep sleep + USB-JTAG.** This board exposes the console through the on-chip
USB-Serial-JTAG (not a CP210x), which powers down in deep sleep: `/dev/ttyACM0`
drops off USB and re-enumerates on wake. That is why the poll is tied to
active slots, and why an OS-side responder must tolerate the port's
disappearance/reappearance.

## Advertising

Frame (`adv_data`, 31 bytes, built by `set_adv_data()`):

| Offset | Content |
|---|---|
| 0…1 | length / type (generated by `ble_hs_adv_fields`) |
| 2…3 | Apple company ID `0x4c 00` (part of `mfg_data`) |
| 4…5 | `0x12 0x19` (offline finding type + 25-byte length) |
| 6 | **status byte**: `FM_STATUS_UNLOCKED`/`FM_STATUS_CONFIG`/(OS) `FM_STATUS_LOWBATT`/`POWER`/`LOGIN`/`NET` + even-parity `FM_STATUS_PARITY` bitfield. ESP32-owned bits 0-1, OS-owned 2-5, parity 6 (see [uart-protocol.md](uart-protocol.md)). `0x02` config, `0x01` unlocked; in the field the OS bits reflect the latched/polled OS state and bit 6 makes the byte even-parity |
| 7…28 | the 22 advertising-key bytes `key[6:28]` |
| 29 | `key[0] >> 6` — the two bits the address is forced to `0b11` |
| 30 | hint, `0x00` (this firmware never sets it) |

Payload as seen by a scanner (the 27 bytes of `mfg_data` after the company
ID): `0x12 0x19 status key[6:28] key[0]>>6 hint`.

- Random static address (OF paper Tab. 2): `(pi[0]|0b11<<6) || pi[1..5]` in
  display order. NimBLE wants host byte order (little-endian, `ble_hs_id.h`),
  so `set_addr_from_key()` stores it backwards: `addr[5] = key[0]|0xC0` (the
  octet BLE validates) … `addr[0] = key[5]`. `key[0]`'s own top bits are
  re-advertised in the payload as `key[0] >> 6`.
- `apply_adv_interval()` converts `adv_ms` to 0.625 ms units, clamped to the
  controller's 0x20…0x4000 window.
- TX power is `ESP_PWR_LVL_P9` (+9 dBm).
- `start_chain()` is idempotent: it stops first, so boot `on_sync`, the
  debug session and a re-key can all call it.

### NimBLE quirks that shaped this code

1. **A host-initiated `ble_gap_adv_stop()` never raises
   `BLE_GAP_EVENT_ADV_COMPLETE`** for legacy advertising — NimBLE only sends
   `LE_SET_ADV_ENABLE=0`; the completion event is reserved for
   controller-initiated termination (directed-adv timeout, connection, …).
   Any "stop and wait for the callback" therefore blocks until its timeout.
   `stop_adv()` is treated as synchronous, and rotation/re-key advance the
   chain directly instead of from the callback. `gap_event_cb` keeps one
   safety net: if advertising ends on its own, restart it.
2. **Disabling an instance that is not running answers "command
   disallowed"** (log noise, and a non-zero return). `stop_adv()` checks its
   own `advertising` flag first and maps the error to `BLE_HS_EALREADY`.
3. **`pdMS_TO_TICKS(1)` is `0`** at `FREERTOS_HZ=100`. Any loop that
   "polls every 1 ms" with it busy-spins, starves the idle task and trips
   the task watchdog. The guard delay is at least one tick (10 ms).

## Key rotation

Two drivers, one effect (`fm_advance_slot()` → `fm_key_init()` →
`ble_adv_apply_current_key()`):

- **Debug window**: `rotation_timer` (esp_timer, periodic `rot_sec`) →
  stop → advance → apply → `ble_adv_start_chain()`. Re-armed on every call
  so a `CONFIG … rot_sec …` change takes effect immediately.
- **Steady state**: no timers run across light sleep, so the wake loop counts
  cycles and advances the slot when `rtc_state.i` reaches
  `cycles_per_slot()`.

`app_on_keys_changed()` (called after a successful `KEYS`) applies the new
key and restarts the chain, which also restarts the rotation timer.

Deriving the advertising public key costs ~2.2 s of CPU
(`derive_scalar` + `p224_mul_gen`), so `fm_current_pubkey()` caches it for
the current slot; `fm_key_init` / `fm_advance_slot` / `fm_pair` /
`fm_unpair` invalidate it. Every rotation therefore pays the derivation
once, while the console (`KEY?`, `CONFIG`) reuses it.

The SK chain is one-way (`SK_{i+1} = X963-KDF(SHA256,"update",SK_i)`), so
losing the chain state only costs the slots that were skipped while the
device was off.

## NVS layout (`fmkeys` namespace)

| Key | Type | Meaning |
|---|---|---|
| `mk`, `skn`, `sk` | blob | master key, SKN, current SK |
| `slot` | u32 | current slot index |
| `dbg` | blob(1) | debug flag |
| `advms`, `rotsec`, `dbgsec` | u32 | config (clamped on load *and* write) |
| `lomode` | u8 | low-battery mode enabled flag |
| `loslots` | u32 | low-battery slots to skip per active slot (clamped 1..48) |
| `pin` | blob(8) | console PIN (8 digits; factory default `00000000`) |
| `pinfails` | u32 | consecutive failed `UNLOCK` attempts |
| `name` | str | device name for `IDENT?` (1..16 of `[A-Za-z0-9_-]`, `""` = none) |

`fm_unpair()` (the `WIPE` command) erases the whole namespace — keys, config,
**PIN and failure counter** — and resets the in-RAM config to the
compile-time defaults, so a wiped device boots back into config mode.

**Persistence rule:** `nvs_write_all()` writes the key blobs only while
`fm_paired`, and erases them otherwise. Writing the zeroed buffers of an
unpaired device would leave "all four values present" behind, and the next
boot would report `paired=1` with an all-zero key chain. `fm_key_init()`
additionally rejects an all-zero master key, which repairs a device that
already has such a state.

## Logging

`run_session()` restores `esp_log_level_set("*", ESP_LOG_INFO)`.
`run_light_sleep_cycle()` sets `*` to `ESP_LOG_NONE` (except its own tag at
`INFO`) when the debug flag is off, so the steady state prints only the two
`PWR` lines per cycle.

## Timing constants (`config.h`)

| Constant | Value | Meaning |
|---|---|---|
| `FM_ADV_GUARD_MS` | 5 ms | how long a burst keeps the instance alive (rounded up to one tick) |
| `FM_MIN_SLEEP_US` | 20 ms | floor for every sleep request |
| `FM_SLEEP_LATENCY_US` | 1 ms | light-sleep entry/wake budget subtracted from the request |
| `FM_RTC_MAGIC` | `0x4D485931` | marks valid RTC state |
| `FM_SLOT_SECONDS` | 120 | slot length (build-time, must match the host's slot length) |
| `FM_SKIP_SLOTS_DEFAULT` | 1 | low-battery slots to skip when `LOWBATT on` without a count |
| `FM_SKIP_SLOTS_MAX` | 48 | clamp for low-battery skip count (also bounds the catch-up loop) |
| `FM_PIN_LEN` | 8 | console PIN length (decimal digits) |
| `FM_PIN_DEFAULT` | `00000000` | factory PIN; having it means config mode |
| `FM_PIN_FAIL_MAX` | 5 | failed `UNLOCK`s before a lockout |
| `FM_PIN_LOCKOUT_BASE_S` | 30 s | lockout after the 5th failure (doubles per further failure) |
| `FM_PIN_LOCKOUT_MAX_S` | 900 s | lockout cap |
