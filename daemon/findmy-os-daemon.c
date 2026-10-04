/*
 * findmy-os-daemon - answers the ESP32 Find My beacon's OS? status polls.
 *
 * The ESP32 inside the laptop advertises four OS-reported status bits in its
 * Find My beacon frame (battery<20%, powered-on, user-logged-in, internet
 * reachable) plus a whole-byte even-parity bit. It is the *client* for these
 * bits: while its console runs it sends a bare "OS?\n" on UART and expects a
 * single-line reply of the form
 *
 *     OK OS batt=<0|1> power=<0|1> user=<0|1> net=<0|1>
 *
 * This daemon is the reply side. It owns /dev/ttyACM0 in the steady state,
 * watches for "OS?" and answers with the laptop's current health, refreshes
 * that health periodically, and is written to be very hard to crash:
 *
 *   - POSIX only (no libudev / dbus / glib),
 *   - every syscall checked, never abort(),
 *   - SIGPIPE ignored, SIGTERM/INT/HUP -> clean shutdown,
 *   - the tty may be absent or drop out (ESP32 deep sleep powers down its
 *     on-chip USB-Serial-JTAG): open is retried forever with backoff,
 *   - battery/login/internet readers each fail soft and independently (a bad
 *     read can only clear that one bit, never crash or wedge the loop),
 *   - all blocking I/O runs under bounded select()/nanosleep() intervals.
 *
 * Build:    cc -O2 -Wall -Wextra -o findmy-os-daemon findmy-os-daemon.c
 * Install (systemd unit + /usr/local/bin):  make install
 */

#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <utmpx.h>

#define TTY_DEFAULT "/dev/ttyACM0"
#define BATTHRESH_DEFAULT 20      /* % below which the battery bit is set */
#define INTERVAL_DEFAULT 10       /* seconds between health refreshes */
#define READ_POLL_US 50000       /* 50 ms select window for tty reads */
#define DRAIN_US 200000          /* 200 ms host drain on (re)open */
#define MAX_LINE 256
#define PEND_CAP 4096
#define RECONNECT_MAX_S 30       /* cap the reconnect backoff (s) */

/* ------------------------------------------------------------------ */
/* Logging: to stderr (systemd/journald) and, with --log, to a file.    */
/* Levels: 1=ERR 2=WARN 3=INFO 4=DEBUG.                                */
/* ------------------------------------------------------------------ */
typedef struct {
    FILE *file;
    bool debug;
} logger_t;

static logger_t g_log = { NULL, false };

static void log_at(int lvl, const char *fmt, va_list ap)
{
    char ts[32];
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    if (strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", &tm) == 0)
        ts[0] = '\0';

    const char *tag = lvl <= 1 ? "ERR" : lvl == 2 ? "WARN"
                    : lvl == 3 ? "INFO" : "DBG";
    fprintf(stderr, "%s [%s] ", ts, tag);
    if (g_log.file) {
        /* va_list is indeterminate after a vfprintf: make a fresh copy for
         * the second sink instead of reusing a consumed one (UB, corrupted
         * arguments / crash). */
        va_list ap2;
        va_copy(ap2, ap);
        vfprintf(stderr, fmt, ap);
        fprintf(g_log.file, "%s [%s] ", ts, tag);
        vfprintf(g_log.file, fmt, ap2);
        fputc('\n', g_log.file);
        fflush(g_log.file);
        va_end(ap2);
    } else {
        vfprintf(stderr, fmt, ap);
    }
    fputc('\n', stderr);
    fflush(stderr);
}

static void log_wrn(const char *fmt, ...)  { va_list a; va_start(a, fmt); log_at(2, fmt, a); va_end(a); }
static void log_inf(const char *fmt, ...)  { va_list a; va_start(a, fmt); log_at(3, fmt, a); va_end(a); }
static void log_dbg(const char *fmt, ...)
{
    if (g_log.debug) { va_list a; va_start(a, fmt); log_at(4, fmt, a); va_end(a); }
}

/* ------------------------------------------------------------------ */

static volatile sig_atomic_t g_running = 1;

static void on_signal(int sig) { (void)sig; g_running = 0; }

static void install_signals(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;                       /* no SA_RESTART: let select() out */
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT,  &sa, NULL);
    sigaction(SIGHUP,  &sa, NULL);

    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = SIG_IGN;               /* dead tty writes must not kill us */
    sigaction(SIGPIPE, &sa, NULL);
}

/* Strict unsigned integer parser. */
static int parse_u32(const char *s, unsigned long *out)
{
    char *end;
    unsigned long v;
    if (!s || !isdigit((unsigned char)*s)) return -1;
    errno = 0;
    v = strtoul(s, &end, 10);
    if (*end != '\0' || errno == ERANGE) return -1;
    *out = v;
    return 0;
}

