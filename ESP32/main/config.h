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

/* Budget for light-sleep entry + wake latency, subtracted from the sleep
 * request so the wake-to-wake period matches the advertising period. */
#define FM_SLEEP_LATENCY_US 1000ULL

#define FM_RTC_MAGIC 0x4D485931u

#define ADV_DATA_LEN 31

/* Status byte (adv_data[6]) bitfield, readable by any BLE scanner. */
#define FM_STATUS_UNLOCKED (1u << 0)   /* 1 = UART console unlocked */
#define FM_STATUS_CONFIG   (1u << 1)   /* 1 = config mode (never sleeps) */

typedef struct {
    uint32_t magic;
    uint8_t  sleep_bit;
    uint8_t  paired;
    uint32_t i;
    uint8_t  adv[ADV_DATA_LEN];
    uint8_t  addr[6];
} fm_rtc_state_t;
