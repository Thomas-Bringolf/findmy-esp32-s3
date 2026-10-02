#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define FM_MASTER_LEN 28
#define FM_SK_LEN 32

/* Runtime advisory parameters (persisted in NVS, set at pairing time). */
#define FM_ADV_MS_DEFAULT 2000
#define FM_ADV_MS_MIN      200  /* 0.2 s minimum advertisement period */
#define FM_ADV_MS_MAX   60000   /* 60 s maximum */
#define FM_ROT_SEC_MIN      1   /* key rotation period bounds; the upper */
#define FM_ROT_SEC_MAX   86400  /* limit keeps cycles_per_slot() free of overflow */
#define FM_DBG_SEC_DEFAULT 600
#define FM_DBG_SEC_MIN      60
#define FM_DBG_SEC_MAX    3600

#ifndef FM_SLOT_SECONDS
#define FM_SLOT_SECONDS 120
#endif

/* Runtime console PIN: FM_PIN_LEN digits, persisted in NVS. The factory
 * default doubles as the config-mode marker (a device still holding it
 * keeps its console open and never sleeps). */
#define FM_PIN_LEN 8
#define FM_PIN_DEFAULT "00000000"

/* UNLOCK anti-bruteforce: FM_PIN_FAIL_MAX wrong PINs in a row arm a
 * lockout of FM_PIN_LOCKOUT_BASE_S seconds that doubles with every further
 * failure, capped at FM_PIN_LOCKOUT_MAX_S. The failure counter lives in
 * NVS, so a power cycle cannot clear it. */
#define FM_PIN_FAIL_MAX 5
#define FM_PIN_LOCKOUT_BASE_S 30
#define FM_PIN_LOCKOUT_MAX_S 900

/* Load P-224 params and restore chain state (keys, slot, debug) from NVS. */
int fm_key_init(void);

/* True when the device holds key material (i.e. was paired over UART). */
bool fm_is_paired(void);

/* Current slot index (0 == pairing time). */
uint32_t fm_current_slot(void);

/* Derive the 28-byte public key X for the current slot. 0 on success. */
int fm_current_pubkey(uint8_t x_out[28]);

/* Advance the SK chain by one slot and persist it. 0 on success. */
int fm_advance_slot(void);

/* Store new key material (pairing), reset the chain to slot 0, persist. */
int fm_pair(const uint8_t master[FM_MASTER_LEN],
            const uint8_t skn[FM_SK_LEN]);

/* Erase all stored key material + config (factory reset to unpaired).
 * Also wipes the console PIN and the UNLOCK failure counter back to the
 * factory defaults. */
int fm_unpair(void);

/* Console PIN: never NULL, FM_PIN_DEFAULT until the first PIN command.
 * fm_set_pin() takes FM_PIN_LEN digits only, 0 on success. */
const char *fm_get_pin(void);
int fm_set_pin(const char *pin);

/* Wrong-UNLOCK attempt counter, persisted in NVS. */
uint32_t fm_pin_fails(void);
int fm_pin_fail_add(void);      /* ++ and persist, 0 on success */
int fm_pin_fail_reset(void);    /* back to 0 and persist, 0 on success */

/* Debug logging flag (persisted in NVS). */
bool fm_debug_enabled(void);
int fm_set_debug(bool enable);

/* Advisory timing parameters. adv_ms is the advertisement period (200..60000),
 * rot_sec the key rotation period in seconds, dbg_sec the post-boot debug
 * window (0 = disabled, 60..3600). Invalid input clamps to defaults. */
uint32_t fm_get_adv_ms(void);
uint32_t fm_get_rot_sec(void);
uint32_t fm_get_dbg_sec(void);
int fm_set_config(uint32_t adv_ms, uint32_t rot_sec, uint32_t dbg_sec);

/* Minimal base64 decoder. Returns decoded length or -1 on error. */
int fm_base64_decode(const char *src, uint8_t *dst, size_t dst_size);