/* ------------------------------------------------------------------ */
/* tty                                                                  */
/* ------------------------------------------------------------------ */
typedef struct {
    int fd;
    char path[256];
    char pending[PEND_CAP];
    size_t plen;
} tty_t;

static void tty_close(tty_t *t)
{
    if (t->fd >= 0) { close(t->fd); t->fd = -1; }
    t->plen = 0;
}

static int tty_open(tty_t *t)
{
    fd_set rfds;
    struct timeval tv;
    struct termios tc;
    char buf[256];
    int fd;

    fd = open(t->path, O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd < 0) return -1;

    if (tcgetattr(fd, &tc) == 0) {
        cfmakeraw(&tc);
        tc.c_cflag &= ~(CSIZE | PARENB);
        tc.c_cflag |= CS8;
        tc.c_cflag &= ~CRTSCTS;            /* USB-JTAG: no hardware flow */
        tc.c_cc[VMIN]  = 1;
        tc.c_cc[VTIME] = 0;
        cfsetispeed(&tc, B115200);
        cfsetospeed(&tc, B115200);
        (void)tcsetattr(fd, TCSAFLUSH, &tc);
    }
    (void)tcflush(fd, TCIFLUSH);

    /* Drain anything that arrived while we were gone so a stale OS? from a
     * previous boot is not answered with stale state. Bounded. */
    tv.tv_sec = 0; tv.tv_usec = DRAIN_US;
    FD_ZERO(&rfds); FD_SET(fd, &rfds);
    while (select(fd + 1, &rfds, NULL, NULL, &tv) > 0) {
        if (read(fd, buf, sizeof(buf)) <= 0) break;
        FD_ZERO(&rfds); FD_SET(fd, &rfds);
        tv.tv_sec = 0; tv.tv_usec = 0;
    }

    t->fd = fd;
    t->plen = 0;
    return 0;
}

/* ------------------------------------------------------------------ */
/* OS health sources (each 0/1, never crash, fail-soft to 0).            */
/* ------------------------------------------------------------------ */
static int os_power(void) { return 1; }

static int os_battery(void)
{
    /* Candidate dir/name pairs; first readable "capacity" wins. */
    static const char *const dirs[] = {
        "/sys/class/power_supply/BAT0",
        "/sys/class/power_supply/BAT1",
        "/sys/class/power_supply/BAT",
        "/sys/class/power_supply/BATTERY",
        NULL
    };
    static const char *const names[] = { "capacity", "Capacity", "CAPACITY", NULL };
    char p[256], buf[64];
    int cap = -1;

    for (int d = 0; dirs[d] && cap < 0; d++) {
        for (int v = 0; names[v]; v++) {
            snprintf(p, sizeof(p), "%s/%s", dirs[d], names[v]);
            int fd = open(p, O_RDONLY);
            if (fd < 0) continue;
            ssize_t n = read(fd, buf, sizeof(buf) - 1);
            close(fd);
            if (n <= 0) continue;
            buf[n] = '\0';
            /* trim trailing whitespace/newline that sysfs appends */
            while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r' ||
                   buf[n - 1] == ' ' || buf[n - 1] == '\t'))
                buf[--n] = '\0';
            unsigned long vv = 0;
            if (parse_u32(buf, &vv) == 0) { cap = (int)vv; break; }
        }
    }

    if (cap < 0) {
        static int logged = -1;
        if (logged != 1) { logged = 1; log_wrn("no battery capacity readable - batt stays off"); }
        return 0;
    }
    return cap < BATTHRESH_DEFAULT ? 1 : 0;
}

static int os_login(void)
{
    struct utmpx *e;
    bool any = false;

    setutxent();
    errno = 0;
    while ((e = getutxent()) != NULL) {
        if (e->ut_type == USER_PROCESS && e->ut_pid > 0 && e->ut_user[0] != '\0') {
            if (strcmp(e->ut_user, "gdm") && strcmp(e->ut_user, "greeter") &&
                strcmp(e->ut_user, "GDM") != 0) {
                any = true;
                break;
            }
        }
    }
    endutxent();
    return any ? 1 : 0;
}

