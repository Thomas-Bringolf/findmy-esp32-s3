# `state_example/` — what `Scripts/state/` looks like

Everything the toolbox remembers between runs lives in **`Scripts/state/`** —
and that folder is **git-ignored on purpose**, because a real one contains:

| File | Why it must stay private |
|---|---|
| `devices.json` | your **private keys** (`master_key`, `skn`) and the console PIN |
| `account_state.json` | **Apple ID + password + anisette/2FA session** |
| `reports.json` | every **GPS coordinate** ever retrieved |
| `pushed_locations.json` | raw location archive of the old scripts |
| `toolbox.log` | command history (redacted, but still yours) |
| `retrieve.lock`, `retrieve_loop.log` | runtime lock + worker log of `retrieve --bg` |

This folder is a **fabricated stand-in** so the repository still shows the
shape of that data. Nothing here is real:

* the two devices (`bike-tag`, `laptop`) are imaginary,
* their keys were generated at random and belong to no Apple account,
* all coordinates are the placeholder `12.34xx / 67.89xx`,
* `account_state.json` holds `example.user@icloud.com` and literal
  `REPLACE-WITH-…` strings — there is no password and no session in here.

## Files

| File | Mimics |
|---|---|
| `devices.json` | the paired beacons: id, port, timing, slot sync, keys |
| `reports.json` | retrieved location reports, one bucket per device |
| `account_state.json` | the saved Apple session (password-free re-use) |
| `toolbox.log` | example of the `HH:MM:SS [CHANNEL] -> message` log |
| `power_test.json` | last `power` capture (duty-cycle telemetry) |

Runtime-only files that a real state folder also has, but that are *not*
mirrored here: `retrieve.lock` (created by `retrieve --bg`),
`retrieve_loop.log`, and the legacy `pushed_locations.json`.

## Making it real

```bash
cp -r state_example Scripts/state     # or just let the toolbox create it
cd Scripts
./findmy-toolbox.py pair              # writes devices.json (fresh keys)
./findmy-toolbox.py retrieve you@example.com   # first login -> account_state.json
./findmy-toolbox.py retrieve          # afterwards: no credentials needed
```

`Scripts/state/` is listed in `.gitignore` — **never remove that entry**,
and never `git add -f` anything from it.
