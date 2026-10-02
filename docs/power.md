# Power

The beacon's steady state is one advertising burst per `adv_ms` with light
sleep in between. This page documents the telemetry, how to measure it, and
the numbers of the current build.

## Telemetry

`main.c` logs two lines per cycle (tag `open_haystack`, always at `INFO`,
also with `DEBUG 0`):

```
I (...) open_haystack: PWR wake t=20299613            # esp_timer µs since boot
I (...) open_haystack: PWR sleep awake_us=7360 sleep_us=1991635
```

- `awake_us` — from wake-up to the `enter_light_sleep()` call: NimBLE
  stop/set-data/start, one on-air event, slot rotation if due, logging.
- `sleep_us` — the light-sleep request for the rest of the period,
  `adv_ms - awake_us - FM_SLEEP_LATENCY_US`, floored at 20 ms.

Because the UART is clock-gated during light sleep, a `PWR` line printed
right before sleeping can straddle the sleep boundary and arrive in two
chunks. Parse the console as a byte stream split on `\n` (as
`findmy-toolbox.py power` does), not with `readline(timeout=…)`.

## Measuring

```bash
cd Scripts
./findmy-toolbox.py power --reset --dbg-sec 60 --seconds 60 \
       --json state/power_run.json
```

1. resets the device (a console is needed to talk to it),
2. sets the requested debug window and reboots so it takes effect,
3. waits the window out (the device deep-sleeps for `adv_ms` and wakes into
   the light-sleep loop),
4. captures `PWR` lines for `--seconds`,
5. reports medians, min/max and the awake duty cycle.

The device is left at that `dbg_sec` — pass `--dbg-sec 600` afterwards to
put it back to the default. It must not be in config mode (factory PIN
`00000000`): config mode never sleeps, so there would be nothing to
measure.

With measured per-state currents from a power profiler you get an average
current directly:

```bash
./findmy-toolbox.py power --reset --dbg-sec 60 --seconds 60 \
       --ma-awake 45 --ma-sleep 1.6
```

`avg = duty * I_awake + (1 - duty) * I_sleep`.

## Results (current build)

`adv_ms=2000`, `rot_sec=120`, `dbg_sec=60`, 19 cycles in 40 s
(`Scripts/state/power_run.json`):

| Metric | Value |
|---|---|
| awake, median | **7.36 ms** (min 6.89, max 14.68) |
| sleep, median | **1.992 s** |
| cycle, median | 1.999 s |
| **awake duty** | **0.37 %** |

The two ~14.5 ms outliers are *not* rotations (that run used `rot_sec=120`
over a 40 s window, so no slot advanced) — they are occasional ~2x cycles
with no identified cause; they do not affect the median.

### Rotation cost (the dominant term)

Each key rotation re-derives the P-224 public key (~2.2 s of CPU) plus one
NVS commit. Measured with `--rot-sec 20 --seconds 60`
(`Scripts/state/power_rot.json`, 30 cycles, 3 rotations):

| Cycle type | awake |
|---|---|
| normal | 7.39 ms (median) |
| rotation | **2227 / 2074 / 2159 ms** |

Overall duty over that run: **11.08 %**. Extrapolated to the shipped
`rot_sec=120` (one rotation per 60 cycles): 0.37 % + 2.15 s / 120 s
≈ **2.2 %** duty — i.e. rotations, not the advertising loop, now dominate
the awake budget. Shrinking the derivation (see
[roadmap.md](roadmap.md)) is the single biggest power win left.

The same derivation used to stall the UART console for 2.2 s on every
`KEY?`/`CONFIG` (racing the 3 s host timeout). It is now cached per slot
in `fm_current_pubkey()` and invalidated only when the key chain changes,
so those commands answer in 60–130 ms.

### What was broken before

The same measurement used to report `awake_us ≈ 1997611` — the CPU was
awake for essentially the whole 2 s period, and the task watchdog printed a
backtrace every ~5 s. Two bugs, both fixed:

1. `ble_adv_publish_once()` waited for `BLE_GAP_EVENT_ADV_COMPLETE` after
   calling `ble_gap_adv_stop()`. NimBLE never raises that event for a
   host-initiated stop of *legacy* advertising, so the wait always ran into
   its full 2 s timeout.
2. The wait loop polled with `vTaskDelay(pdMS_TO_TICKS(1))`, which is
   `vTaskDelay(0)` at `FREERTOS_HZ=100` — a busy loop that starved the idle
   task (hence the watchdog), on top of bug 1.

`stop_adv()` is now treated as synchronous, and the guard delay is at least
one tick.

## Estimating the current

With `CONFIG_PM_ENABLE` unset, the CPU runs at 160 MHz while awake and
`esp_light_sleep_start()` gates its clock while asleep. Rough ESP32-S3
figures (verify on your own board — clock tree, radio state and board
design all matter):

| State | Typical | Share of the 2 s cycle | Contribution |
|---|---|---|---|
| awake (160 MHz + one adv burst) | ~25–40 mA | 2.2 % (incl. rotation) | ~0.6–0.9 mA |
| light sleep | ~0.7–1.5 mA | 97.8 % | ~0.7–1.5 mA |
| **average** | | | **≈ 1.5–2.3 mA** |

Without the per-slot key derivation the awake share would be 0.37 % and the
average ≈ 1.1–1.6 mA.

The radio burst itself is a few hundred µs per cycle on top of that. For a
definitive number, feed `--ma-awake` / `--ma-sleep` from a power profiler.

## Where the remaining awake time goes

| Contributor | Cost |
|---|---|
| NimBLE stop → set data → start (3 HCI commands) | ~1–3 ms |
| guard delay (`FM_ADV_GUARD_MS`, one tick) | 10 ms ceiling, measured ≈ 5 ms |
| PWR log lines over UART (115200) | ~1 ms per cycle |
| key rotation (every `rot_sec`) | **~2.15 s** (P-224 derivation + NVS commit), once per slot |

## Low-battery mode

`LOWBATT on [n]` makes the beacon alternate one normal active slot with `n`
slots of **deep sleep** rather than light sleep. The `power` telemetry only
sees the *active* slot (the deep-sleep gap has no `PWR` lines), so the duty
it reports is per-active-slot, not the true long-term average. The real
saving is the idle current: deep sleep (~10 µA) replaces light sleep
(~0.7–1.5 mA) for `n/2` of the time, so the long-run average trends toward
`(active_duty·I_awake) + (sleep_fraction·I_deep + active_fraction·~1mA)`.

To observe the cycle, capture the raw console over the PWR lines: you see a
run of `PWR sleep … awake_us=7 → sleep_us=1991 ms` light cycles, then a
silence of `n·rot_sec` (deep sleep), then a wake that logs
`resumed key chain at slot k` → `slot=k+n` (the batch catch-up, one P-224)
followed by `NimBLE host synced` and the next run of light cycles.

Measured on this build (`rot_sec=20`, `LOWBATT on 1`) the deep-sleep gap and
wake catch-up are visible in that exact sequence; slot accounting stays
aligned (advances by `1 + n` per active+sleep pair, one P-224 on wake).

Ideas to push the duty cycle lower are tracked in
[roadmap.md](roadmap.md).