static int os_net(void)
{
    FILE *f;
    char line[256];
    int has4 = 0, has6 = 0;

    f = fopen("/proc/net/route", "r");
    if (f) {
        while (fgets(line, sizeof(line), f)) {
            char iface[64];
            unsigned long dest, gw, flags;
            unsigned long d1, d2, d3, d4;
            if (sscanf(line, "%63s %8lx %8lx %4lx %lx %lx %lx %lx",
                       iface, &dest, &gw, &flags, &d1, &d2, &d3, &d4) == 8 &&
                iface[0] != 'I' && dest == 0UL && (flags & 0x2UL)) {
                has4 = 1;
                break;
            }
        }
        fclose(f);
    }

    f = fopen("/proc/net/ipv6_route", "r");
    if (f) {
        while (fgets(line, sizeof(line), f)) {
            char iface[64];
            char dest[33];
            dest[32] = '\0';
            if (sscanf(line, "%32s %63s", dest, iface) >= 1) {
                int allz = 1;
                for (int i = 0; i < 32; i++) if (dest[i] != '0') { allz = 0; break; }
                /* a default v6 route via a real interface */
                if (allz && iface[0] != '\0' && strcmp(iface, "lo") != 0) {
                    has6 = 1;
                    break;
                }
            }
        }
        fclose(f);
    }
    return (has4 || has6) ? 1 : 0;
}

typedef struct {
    int batt, power, login, net;
} os_state_t;

static void state_apply(os_state_t *st)
{
    os_state_t next;
    next.batt  = os_battery();
    next.power = os_power();
    next.login = os_login();
    next.net   = os_net();
    if (memcmp(&next, st, sizeof(next)) != 0) {
        log_inf("OS state: batt=%d power=%d login=%d net=%d",
                next.batt, next.power, next.login, next.net);
        *st = next;
    }
}

/* Answer one OS? request. 0 on success, -1 if the tty died mid-write. */
static int answer_os(tty_t *t, const os_state_t *st)
{
    char resp[128];
    int n = snprintf(resp, sizeof(resp),
                     "OK OS batt=%d power=%d user=%d net=%d\n",
                     st->batt, st->power, st->login, st->net);
    if (n <= 0) return 0;
    const char *p = resp;
    int left = n;
    while (left > 0 && g_running) {
        ssize_t w = write(t->fd, p, (size_t)left);
        if (w < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return -1;
        }
        p  += w;
        left -= (int)w;
    }
    log_dbg("answered OS? -> batt=%d power=%d user=%d net=%d",
            st->batt, st->power, st->login, st->net);
    return 0;
}

/* Read one line (blocking, bounded by READ_POLL_US). Returns 0 on a line,
 * 1 on timeout/no-line-yet, -1 on fatal tty error/EOF. */
static int tty_read_line(tty_t *t, char *out, size_t cap)
{
    for (;;) {
        char *nl = (char *)memchr(t->pending, '\n', t->plen);
        if (nl) {
            size_t linelen = (size_t)(nl - t->pending);
            unsigned char cr_only = (linelen == 1 && t->pending[0] == '\r');
            if (linelen == 0 || cr_only) {       /* skip empty / CR-only lines */
                memmove(t->pending, nl + 1, t->plen - (size_t)(nl + 1 - t->pending));
                t->plen -= (size_t)(nl + 1 - t->pending);
                continue;
            }
            if (linelen >= cap) linelen = cap - 1;
            memcpy(out, t->pending, linelen);
            out[linelen] = '\0';
            while (linelen > 0 && (out[linelen - 1] == '\r' || out[linelen - 1] == '\n'))
                out[--linelen] = '\0';
            memmove(t->pending, nl + 1, t->plen - (size_t)(nl + 1 - t->pending));
            t->plen -= (size_t)(nl + 1 - t->pending);
            return 0;
        }
        if (t->plen >= PEND_CAP - 1) {
            log_wrn("tty line buffer overflow - dropping partial line");
            t->plen = 0;
            continue;
        }
        fd_set rfds;
        struct timeval tv;
        FD_ZERO(&rfds);
        FD_SET(t->fd, &rfds);
        tv.tv_sec = 0;
        tv.tv_usec = READ_POLL_US;
        int r = select(t->fd + 1, &rfds, NULL, NULL, &tv);
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (r == 0) return 1;
        /* Read into a modest bounded stack buffer first; the compiler then
         * proves the destination size, and we append only what fits. */
        char tmp[256];
        ssize_t n = read(t->fd, tmp, sizeof(tmp));
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return -1;
        }
        if (n == 0) return -1;             /* EOF: device dropped off USB */
        size_t take = ((size_t)n < (PEND_CAP - 1 - t->plen))
                    ? (size_t)n : (PEND_CAP - 1 - t->plen);
        memcpy(t->pending + t->plen, tmp, take);
        t->plen += take;
    }
}

