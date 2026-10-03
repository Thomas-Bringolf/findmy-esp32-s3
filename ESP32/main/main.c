#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_attr.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "findmy_keys.h"
#include "uart_cmd.h"
#include "config.h"
#include "beacon.h"

static const char *LOG_TAG = "open_haystack";

RTC_DATA_ATTR fm_rtc_state_t rtc_state;

static void enter_light_sleep(uint64_t sleep_us)
{
    if (sleep_us < FM_MIN_SLEEP_US) {
        sleep_us = FM_MIN_SLEEP_US;
    }
    esp_sleep_enable_timer_wakeup(sleep_us);
    esp_light_sleep_start();
}

static void enter_deep_sleep(uint64_t sleep_us)
{
    if (sleep_us < FM_MIN_SLEEP_US) {
        sleep_us = FM_MIN_SLEEP_US;
    }
    rtc_state.magic = FM_RTC_MAGIC;
    rtc_state.sleep_bit = 1;
    esp_sleep_enable_timer_wakeup(sleep_us);
    esp_deep_sleep_start();
}

static void enter_sleep_cycle(void)
{
    if (fm_is_paired()) {
        /* Publish the frame that outlives the debug window: status byte
         * 0 (locked + sleep-cycle mode), so the snapshot must be taken
         * after clearing it. Config-mode devices never get here. */
        adv_data[6] = 0x00;
        ble_adv_publish_frame();
        rtc_state.i = 0;
    } else {
        rtc_state.paired = 0;
        ESP_LOGW(LOG_TAG, "unpaired: sleeping without advertising "
                          "(power-cycle to get the pairing console back)");
    }

    ble_adv_shutdown();
    enter_deep_sleep((uint64_t)fm_get_adv_ms() * 1000ULL);
}

/* ------------------------------------------------------------------ */
/* Console lock state. Owned here because the countdown that decides
 * when the device sleeps is driven from the session loop below:
 *   - boots LOCKED (volatile, any wake starts locked),
 *   - UNLOCK pauses the countdown, LOCK re-arms it for dbg_sec,
 *   - config mode (unpaired or still on the factory PIN) never sleeps. */
/* ------------------------------------------------------------------ */

static volatile bool s_unlocked = false;
static volatile bool s_countdown = false;   /* locked + normal mode */
static volatile int64_t s_deadline_us = 0;

static bool config_mode(void)
{
    return !fm_is_paired() ||
           strcmp(fm_get_pin(), FM_PIN_DEFAULT) == 0;
}

bool app_is_unlocked(void)
{
    return s_unlocked;
}

/* Compose the 8-bit advertised status byte from the ESP32's own state
 * (unlocked/config), the OS-reported state (battery via findmy_keys,
 * power/login/net via uart_cmd) and whole-byte even parity (bit 6). */
static uint8_t status_compute(void)
{
    uint8_t status = 0;

    if (s_unlocked) {
        status |= FM_STATUS_UNLOCKED;
    }
    if (config_mode()) {
        status |= FM_STATUS_CONFIG;
    }
    if (fm_low_battery_on()) {
        status |= FM_STATUS_LOWBATT;
    }
    if (app_os_power()) {
        status |= FM_STATUS_POWER;
    }
    if (app_os_login()) {
        status |= FM_STATUS_LOGIN;
    }
    if (app_os_net()) {
        status |= FM_STATUS_NET;
    }
    return fm_status_with_parity(status);
}

/* Recompute the advertised status byte and push it while advertising. */
void app_update_status(void)
{
    uint8_t status = status_compute();

    if (adv_data[6] == status) {
        return;
    }
    adv_data[6] = status;
    ble_adv_set_status();
}

void app_set_unlocked(bool unlocked)
{
    s_unlocked = unlocked;
    if (unlocked) {
        s_countdown = false;                       /* console stays open */
    } else if (config_mode()) {
        s_countdown = false;                       /* config mode never sleeps */
    } else {
        const uint32_t dbg = fm_get_dbg_sec();
        /* dbg_sec=0 means "no debug window": the device goes right back
         * to sleep, exactly like the old blind delay did. */
        s_deadline_us = (dbg > 0)
            ? esp_timer_get_time() + (int64_t)dbg * 1000000LL
            : 0;
        s_countdown = true;
    }
    app_update_status();
    ESP_LOGI(LOG_TAG, "console %s (%s mode, dbg_sec=%u)",
             unlocked ? "unlocked" : "locked",
             config_mode() ? "config" : "normal",
             (unsigned)fm_get_dbg_sec());
}

