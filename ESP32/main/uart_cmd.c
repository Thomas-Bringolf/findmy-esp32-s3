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
#include "config.h"
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

/* OS-owned advertised status bits (RAM only): powered-on, a user logged in,
 * internet reachable, plus whether the bits are latched from OS? polls
 * (poll mode) or set directly. Battery is persisted separately in
 * findmy_keys (fm_low_battery_on) and drives the skip-slot mechanism. */
static bool s_os_power = false;
static bool s_os_login = false;
static bool s_os_net = false;
static bool s_os_poll = true;   /* default: a daemon feeds the status */

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

/* Line reader on stdin; tolerates \r\n and EOF-when-empty. Lines that do
 * not fit are dropped whole (never executed partially). `timeout_ms` bounds
 * the wait for a complete line (UINT32_MAX = block forever); returns 0 on
 * timeout with no complete line. The 20 ms EOF poll is kept so the task
 * sleeps between bytes instead of busy-spinning on the VFS UART. */
int read_line_timeout(char *buf, size_t n, uint32_t timeout_ms)
{
    size_t len = 0;
    bool overflow = false;
    const TickType_t start = xTaskGetTickCount();
    const TickType_t ticks = (timeout_ms == UINT32_MAX)
                           ? portMAX_DELAY
                           : pdMS_TO_TICKS(timeout_ms);

    for (;;) {
        int c = fgetc(stdin);
        if (c == EOF) {
            if (timeout_ms != UINT32_MAX &&
                (int32_t)(xTaskGetTickCount() - (start + ticks)) >= 0) {
                return 0;   /* no complete line within the budget */
            }
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
    reply("STATUS paired=%d slot=%u debug=%d adv_ms=%u rot_sec=%u dbg_sec=%u "
          "lomode=%d loslots=%u",
          fm_is_paired() ? 1 : 0,
          fm_is_paired() ? (unsigned)fm_current_slot() : 0u,
          fm_debug_enabled() ? 1 : 0,
          (unsigned)fm_get_adv_ms(),
          (unsigned)fm_get_rot_sec(),
          (unsigned)fm_get_dbg_sec(),
          fm_low_battery_on() ? 1 : 0,
          (unsigned)fm_skip_slots());
}

/* ------------------------------------------------------------------ */
/* OS status bits: accessors, direct set, OS? poll + latched getter.  */
/* ------------------------------------------------------------------ */

bool app_os_power(void)      { return s_os_power; }
bool app_os_login(void)      { return s_os_login; }
bool app_os_net(void)        { return s_os_net; }
bool app_os_poll_mode(void)  { return s_os_poll; }
int  app_os_battery(void)    { return fm_low_battery_on() ? 1 : 0; }

/* Direct set (console/push path): RAM bits are set outright; the battery
 * flag is persisted to NVS (it drives the skip-slot mechanism) and the
 * advertised status byte is recomputed + pushed. */
void app_os_set(int low_batt, int power, int login, int net)
{
    bool low = low_batt != 0;
    if (fm_set_os_battery(low) != 0) {
        ESP_LOGE(TAG, "OSSTATE: failed to persist battery flag");
    }
    s_os_power = power != 0;
    s_os_login = login != 0;
    s_os_net = net != 0;
    app_update_status();
    ESP_LOGI(TAG, "OS status set directly: batt=%d power=%d login=%d net=%d",
             low ? 1 : 0, power ? 1 : 0, login ? 1 : 0, net ? 1 : 0);
}

void app_os_set_poll_mode(bool poll)
{
    s_os_poll = poll;
    ESP_LOGI(TAG, "OS status source: %s",
             poll ? "poll (OS? replies)" : "direct (OSSTATE)");
}

/* Parse an `OK OS batt=.. power=.. user=.. net=..` reply into the latch. */
static int os_parse_reply(const char *line, int *batt, int *power,
                          int *login, int *net)
{
    const char *p = line;

    while (*p) {
        const char *key;
        int *dst = NULL;
        if (strncmp(p, "batt=", 5) == 0)       { key = p + 5; dst = batt; }
        else if (strncmp(p, "power=", 6) == 0)  { key = p + 6; dst = power; }
        else if (strncmp(p, "user=", 5) == 0)   { key = p + 5; dst = login; }
        else if (strncmp(p, "net=", 4) == 0)    { key = p + 4; dst = net; }
        else { p++; continue; }
        *dst = (*key == '1') ? 1 : 0;
        p = key;
    }
    return (*batt >= 0 && *power >= 0 && *login >= 0 && *net >= 0) ? 0 : -1;
}

/* Send one OS? query to the (real or test) daemon. Logged so the poll
 * cadence is observable on the console. */
void app_os_poll_request(void)
{
    printf("OS?\n");
    fflush(stdout);
    ESP_LOGI(TAG, "OS? poll sent");
}

/* Wait for the daemon's `OK OS ...` reply and latch the OS bits. With no
 * reply (daemon absent / OS off), power/login/net go to 0 while battery is
 * left as persisted (a missing reply must not spuriously re-arm cycle-skip,
 * nor clear a low-battery flag the OS already reported). The reply is logged
 * either way. */
int app_os_poll_read(void)
{
    char line[CMD_LINE_MAX];
    int batt = -1, pwr = -1, usr = -1, ntw = -1;

    int len = read_line_timeout(line, sizeof(line), FM_POLL_REPLY_MS);
    if (len <= 0) {
        s_os_power = false;
        s_os_login = false;
        s_os_net = false;
        ESP_LOGI(TAG, "OS? poll: no reply, OS bits cleared");
        return -1;
    }
    if (strncmp(line, "OK OS", 5) == 0 &&
        os_parse_reply(line, &batt, &pwr, &usr, &ntw) == 0) {
        if (batt >= 0 && fm_set_os_battery(batt != 0) != 0) {
            ESP_LOGE(TAG, "OS? poll: failed to persist battery flag");
        }
        if (pwr >= 0) s_os_power = pwr != 0;
        if (usr >= 0) s_os_login = usr != 0;
        if (ntw >= 0) s_os_net = ntw != 0;
        app_update_status();
        ESP_LOGI(TAG, "OS? poll: batt=%d power=%d login=%d net=%d",
                 batt, pwr, usr, ntw);
        return 0;
    }
    /* Unexpected reply line: do not trust it. Log and treat as no reply. */
    ESP_LOGW(TAG, "OS? poll: unexpected reply '%s'", line);
    return -1;
}

int app_os_poll(void)
{
    app_os_poll_request();
    return app_os_poll_read();
}

/* OS? getter (console side). Reports the currently latched OS state, the
 * comma-free way that reflects the advertised byte without the parity bit. */
static void handle_os(void)
{
    reply("OS batt=%d power=%d user=%d net=%d",
          fm_low_battery_on() ? 1 : 0,
          s_os_power ? 1 : 0,
          s_os_login ? 1 : 0,
          s_os_net ? 1 : 0);
}

static void handle_osstate(char *b, char *p, char *u, char *n)
{
    uint32_t batt, power, login, net;

    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    if (!parse_u32(b, &batt) || batt > 1 ||
        !parse_u32(p, &power) || power > 1 ||
        !parse_u32(u, &login) || login > 1 ||
        !parse_u32(n, &net) || net > 1) {
        reply("ERR ARGS");
        return;
    }
    app_os_set((int)batt, (int)power, (int)login, (int)net);
    reply("OK OSSTATE batt=%u power=%u user=%u net=%u",
          batt, power, login, net);
}

static void handle_osmode(char *arg)
{
    if (strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    if (arg == NULL) {
        reply("OSMODE %s", s_os_poll ? "poll" : "direct");
        return;
    }
    if (strcmp(arg, "poll") == 0) {
        app_os_set_poll_mode(true);
    } else if (strcmp(arg, "direct") == 0) {
        app_os_set_poll_mode(false);
    } else {
        reply("ERR ARGS");
        return;
    }
    reply("OK OSMODE %s", s_os_poll ? "poll" : "direct");
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

/* LOWBATT on|off [slots]: toggle low-battery mode. `on` without a count
 * defaults to FM_SKIP_SLOTS_DEFAULT (1 = skip every other slot). The mode
 * only affects steady state, so it takes effect at the end of the current
 * slot. Mirrored on the advertising status byte (FM_STATUS_LOWBATT). */
static void handle_lowbatt(char *state_s, char *slots_s)
{
    bool on;

    if (state_s == NULL || strtok(NULL, " ") != NULL) {
        reply("ERR ARGS");
        return;
    }
    if (!fm_is_paired()) {
        reply("ERR UNPAIRED");
        return;
    }
    if (strcmp(state_s, "on") == 0 || strcmp(state_s, "1") == 0) {
        on = true;
    } else if (strcmp(state_s, "off") == 0 || strcmp(state_s, "0") == 0) {
        on = false;
    } else {
        reply("ERR ARGS");
        return;
    }
    uint32_t slots = on ? FM_SKIP_SLOTS_DEFAULT : 1;
    if (slots_s != NULL && !parse_u32(slots_s, &slots)) {
        reply("ERR ARGS");
        return;
    }
    if (fm_set_low_battery(on, slots) != 0) {
        reply("ERR NVS");
        return;
    }
    app_update_status();
    ESP_LOGI(TAG, "low-battery mode %s (%u slot(s) skipped per active slot)",
             on ? "on" : "off",
             (unsigned)(on ? fm_skip_slots() : 0u));
    reply("OK LOWBATT %s loslots=%u", on ? "on" : "off",
          on ? (unsigned)fm_skip_slots() : 0u);
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
    /* OS? (getter) is answered even while locked, like IDENT?: it reports
     * the latched OS state and carries no secret or side effect. */
    if (strcmp(cmd, "OS?") == 0) {
        if (strtok(NULL, " ") != NULL) {
            reply("ERR ARGS");
            return;
        }
        handle_os();
        return;
    }
    if (!app_is_unlocked()) {
        reply("LOCKED");
        return;
    }

    if (strcmp(cmd, "PING") == 0) {
        reply("PONG fw=4 paired=%d", fm_is_paired() ? 1 : 0);
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
    } else if (strcmp(cmd, "LOWBATT") == 0) {
        char *a = strtok(NULL, " ");
        char *b = strtok(NULL, " ");
        handle_lowbatt(a, b);
    } else if (strcmp(cmd, "OSSTATE") == 0) {
        char *a = strtok(NULL, " ");
        char *b = strtok(NULL, " ");
        char *c = strtok(NULL, " ");
        char *d = strtok(NULL, " ");
        handle_osstate(a, b, c, d);
    } else if (strcmp(cmd, "OSMODE") == 0) {
        handle_osmode(strtok(NULL, " "));
    } else {
        reply("ERR CMD");
    }
}

static void uart_cmd_task(void *arg)
{
    TickType_t last_poll = xTaskGetTickCount();

    for (;;) {
        /* Read a command line. When the read times out (no host input for
         * FM_DEBUG_READ_MS) the console is idle, and only then do we run the
         * debug-session OS? poll - never in the same iteration a command
         * arrived, so the poll can't read and discard a pending command. */
        int len = read_line_timeout(cmd_line, sizeof(cmd_line),
                                    FM_DEBUG_READ_MS);
        if (len > 0) {
            handle_line(cmd_line);
            continue;
        }
        if (app_os_poll_mode() && fm_is_paired() &&
            (int32_t)(xTaskGetTickCount() - last_poll) >=
                (int32_t)pdMS_TO_TICKS(FM_DEBUG_POLL_MS)) {
            last_poll = xTaskGetTickCount();
            app_os_poll();
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