static void usage(FILE *to, const char *prog)
{
    fprintf(to,
        "Usage: %s [options]\n"
        "Answers the ESP32 Find My beacon's OS? status polls over UART and\n"
        "reports this laptop's battery / power / login / internet state.\n"
        "\n"
        "  -d, --debug         verbose (DEBUG) logging\n"
        "  -l, --log FILE      also append logs to FILE (e.g. /var/log/...)\n"
        "  -t, --tty PATH      serial device (default %s, env FINDMY_TTY)\n"
        "  -i, --interval SEC  health refresh seconds (default %d)\n"
        "  -h, --help          show this help and exit\n",
        prog, TTY_DEFAULT, INTERVAL_DEFAULT);
}

int main(int argc, char **argv)
{
    const char *tty_path = TTY_DEFAULT;
    const char *log_path = NULL;
    long interval = INTERVAL_DEFAULT;

    const char *env_tty = getenv("FINDMY_TTY");
    if (env_tty && *env_tty) tty_path = env_tty;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "-h") || !strcmp(a, "--help")) {
            usage(stdout, argv[0]);
            return 0;
        } else if (!strcmp(a, "-d") || !strcmp(a, "--debug")) {
            g_log.debug = true;
        } else if (!strcmp(a, "-l") || !strcmp(a, "--log")) {
            if (i + 1 >= argc) { fprintf(stderr, "--log needs a FILE\n"); return 2; }
            log_path = argv[++i];
        } else if (!strcmp(a, "-t") || !strcmp(a, "--tty")) {
            if (i + 1 >= argc) { fprintf(stderr, "--tty needs a PATH\n"); return 2; }
            tty_path = argv[++i];
        } else if (!strcmp(a, "-i") || !strcmp(a, "--interval")) {
            unsigned long v;
            if (i + 1 >= argc || parse_u32(argv[++i], &v) != 0 || v < 1 || v > 86400) {
                fprintf(stderr, "--interval: needs 1..86400\n");
                return 2;
            }
            interval = (long)v;
        } else {
            fprintf(stderr, "unknown option: %s\n", a);
            usage(stderr, argv[0]);
            return 2;
        }
    }

    if (log_path) {
        g_log.file = fopen(log_path, "a");
        if (!g_log.file) {
            fprintf(stderr, "cannot open log file '%s': %s\n",
                    log_path, strerror(errno));
            return 2;
        }
    }

    install_signals();

    tty_t tty = { -1, "", {0}, 0 };
    snprintf(tty.path, sizeof(tty.path), "%s", tty_path);

    os_state_t st = { 0, 1, 0, 0 };
    state_apply(&st);                        /* up-front so the reply is fresh */

    log_inf("findmy-os-daemon starting: tty=%s interval=%lds debug=%d",
            tty.path, interval, g_log.debug ? 1 : 0);

    time_t last_refresh = time(NULL);
    int was_up = 0;
    int retry_delay = 1;
    time_t last_open = 0;

    while (g_running) {
        time_t now = time(NULL);

        if (now - last_refresh >= interval) {
            state_apply(&st);
            last_refresh = now;
        }

        if (tty.fd < 0) {
            if (difftime(now, last_open) >= (double)retry_delay) {
                last_open = now;
                if (tty_open(&tty) == 0) {
                    log_inf("connected to %s", tty.path);
                    was_up = 1;
                    retry_delay = 1;
                } else {
                    int absent = (errno == ENOENT || errno == ENXIO || errno == EBUSY);
                    if (was_up) {
                        log_wrn("lost %s: %s - reconnecting (delay %ds)",
                                tty.path, strerror(errno), retry_delay);
                        was_up = 0;
                    }
                    if (!absent) {           /* real error: retry fast */
                        retry_delay = 1;
                    } else if (retry_delay < RECONNECT_MAX_S) {
                        retry_delay *= 2;    /* absent: back off, keep trying */
                    }
                }
            }
            struct timespec ts = { 0, 250 * 1000000L };
            while (nanosleep(&ts, &ts) != 0 && errno == EINTR && g_running) { }
            continue;
        }

        char line[MAX_LINE];
        int rc = tty_read_line(&tty, line, sizeof(line));
        if (rc == -1) {
            log_inf("tty read error on %s - closing (device likely in deep "
                    "sleep)", tty.path);
            tty_close(&tty);
            retry_delay = 1;                 /* reconnect fast when it returns */
            continue;
        }
        if (rc == 0) {
            if (strcmp(line, "OS?") == 0) {
                if (answer_os(&tty, &st) != 0) {
                    tty_close(&tty);
                    continue;
                }
            } else {
                log_dbg("ignoring non-OS? line: '%s'", line);
            }
        }
        /* rc == 1: read timeout, nothing to do */
    }

    tty_close(&tty);
    log_inf("findmy-os-daemon stopping");
    if (g_log.file) fclose(g_log.file);
    return 0;
}