static uint32_t cycles_per_slot(void)
{
    const uint32_t adv = fm_get_adv_ms();
    if (adv == 0) return 1;
    uint32_t c = (fm_get_rot_sec() * 1000u) / adv;
    return c ? c : 1;
}

/* Slot-boundary work: poll the OS state (poll mode only) and advance the
 * key chain. In normal mode the OS? query is sent before the ~2.2 s key
 * derivation so the daemon's reply is already buffered by the time it is
 * read - the poll round-trip costs no extra awake time. In low-battery skip
 * mode there is no derivation to overlap (the new slot's key is computed on
 * wake), so we send OS?, read the reply and wait out the short round-trip,
 * then deep-sleep the skipped slots (this function never returns then). */
static void slot_end(void)
{
    const bool lowbat_skip = fm_low_battery_on() && fm_skip_slots() > 0;

    if (app_os_poll_mode()) {
        app_os_poll_request();
    }

    if (lowbat_skip) {
        if (app_os_poll_mode()) {
            (void)app_os_poll_read();
        }
        adv_data[6] = status_compute();
        rtc_state.i = 0;
        rtc_state.lowbat_skip = 1;
        ble_adv_shutdown();
        enter_deep_sleep((uint64_t)fm_skip_slots() *
                         (uint64_t)fm_get_rot_sec() * 1000000ULL);
        /* never returns */
    }

    if (fm_key_init() == 0 && fm_advance_slot() == 0) {
        ble_adv_apply_current_key();
        if (app_os_poll_mode()) {
            (void)app_os_poll_read();
        }
        adv_data[6] = status_compute();
        rtc_state.i = 0;
    }
}

/* Steady-state loop entered after the post-boot debug window: publish one
 * advertising event per adv_ms and light-sleep the rest of the cycle. In
 * low-battery mode, one slot of this alternates with a deep sleep covering
 * fm_skip_slots() more slots. Returns only if the device turns out to be
 * unpaired (the caller then falls back to the pairing console instead of
 * light-sleeping forever). */
static void run_light_sleep_cycle(void)
{
    (void)nvs_flash_init();
    if (fm_key_init() != 0) {
        ESP_LOGE(LOG_TAG, "key store init failed");
        return;
    }

    /* fm_key_init() must run first: it loads adv_ms/rot_sec from NVS.
     * With debug off only the PWR telemetry lines of this tag are printed. */
    if (!fm_debug_enabled()) {
        esp_log_level_set("*", ESP_LOG_NONE);
        esp_log_level_set(LOG_TAG, ESP_LOG_INFO);
    }

    if (!fm_is_paired()) {
        ESP_LOGW(LOG_TAG, "woke up unpaired: returning to the pairing console");
        return;
    }

    const uint64_t cycle_us = (uint64_t)fm_get_adv_ms() * 1000ULL;

    if (rtc_state.lowbat_skip) {
        /* Woke from a low-battery deep sleep: jump the one-way SK chain
         * forward by the skipped slots and derive only the current slot's
         * public key. The skipped slots never derive a key. */
        rtc_state.lowbat_skip = 0;
        rtc_state.i = 0;
        if (fm_advance_slots(fm_skip_slots()) != 0) {
            ESP_LOGE(LOG_TAG, "low-battery catch-up failed");
        }
        ble_adv_apply_current_key();
    } else if (rtc_state.magic == FM_RTC_MAGIC && rtc_state.paired) {
        ble_adv_restore_frame();
    } else {
        /* RTC memory lost (brown-out/battery swap): rebuild from NVS. */
        ble_adv_apply_current_key();
    }
    /* Preserve the OS-reported bits carried over from the debug session /
     * previous wake instead of zeroing them; the steady-state loop keeps
     * them fresh via the slot-end poll. */
    adv_data[6] = status_compute();

    ble_adv_host_init();

    while (1) {
        const int64_t t_wake = esp_timer_get_time();

        ESP_LOGI(LOG_TAG, "PWR wake t=%lld", (long long)t_wake);

        adv_data[6] = status_compute();
        (void)ble_adv_publish_once();

        rtc_state.i++;
        if (rtc_state.i >= cycles_per_slot()) {
            slot_end();
        }

        /* Time spent awake since this cycle's wake-up (light-sleep time is
         * not part of the delta, so the measurement is valid whether or not
         * esp_timer keeps counting across sleep). */
        const int64_t awake_us = esp_timer_get_time() - t_wake;
        uint64_t sleep_us;

        if ((uint64_t)awake_us + FM_SLEEP_LATENCY_US < cycle_us) {
            sleep_us = cycle_us - (uint64_t)awake_us - FM_SLEEP_LATENCY_US;
        } else {
            sleep_us = FM_MIN_SLEEP_US;
        }

        ESP_LOGI(LOG_TAG, "PWR sleep awake_us=%lld sleep_us=%llu",
                 (long long)awake_us, (unsigned long long)sleep_us);
        enter_light_sleep(sleep_us);
    }
}

