#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "config.h"

/* Shared beacon frame (ble_adv.c) and RTC-retained state (main.c). */
extern uint8_t adv_data[ADV_DATA_LEN];
extern fm_rtc_state_t rtc_state;

/* ble_adv.c */
void ble_adv_host_init(void);
void ble_adv_start_chain(void);
bool ble_adv_publish_once(void);
void ble_adv_shutdown(void);
void ble_adv_apply_current_key(void);
void ble_adv_restore_frame(void);
void ble_adv_publish_frame(void);

/* Push a changed status byte (adv_data[6]) on air without touching keys,
 * the device address, the interval or the rotation timer. No-op while not
 * advertising - the next ble_adv_start_chain() picks the byte up. */
void ble_adv_set_status(void);
