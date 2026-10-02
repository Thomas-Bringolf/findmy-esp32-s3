# Roadmap

Ordered by what I would do next. Items marked *(done)* were completed in
this pass; the rest are proposals.

## Done in this pass

- Console protocol hardened and fully tested: strict numeric parsing,
  overflow-safe line reader, `WIPE`, `fw=2`, pairing window never consumed
  by a failed `KEYS` — now `findmy-toolbox.py test` (79 checks); the old
  standalone `Scripts/old/test_uart.py` is archived.
- Key rotation in the debug window actually runs (it used to wait for a GAP
  event NimBLE never sends) — verified with three rotations in 60 s.
- Power: steady-state duty cycle 100 % → **0.37 % awake per cycle**
  (≈2.2 % including the per-slot key derivation), watchdog backtraces gone
  ([power.md](power.md)).
- WIPE regression fixed: the unpaired device no longer writes zeroed key
  blobs back to NVS, so a `CONFIG` + reboot keeps `paired=0`.
- `Scripts/old/measure_power.py` for repeatable duty-cycle measurements.
- P-224 public key now cached per slot (`fm_current_pubkey`): `KEY?` and
  `CONFIG` went from 2.2 s to 60–130 ms, which also removed the intermittent
  host-timeout/off-by-one failures in the UART suite.

- Device identity + state-driven menu: `NAME`/`IDENT?` in the firmware
  (`IDENT?` is answered **while locked**), toolbox `connect` / `disconnect` /
  `reset`, `apple-id connect|disconnect`, and a menu that only offers what the
  current connections allow.
- Retrieval made trustworthy: the worker remembers the newest slot it
  reached and resumes there after an outage (capped at 24 h) instead of
  silently skipping everything older than `--back`; an empty Apple body is
  logged and retried instead of swallowed; every report archives the key
  hash it was fetched under, which arms `retrieve --doctor` (positive
  control for "is this Apple ID banned?"); `reports.json` is written
  atomically and the menu survives archive entries it cannot parse.
  Replaces `Scripts/old/retrieve_loop.sh`, which had been calling a
  trashed script since the repository cleanup.

## Power

1. **Speed up the P-224 derivation** (biggest remaining win). One
   `derive_pubkey_x()` costs **~2.2 s** of CPU — 10–30x slower than the
   textbook double-and-add needs on this hardware — so every rotation burns
   2.15 s awake (≈1.8 percentage points of duty, [power.md](power.md)).
   Candidate fixes, cheapest first: (a) precompute/reuse the reduction
   context instead of `mbedtls_mpi_mod_mpi` per operation, (b) fixed
   4×32-bit limb arithmetic with Montgomery reduction, (c) windowed
   (`w=4` + NAF) scalar multiplication instead of bit-by-bit
   double-and-add. Success criterion: rotation awake time < 100 ms with
   `verify` + `sync-ble` still matching the `findmy` chain.
2. **Measure real current** with a power profiler and record
   `--ma-awake` / `--ma-sleep` in `power.md`. Everything above the measured
   duty cycle is still an estimate.
3. **Continuous advertising in the steady state.** Today each cycle does
   stop → set data → start (≈1.5–3 ms of HCI traffic). Starting the
   instance once and letting the controller time the interval itself would
   keep the radio schedule identical while removing that cost — *if* the
   controller keeps its clock across light sleep (needs a test: scan while
   the CPU sleeps, then compare awake times).
4. **Tune the guard.** `FM_ADV_GUARD_MS=5` rounds to 0 ticks at 100 Hz and
   is replaced by one full tick (0–10 ms, ≈5 ms average) — the single
   biggest line item in the 7.4 ms. Options: raise `FREERTOS_HZ` for finer
   delays, or wait for the controller's first TX rather than a fixed delay.
5. **`CONFIG_PM_ENABLE` + tickless idle.** Would let the scheduler sleep
   between work instead of waking every tick. Must be validated against the
   NimBLE host task and is a bigger behavioural change, so it comes after
   3–4.
6. **Lower the CPU frequency while awake** (160 → 80/40 MHz) for the burst;
   the work per cycle is a handful of HCI commands (but the P-224
   derivation would get proportionally slower).
7. **Quiet mode**: drop the two `PWR` lines (or log one summary line every
   N cycles) in production builds — ~1 ms per cycle plus UART traffic.

## Robustness

1. Re-enable the task watchdog as a deliberate check now that the busy loops
   are gone, and make a panic reboot into the console instead of a silent
   crash loop.
2. Persist and report reset/brown-out reasons (`esp_reset_reason`) on the
   console, so "the slot jumped" is diagnosable.
3. Host side: retry a command once on `TIMEOUT` (the console can be busy
   with log output), and make `Beacon.cmd()` fail loudly when the reply
   belongs to the previous command.
4. Long-running soak: 24 h of `retrieve --bg` + periodic `verify`
   to catch slot drift and report gaps.

## Protocol & features

1. **Authenticated console.** The static PIN only protects a USB cable;
   an HMAC over a nonce would stop replay and make the window revocable.
2. **Secondary key chain (SKS)** — the official accessory protocol has two
   chains; only the primary is implemented, so finders using the secondary
   path would miss the beacon.
3. **`SLOT_SECONDS` at runtime** instead of build time (`FM_SLOT_SECONDS`),
   so the host `--slot-seconds` can never drift from the firmware.
4. **`HELP` / `VERSION`** console commands (there is a `fw=3` in `PONG`, but
   no way to enumerate commands).
5. **OTA** (`esp_https_ota`) so field devices do not need a USB cable.

## Retrieval & tooling

1. Apple reports for `esp32-s3-test` are still **0** — the beacon needs a
   locked iPhone nearby to be found. The loop in
   `Scripts/state/retrieve_loop.log` keeps polling; add an alert when a
   device stays at zero reports for > N hours.
2. Dashboard: surface the `PWR` duty cycle next to freshness.
3. CI: `python3 -m py_compile Scripts/findmy-toolbox.py` + an `idf.py build`,
   with `findmy-toolbox.py test` as a hardware-in-the-loop job.

## Known limitations (by design, documented)

- A `CONFIG … dbg_sec …` change applies to the **next** session only.
- The console is only reachable while the session runs (locked after every
  reset); config mode — unpaired or the factory PIN — is the escape hatch
  that never sleeps.
- Only the primary key chain is queried (1 key per slot).
