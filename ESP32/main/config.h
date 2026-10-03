#pragma once

#include <stdint.h>

/* How long each wake cycle transmits before the controller is stopped again
 * (host-side measurement showed ~5 ms is enough for one on-air event). */
#ifndef FM_ADV_GUARD_MS
#define FM_ADV_GUARD_MS 5
#endif

/* Floor for every sleep request so a cycle that overruns its budget can
 * never busy-spin the CPU. */
#define FM_MIN_SLEEP_US 20000ULL

/* How often the OS? poll fires while the console session runs (the device
 * is awake there anyway, so this is only for test/liveness). In the
 * steady-state loop the poll runs once per active-slot boundary instead,
 * overlapped with the key derivation to add no awake time. */
#define FM_DEBUG_POLL_MS 5000u
/* Console idle listening granularity: how long the UART reader waits for a
 * complete command line before returning to service the debug-session poll
 * timer (and re-listen). A real interactive line is well under this. */
#define FM_DEBUG_READ_MS 1000u
/* How long the firmware waits for a daemon's OS? reply after sending it. */
#define FM_POLL_REPLY_MS 50u

/* Budget for light-sleep entry + wake latency, subtracted from the sleep
 * request so the wake-to-wake period matches the advertising period. */
#define FM_SLEEP_LATENCY_US 1000ULL

#define FM_RTC_MAGIC 0x4D485931u

#define ADV_DATA_LEN 31

/* Status byte (adv_data[6]) bitfield, readable by any BLE scanner.
 * Bits 0-1 are the ESP32's own state; bits 2-5 are OS-reported state
 * (set directly over the console, or latched from OS? polls); bit 6 is
 * even parity over the whole byte; bit 7 is spare (always 0). */
#define FM_STATUS_UNLOCKED (1u << 0)   /* 1 = UART console unlocked (ESP32) */
#define FM_STATUS_CONFIG   (1u << 1)   /* 1 = config mode (ESP32, never sleeps) */
#define FM_STATUS_LOWBATT  (1u << 2)   /* 1 = OS reports battery < 20% (skips slots) */
#define FM_STATUS_POWER    (1u << 3)   /* 1 = OS powered on */
#define FM_STATUS_LOGIN    (1u << 4)   /* 1 = OS has a user logged in */
#define FM_STATUS_NET      (1u << 5)   /* 1 = OS has internet access */
#define FM_STATUS_PARITY   (1u << 6)   /* even parity over all 8 bits */
#define FM_STATUS_SPARE    (1u << 7)   /* unused, always 0 */

/* The OS-owned bits the latched/poll state can drive. */
#define FM_STATUS_OS_MASK (FM_STATUS_LOWBATT | FM_STATUS_POWER | \
                           FM_STATUS_LOGIN | FM_STATUS_NET)

/* Return `b` with bit 6 set so that the popcount of the whole byte is even
 * (total number of 1-bits, including the parity bit itself, is even). */
static inline uint8_t fm_status_with_parity(uint8_t b)
{
    uint8_t pop = __builtin_popcount((unsigned)(b & ~FM_STATUS_PARITY));
    return (uint8_t)((b & ~FM_STATUS_PARITY) |
                     ((pop & 1u) ? FM_STATUS_PARITY : 0u));
}

/* Low-battery skip-slot bounds live in findmy_keys.h, next to the rest of
 * the runtime config limits (FM_SKIP_SLOTS_MAX, FM_SKIP_SLOTS_DEFAULT). */

typedef struct {
    uint32_t magic;
    uint8_t  sleep_bit;
    uint8_t  paired;
    uint8_t  lowbat_skip;   /* 1 = wake must batch-advance the slot chain */
    uint32_t i;
    uint8_t  adv[ADV_DATA_LEN];
    uint8_t  addr[6];
} fm_rtc_state_t;
