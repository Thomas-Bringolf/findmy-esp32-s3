#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "findmy_keys.h"
#include "uart_cmd.h"

#define CMD_LINE_MAX 256

static const char *TAG = "uart_cmd";

static char cmd_line[CMD_LINE_MAX];

/* UNLOCK lockout deadline (RTC-retained). The magic only survives within
 * the boot that armed it: uart_cmd_start() clears it, so a deep-sleep wake
 * or power cycle re-arms the window from the persisted failure counter
 * instead of inheriting a deadline measured in a previous esp_timer epoch
 * (where "remaining" could be days). */
static RTC_DATA_ATTR uint32_t lock_magic;
static RTC_DATA_ATTR uint64_t lock_deadline_us;

#define LOCK_MAGIC 0x4C4F4331u

/* Lockout length after `fails` consecutive wrong PINs: FM_PIN_FAIL_MAX
 * failures arm FM_PIN_LOCKOUT_BASE_S, doubling per further failure up to
 * FM_PIN_LOCKOUT_MAX_S. */
static uint32_t lockout_s(uint32_t fails)
{
    uint32_t s = FM_PIN_LOCKOUT_BASE_S;

    for (uint32_t i = FM_PIN_FAIL_MAX; i < fails && s < FM_PIN_LOCKOUT_MAX_S; i++) {
        s *= 2;
    }
    return s > FM_PIN_LOCKOUT_MAX_S ? FM_PIN_LOCKOUT_MAX_S : s;
}

/* Microseconds left in the active lockout, 0 when there is none. */
static int64_t lockout_remaining_us(void)
{
    if (lock_magic != LOCK_MAGIC) {
        return 0;
    }
    int64_t left = (int64_t)lock_deadline_us - esp_timer_get_time();
    return left > 0 ? left : 0;
}

static void arm_lockout(uint32_t fails)
{
    lock_deadline_us = (uint64_t)esp_timer_get_time() +
                       (uint64_t)lockout_s(fails) * 1000000ULL;
    lock_magic = LOCK_MAGIC;
}

static uint32_t lockout_remaining_s(void)
{
    int64_t us = lockout_remaining_us();
    return (uint32_t)((us + 999999) / 1000000);
}

/* FM_PIN_LEN decimal digits, nothing else. NULL/short/long/non-digit all
 * count as a malformed argument (ERR ARGS), never as a wrong PIN. */
static bool valid_pin(const char *pin)
{
    if (pin == NULL || strlen(pin) != FM_PIN_LEN) {
        return false;
    }
    for (int i = 0; i < FM_PIN_LEN; i++) {
        if (pin[i] < '0' || pin[i] > '9') {
            return false;
        }
    }
    return true;
}

static void reply(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    vprintf(format, args);
    va_end(args);
    printf("\n");
    fflush(stdout);
}

/* Strict uint32 parser: rejects empty strings, sign characters, trailing
 * junk and overflow so garbage on the line can never silently become a
 * config value. */
static bool parse_u32(const char *s, uint32_t *out)
{
    char *end;
    unsigned long v;

    if (s == NULL || *s < '0' || *s > '9') {
        return false;
    }
    errno = 0;
    v = strtoul(s, &end, 10);
    if (*end != '\0' || errno == ERANGE || v > UINT32_MAX) {
        return false;
    }
    *out = (uint32_t)v;
    return true;
}

/* Blocking line reader on stdin; tolerates \r\n and EOF-when-empty.
 * Lines that do not fit are dropped whole (never executed partially). */
static int read_line(char *buf, size_t n)
{
    size_t len = 0;
    bool overflow = false;

    for (;;) {
        int c = fgetc(stdin);
        if (c == EOF) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (c == '\r' || c == '\n') {
            if (overflow) {
                len = 0;
                overflow = false;
                continue;
            }
            if (len > 0) {
                buf[len] = '\0';
                return (int)len;
            }
            continue;
        }
        if (len + 1 < n) {
            buf[len++] = (char)c;
        } else {
            overflow = true;
        }
    }
}

/* UNLOCK <pin>: the only command a locked console accepts (besides LOCK
 * on an already-unlocked device). Wrong PINs are counted, and
 * FM_PIN_FAIL_MAX of them arm an exponentially growing lockout. */
