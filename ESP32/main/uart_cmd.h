#pragma once

#include <stdbool.h>

#include <stdint.h>
#include <stddef.h>

/* Start the UART command task (line-based protocol over the USB console).
 * Only runs during the post-boot debug window; the light-sleep cycle has
 * no console. Also invalidates any lockout deadline inherited from a
 * previous boot. */
void uart_cmd_start(void);

/* Called by uart_cmd after new key material or timing parameters were
 * accepted, so the application can (re)configure advertising.
 * Implemented in ble_adv.c. */
void app_on_keys_changed(void);

/* Console lock state, owned by main.c because main.c also drives the
 * countdown that decides when the device goes back to sleep:
 * UNLOCK pauses it, LOCK re-arms it, the countdown only runs while locked
 * in normal mode (config mode never sleeps). */
bool app_is_unlocked(void);
void app_set_unlocked(bool unlocked);

/* Recompute the advertised status byte (adv_data[6]) from the lock and
 * config state and push it on air while advertising. Implemented in
 * main.c. */
void app_update_status(void);

/* ------------------------------------------------------------------ */
/* OS-owned status bits (advertised, settable over the console or latched
 * from OS? polls). Power/login/net are RAM-only; battery is persisted in
 * NVS (findmy_keys) and also drives the skip-slot mechanism. */
/* ------------------------------------------------------------------ */

/* The latched OS state (RAM). */
bool app_os_power(void);
bool app_os_login(void);
bool app_os_net(void);
bool app_os_poll_mode(void);
int  app_os_battery(void);           /* fm_low_battery_on(), for the getter */

/* Direct set (consoles/push): update RAM bits and persist the battery flag
 * to NVS, then push the recomputed status byte. */
void app_os_set(int low_batt, int power, int login, int net);

/* Select the OS status source: poll=1 latches bits from OS? replies, 0 =
 * direct set only (no firmware-initiated OS? polls). */
void app_os_set_poll_mode(bool poll);

/* One OS? round-trip: send the query, wait (short timeout) for the daemon's
 * reply and latch the OS bits. 0 = reply latched, -1 = no reply (OS bits
 * power/login/net go to 0). log side-effect only. The reply latency is
 * covered by the caller's key derivation when present. */
int app_os_poll(void);

/* Split variants so the slot-boundary key derivation can overlap the poll
 * round-trip: request() sends the query, read() consumes the reply. */
void app_os_poll_request(void);
int  app_os_poll_read(void);

/* Line reader with an optional timeout (ms). Returns the line length
 * (>0) on a complete line, 0 on timeout / empty input, and drops lines too
 * long to fit (never executed partially), like the blocking read_line(). */
int read_line_timeout(char *buf, size_t n, uint32_t timeout_ms);
