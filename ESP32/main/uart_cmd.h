#pragma once

#include <stdbool.h>

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