static void handle_unlock(char *pin)
{
    uint32_t fails = fm_pin_fails();

    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");   /* trailing garbage is not silently ignored */
        return;
    }

    /* Re-arm the window after a reboot: the failure counter survived in
     * NVS, the deadline did not necessarily (and must not be trusted
     * across esp_timer epochs). */
    if (fails >= FM_PIN_FAIL_MAX && lock_magic != LOCK_MAGIC) {
        arm_lockout(fails);
    }
    if (lockout_remaining_us() > 0) {
        reply("ERR LOCK %u", (unsigned)lockout_remaining_s());
        ESP_LOGW(TAG, "UNLOCK refused, lockout active for %u s",
                 (unsigned)lockout_remaining_s());
        return;
    }
    if (!valid_pin(pin)) {
        reply("ERR ARGS");
        return;
    }
    if (strcmp(pin, fm_get_pin()) != 0) {
        if (fm_pin_fail_add() != 0) {
            ESP_LOGE(TAG, "failed to persist the failure counter");
        }
        fails++;
        if (fails >= FM_PIN_FAIL_MAX) {
            arm_lockout(fails);
            reply("ERR LOCK %u", (unsigned)lockout_s(fails));
            ESP_LOGW(TAG, "wrong PIN x%u: lockout for %u s",
                     (unsigned)fails, (unsigned)lockout_s(fails));
        } else {
            reply("ERR PIN");
            ESP_LOGW(TAG, "wrong PIN (attempt %u/%u)",
                     (unsigned)fails, (unsigned)FM_PIN_FAIL_MAX);
        }
        return;
    }
    if (fails > 0 && fm_pin_fail_reset() != 0) {
        ESP_LOGE(TAG, "failed to clear the failure counter");
    }
    lock_magic = 0;
    app_set_unlocked(true);
    reply("OK UNLOCK");
}

/* LOCK <pin>: closes the console again and restarts the dbg_sec countdown.
 * A wrong PIN is not counted here - the caller is already authenticated. */
static void handle_lock(char *pin)
{
    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    if (!valid_pin(pin)) {
        reply("ERR ARGS");
        return;
    }
    if (strcmp(pin, fm_get_pin()) != 0) {
        ESP_LOGW(TAG, "lock attempt with wrong PIN");
        reply("ERR PIN");
        return;
    }
    app_set_unlocked(false);
    reply("OK LOCK");
}

/* PIN <new>: rotate the console PIN. Needs key material (ERR UNPAIRED)
 * but not a configured one, so it also runs right after KEYS. */
static void handle_pin(char *pin)
{
    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    if (!fm_is_paired()) {
        reply("ERR UNPAIRED");
        return;
    }
    if (!valid_pin(pin)) {
        reply("ERR ARGS");
        return;
    }
    if (fm_set_pin(pin) != 0) {
        reply("ERR NVS");
        return;
    }
    /* Leaving the factory PIN ends config mode: clear FM_STATUS_CONFIG
     * and, for the first time, allow the countdown to run at all. */
    app_update_status();
    ESP_LOGI(TAG, "console PIN changed");
    reply("OK PIN");
}

static void handle_keys(char *mk_b64, char *skn_b64,
                        char *adv_ms_s, char *rot_sec_s, char *dbg_sec_s)
{
    uint8_t master[FM_MASTER_LEN], skn[FM_SK_LEN];
    uint32_t adv, rot, dbg;
    bool have_cfg = false;

    if (mk_b64 == NULL || skn_b64 == NULL) {
        reply("ERR ARGS");
        return;
    }
    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    /* KEYS is only reachable from an unlocked console: the old PAIR
     * + 60 s window was replaced by the lock itself. */

    /* Validate everything before touching NVS, so a bad line never leaves
     * a half-paired state behind. */
    adv = fm_get_adv_ms();
    rot = fm_get_rot_sec();
    dbg = fm_get_dbg_sec();
    if (adv_ms_s != NULL || rot_sec_s != NULL || dbg_sec_s != NULL) {
        if ((adv_ms_s != NULL && !parse_u32(adv_ms_s, &adv)) ||
            (rot_sec_s != NULL && !parse_u32(rot_sec_s, &rot)) ||
            (dbg_sec_s != NULL && !parse_u32(dbg_sec_s, &dbg))) {
            reply("ERR ARGS");
            return;
        }
        have_cfg = true;
    }

    if (fm_base64_decode(mk_b64, master, sizeof(master)) != FM_MASTER_LEN ||
        fm_base64_decode(skn_b64, skn, sizeof(skn)) != FM_SK_LEN) {
        reply("ERR B64");
        return;
    }
    if (fm_pair(master, skn) != 0) {
        reply("ERR NVS");
        return;
    }

    /* Optional timing parameters supplied at pairing time. */
    if (have_cfg && fm_set_config(adv, rot, dbg) != 0) {
        reply("ERR NVS");
        return;
    }

    ESP_LOGI(TAG, "paired: new key chain stored, slot 0");
    app_on_keys_changed();
    app_update_status();   /* paired now: config bit depends on the PIN */
    reply("OK KEYS");
}

