# findmy-os-daemon

Answers the ESP32 Find My beacon's `OS?` status polls over UART, telling it
this laptop's current health so it can advertise four OS status bits in its
beacon frame (battery<20%, powered-on, user-logged-in, internet reachable)
with whole-byte even parity.

The daemon is the **reply side**: the ESP32 is the client (its console sends a
bare `OS?\n`), this daemon owns `/dev/ttyACM0` and answers

```
OK OS batt=<0|1> power=<0|1> user=<0|1> net=<0|1>
```

It is built to be very hard to crash. See the header of
`findmy-os-daemon.c` for the full robustness contract.

## Build & run manually

```sh
cc -O2 -Wall -Wextra -Werror -o findmy-os-daemon findmy-os-daemon.c
./findmy-os-daemon                 # normal logs to stderr
./findmy-os-daemon -d              # verbose (DEBUG) logs to stderr
./findmy-os-daemon -d -l /var/log/findmy-os-daemon.log   # ...and to a file
./findmy-os-daemon -t /dev/ttyACM1   # non-default serial node
./findmy-os-daemon -i 15             # refresh health every 15 s (default 10)
```

`make` / `make test` build and smoke-test it.

## Install (systemd)

```sh
sudo make install     # binary -> /usr/local/bin, unit -> /etc/systemd/system
sudo systemctl enable --now findmy-os-daemon
```

Normal logs go to stderr and are captured by journald, so both these work:

```sh
systemctl status findmy-os-daemon
journalctl -u findmy-os-daemon -f
```

For verbose/file logging, call it manually with `-d -l FILE` (the systemd
unit runs it in normal mode).

## How the bits are derived

| bit | source | failure behaviour |
|---|---|---|
| `batt` | `/sys/class/power_supply/BAT*/capacity` < 20% | 0 if unreadable (safe), logged once |
| `power` | always 1 while the daemon runs | 1 |
| `login` | an interactive `USER_PROCESS` in `/run/utmp` | 0 if none |
| `net` | a default route in `/proc/net/route` (v4) or `/proc/net/ipv6_route` | 0 if none |

## Design notes

- **POSIX only** — no libudev / dbus / glib; fewer moving parts to break.
- **Runs as root** under systemd so it can open the tty and read system files.
- **Tolerates a missing / just-fit-again ACM0.** The ESP32's on-chip
  USB-Serial-JTAG drops off USB during deep sleep, so `/dev/ttyACM0`
  disappears and reappears; the daemon reconnects forever with backoff and
  never crashes.
- **Coexistence with the toolbox.** The daemon is meant to own the tty in the
  steady state. A toolbox console session opens the same node; either use one
  at a time, or stop the daemon (`systemctl stop findmy-os-daemon`) before a
  long toolbox session. The daemon itself survives the toolbox grabbing the
  port (read/write errors are handled), it just cannot answer `OS?` while
  someone else holds the line.