/* Session loop: stays here until the countdown expires (enter_sleep_cycle()
 * never returns). UNLOCK clears s_countdown, LOCK re-arms it, so a device
 * left unlocked stays awake until someone locks it or power-cycles it. */
static void run_session(void)
{
    for (;;) {
        if (s_countdown && esp_timer_get_time() >= s_deadline_us) {
            enter_sleep_cycle();
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

static int run_debug_session(void)
{
    rtc_state.magic = FM_RTC_MAGIC;
    rtc_state.sleep_bit = 0;
    rtc_state.i = 0;
    rtc_state.paired = 0;

    /* A light-sleep cycle may have silenced the console (debug disabled);
     * the debug window always logs at the default level again. */
    esp_log_level_set("*", ESP_LOG_INFO);

    ESP_ERROR_CHECK(nvs_flash_init());

    if (fm_key_init() != 0) {
        ESP_LOGE(LOG_TAG, "key store init failed, stopping.");
        enter_sleep_cycle();
        return -1;
    }

    /* Apply the key/address BEFORE starting the NimBLE host, so that on_sync()
     * (which installs the random static address) sees a valid address. */
    if (fm_is_paired()) {
        ble_adv_apply_current_key();
    } else {
        ESP_LOGI(LOG_TAG, "unpaired: use findmy-toolbox.py to provision keys "
                          "over UART (config mode: console stays open)");
    }

    /* Boots locked: arms the countdown (or not, in config mode) and sets
     * the status byte before the first advertisement goes out. */
    app_set_unlocked(false);

    ble_adv_host_init();

    ble_adv_start_chain();

    /* The console LAST: KEYS/CONFIG restart advertising, and every host API
     * they reach (ble_hs_synced(), ble_gap_adv_*) needs the NimBLE host to be
     * initialised. Starting the UART task before ble_adv_host_init() left a
     * window (the P-224 derivation above takes ~2.2 s) in which a fast host
     * could crash the device with a NULL deref in ble_hs_synced(). */
    uart_cmd_start();

    ESP_LOGI(LOG_TAG, "Find My beacon up (paired=%d, debug=%d, slot=%us, %u wake cycles/slot)",
             fm_is_paired() ? 1 : 0, fm_debug_enabled() ? 1 : 0, fm_current_slot(),
             (unsigned)cycles_per_slot());
    ESP_LOGI(LOG_TAG, "adv %u ms, rotation %u s, dbg window %u s, pin %s",
             (unsigned)fm_get_adv_ms(),
             (unsigned)fm_get_rot_sec(),
             (unsigned)fm_get_dbg_sec(),
             config_mode() ? "default (config mode)" : "set");

    run_session();
    return 0;
}

void app_main(void)
{
    const bool woke_from_deep_sleep =
        rtc_state.magic == FM_RTC_MAGIC &&
        rtc_state.sleep_bit == 1 &&
        (esp_sleep_get_wakeup_causes() & BIT(ESP_SLEEP_WAKEUP_TIMER)) != 0;

    if (woke_from_deep_sleep) {
        run_light_sleep_cycle();   /* paired: never returns */
    }

    run_debug_session();
}