static void handle_slot(void)
{
    if (!fm_is_paired()) {
        reply("ERR UNPAIRED");
        return;
    }
    reply("SLOT %u", (unsigned)fm_current_slot());
}

static void handle_key(void)
{
    uint8_t x[28];

    if (!fm_is_paired()) {
        reply("ERR UNPAIRED");
        return;
    }
    if (fm_current_pubkey(x) != 0) {
        reply("ERR CRYPTO");
        return;
    }
    char hex[57];
    for (int i = 0; i < 28; i++) {
        sprintf(&hex[i * 2], "%02x", x[i]);
    }
    reply("KEY %s", hex);
}

static void handle_debug(char *arg)
{
    bool on;

    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    if (arg == NULL || (strcmp(arg, "0") != 0 && strcmp(arg, "1") != 0)) {
        reply("ERR ARGS");
        return;
    }
    on = arg[0] == '1';
    if (fm_set_debug(on) != 0) {
        reply("ERR NVS");
        return;
    }
    reply("OK DEBUG %d", on ? 1 : 0);
}

static void handle_stat(void)
{
    reply("STAT paired=%d slot=%u debug=%d",
          fm_is_paired() ? 1 : 0,
          fm_is_paired() ? (unsigned)fm_current_slot() : 0u,
          fm_debug_enabled() ? 1 : 0);
}

static void handle_status(void)
{
    reply("STATUS paired=%d slot=%u debug=%d adv_ms=%u rot_sec=%u dbg_sec=%u",
          fm_is_paired() ? 1 : 0,
          fm_is_paired() ? (unsigned)fm_current_slot() : 0u,
          fm_debug_enabled() ? 1 : 0,
          (unsigned)fm_get_adv_ms(),
          (unsigned)fm_get_rot_sec(),
          (unsigned)fm_get_dbg_sec());
}

static void handle_config(char *adv_ms_s, char *rot_sec_s, char *dbg_sec_s)
{
    uint32_t adv, rot, dbg;

    if (!parse_u32(adv_ms_s, &adv) || !parse_u32(rot_sec_s, &rot) ||
        !parse_u32(dbg_sec_s, &dbg)) {
        reply("ERR ARGS");
        return;
    }
    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");   /* trailing garbage is not silently ignored */
        return;
    }
    if (fm_set_config(adv, rot, dbg) != 0) {
        reply("ERR NVS");
        return;
    }
    app_on_keys_changed();
    reply("OK CONFIG adv_ms=%u rot_sec=%u dbg_sec=%u",
          (unsigned)fm_get_adv_ms(),
          (unsigned)fm_get_rot_sec(),
          (unsigned)fm_get_dbg_sec());
}

/* Factory reset (WIPE, no arguments): drops the key chain, the console PIN
 * and the failure counter, then reboots into the config-mode console
 * (factory PIN, never sleeps) - the escape hatch when a PIN is forgotten.
 * Only reachable from an unlocked console. */
static void handle_wipe(void)
{
    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");   /* the PIN argument was dropped with PAIR */
        return;
    }
    if (fm_unpair() != 0) {
        reply("ERR NVS");
        return;
    }
    ESP_LOGW(TAG, "key store wiped (factory PIN restored), rebooting");
    reply("OK WIPE");
    vTaskDelay(pdMS_TO_TICKS(200));   /* let the console drain */
    esp_restart();
}

/* Device identity: IDENT? reports name + pairing state and is the one
 * read-only command the lock gate lets through, so a host can tell several
 * beacons apart (or find an unpaired one) without knowing a PIN. NAME sets
 * the name and needs an unlocked console. */
static void handle_ident(char *extra)
{
    const char *name;

    if (extra != NULL) {
        reply("ERR ARGS");
        return;
    }
    name = fm_get_name();
    reply("IDENT name=%s paired=%d", name[0] ? name : "-",
          fm_is_paired() ? 1 : 0);
}

static void handle_name(char *name)
{
    int rc;

    if (name == NULL || strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    rc = fm_set_name(name);
    if (rc == -1) {
        reply("ERR ARGS");     /* charset or length outside FM_NAME_LEN */
        return;
    }
    if (rc != 0) {
        reply("ERR NVS");
        return;
    }
    reply("OK NAME %s", name);
}

/* The lock gate: UNLOCK is the only command a locked console executes
 * (LOCK only answers "LOCKED"), everything else - PING included - is
 * refused with LOCKED, so a lost device can be probed for its state but
 * nothing else. IDENT? is the exception: it carries no secret and is what
 * makes a multi-device host possible. Once unlocked, every command is
 * available. */
static void handle_line(char *line)
{
    char *cmd = strtok(line, " ");

    if (cmd == NULL) {
        return;
    }
    if (strcmp(cmd, "UNLOCK") == 0) {
        handle_unlock(strtok(NULL, " "));
        return;
    }
    if (strcmp(cmd, "LOCK") == 0) {
        if (!app_is_unlocked()) {
            reply("LOCKED");
            return;
        }
        handle_lock(strtok(NULL, " "));
        return;
    }
    if (strcmp(cmd, "IDENT?") == 0) {
        handle_ident(strtok(NULL, " "));
        return;
    }
    if (!app_is_unlocked()) {
        reply("LOCKED");
        return;
    }

    if (strcmp(cmd, "PING") == 0) {
        reply("PONG fw=3 paired=%d", fm_is_paired() ? 1 : 0);
    } else if (strcmp(cmd, "WIPE") == 0) {
        handle_wipe();
    } else if (strcmp(cmd, "KEYS") == 0) {
        /* Parse the tokens one by one: several strtok() calls in one
         * expression would be unsequenced and their order unspecified. */
        char *a = strtok(NULL, " ");
        char *b = strtok(NULL, " ");
        char *c = strtok(NULL, " ");
        char *d = strtok(NULL, " ");
        char *e = strtok(NULL, " ");
        handle_keys(a, b, c, d, e);
    } else if (strcmp(cmd, "PIN") == 0) {
        handle_pin(strtok(NULL, " "));
    } else if (strcmp(cmd, "NAME") == 0) {
        handle_name(strtok(NULL, " "));
    } else if (strcmp(cmd, "SLOT?") == 0) {
        handle_slot();
    } else if (strcmp(cmd, "KEY?") == 0) {
        handle_key();
    } else if (strcmp(cmd, "DEBUG") == 0) {
        handle_debug(strtok(NULL, " "));
    } else if (strcmp(cmd, "STAT?") == 0) {
        handle_stat();
    } else if (strcmp(cmd, "STATUS?") == 0) {
        handle_status();
    } else if (strcmp(cmd, "CONFIG") == 0) {
        char *a = strtok(NULL, " ");
        char *b = strtok(NULL, " ");
        char *c = strtok(NULL, " ");
        handle_config(a, b, c);
    } else {
        reply("ERR CMD");
    }
}

static void uart_cmd_task(void *arg)
{
    for (;;) {
        int len = read_line(cmd_line, sizeof(cmd_line));
        if (len > 0) {
            handle_line(cmd_line);
        }
    }
}

void uart_cmd_start(void)
{
    /* New boot: a deadline armed in a previous run was measured against a
     * different esp_timer epoch, so drop it. The persisted failure counter
     * re-arms the window on the next UNLOCK attempt if it still says so. */
    lock_magic = 0;
    xTaskCreate(uart_cmd_task, "uart_cmd", 4096, NULL, 5, NULL);
}
