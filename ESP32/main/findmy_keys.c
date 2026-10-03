#include <string.h>

#include "esp_log.h"
#include "findmy_keys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "mbedtls/bignum.h"
#include "mbedtls/private/sha256.h"

static const char *TAG = "findmy_keys";

int fm_base64_decode(const char *src, uint8_t *dst, size_t dst_size)
{
    size_t out = 0;
    unsigned val = 0;
    int bits = 0;

    for (; *src; ++src) {
        if (*src == '=' || *src == '\n' || *src == '\r') {
            continue;
        }
        int c;
        if (*src >= 'A' && *src <= 'Z') c = *src - 'A';
        else if (*src >= 'a' && *src <= 'z') c = *src - 'a' + 26;
        else if (*src >= '0' && *src <= '9') c = *src - '0' + 52;
        else if (*src == '+') c = 62;
        else if (*src == '/') c = 63;
        else return -1;

        val = (val << 6) | (unsigned)c;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (out >= dst_size) {
                return -1;
            }
            dst[out++] = (uint8_t)((val >> bits) & 0xFF);
        }
    }
    return (int)out;
}

/* ------------------------------------------------------------------ */
/* P-224 (secp224r1). Mbed TLS 4 dropped P-224 support, so the curve
 * arithmetic is implemented here on top of mbedtls_mpi. */
/* ------------------------------------------------------------------ */

/* Order of the P-224 group */
static const char P224_N_HEX[] =
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFF16A2E0B8F03E13DD29455C5C2A3D";
/* Field prime */
static const char P224_P_HEX[] =
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF000000000000000000000001";
/* Generator */
static const char P224_GX_HEX[] =
    "B70E0CBD6BB4BF7F321390B94A03C1D356C21122343280D6115C1D21";
static const char P224_GY_HEX[] =
    "BD376388B5F723FB4C22DFE6CD4375A05A07476444D5819985007E34";

typedef struct {
    mbedtls_mpi X, Y, Z; /* Jacobian coordinates; Z == 0 is the point at infinity */
} p224_point;

static mbedtls_mpi P224_P;
static bool P224_P_loaded = false;

static int p224_init_curve(void)
{
    if (P224_P_loaded) {
        return 0;
    }
    if (mbedtls_mpi_read_string(&P224_P, 16, P224_P_HEX) != 0) {
        return -1;
    }
    P224_P_loaded = true;
    return 0;
}

static void p224_point_init(p224_point *pt)
{
    mbedtls_mpi_init(&pt->X);
    mbedtls_mpi_init(&pt->Y);
    mbedtls_mpi_init(&pt->Z);
}

static void p224_point_free(p224_point *pt)
{
    mbedtls_mpi_free(&pt->X);
    mbedtls_mpi_free(&pt->Y);
    mbedtls_mpi_free(&pt->Z);
}

static int p224_copy(p224_point *dst, const p224_point *src)
{
    return mbedtls_mpi_copy(&dst->X, &src->X) ||
                   mbedtls_mpi_copy(&dst->Y, &src->Y) ||
                   mbedtls_mpi_copy(&dst->Z, &src->Z)
               ? -1
               : 0;
}

static int p224_set_infinity(p224_point *pt)
{
    return mbedtls_mpi_lset(&pt->X, 1) || mbedtls_mpi_lset(&pt->Y, 1) ||
                   mbedtls_mpi_lset(&pt->Z, 0)
               ? -1
               : 0;
}

static int p224_is_infinity(const p224_point *pt)
{
    return mbedtls_mpi_cmp_int(&pt->Z, 0) == 0;
}

static int p224_mod(mbedtls_mpi *r, const mbedtls_mpi *a)
{
    return mbedtls_mpi_mod_mpi(r, a, &P224_P) ? -1 : 0;
}

static int p224_modmul(mbedtls_mpi *r, const mbedtls_mpi *a, const mbedtls_mpi *b)
{
    mbedtls_mpi t;
    int rc = -1;

    mbedtls_mpi_init(&t);
    if (mbedtls_mpi_mul_mpi(&t, a, b) == 0 && p224_mod(r, &t) == 0) {
        rc = 0;
    }
    mbedtls_mpi_free(&t);
    return rc;
}

/* Jacobian doubling, exploits a = -3: alpha = 3*(X - Z^2)*(X + Z^2) */
static int p224_double(p224_point *pt)
{
    mbedtls_mpi delta, gamma, beta, alpha, t, x3, y3, z3;
    int rc = -1;

    if (p224_is_infinity(pt)) {
        return 0;
    }

    mbedtls_mpi_init(&delta);
    mbedtls_mpi_init(&gamma);
    mbedtls_mpi_init(&beta);
    mbedtls_mpi_init(&alpha);
    mbedtls_mpi_init(&t);
    mbedtls_mpi_init(&x3);
    mbedtls_mpi_init(&y3);
    mbedtls_mpi_init(&z3);

    if (p224_modmul(&delta, &pt->Z, &pt->Z) != 0 ||          /* delta = Z^2 */
        p224_modmul(&gamma, &pt->Y, &pt->Y) != 0 ||          /* gamma = Y^2 */
        p224_modmul(&beta, &pt->X, &gamma) != 0) {           /* beta  = X*gamma */
        goto cleanup;
    }

    /* alpha = 3*(X - delta)*(X + delta) */
    if (mbedtls_mpi_sub_mpi(&t, &pt->X, &delta) != 0 ||
        mbedtls_mpi_add_mpi(&alpha, &pt->X, &delta) != 0 ||
        p224_modmul(&alpha, &t, &alpha) != 0 ||
        mbedtls_mpi_mul_int(&t, &alpha, 3) != 0 ||
        p224_mod(&alpha, &t) != 0) {
        goto cleanup;
    }

    /* x3 = alpha^2 - 8*beta */
    if (mbedtls_mpi_mul_int(&t, &beta, 8) != 0 || p224_mod(&t, &t) != 0 ||
        p224_modmul(&x3, &alpha, &alpha) != 0 ||
        mbedtls_mpi_sub_mpi(&x3, &x3, &t) != 0 || p224_mod(&x3, &x3) != 0) {
        goto cleanup;
    }

    /* z3 = (Y + Z)^2 - gamma - delta */
    if (mbedtls_mpi_add_mpi(&z3, &pt->Y, &pt->Z) != 0 ||
        p224_modmul(&z3, &z3, &z3) != 0 ||
        mbedtls_mpi_sub_mpi(&z3, &z3, &gamma) != 0 ||
        mbedtls_mpi_sub_mpi(&z3, &z3, &delta) != 0 ||
        p224_mod(&z3, &z3) != 0) {
        goto cleanup;
    }

    /* y3 = alpha*(4*beta - x3) - 8*gamma^2 */
    if (mbedtls_mpi_mul_int(&t, &beta, 4) != 0 || p224_mod(&t, &t) != 0 ||
        mbedtls_mpi_sub_mpi(&t, &t, &x3) != 0 || p224_mod(&t, &t) != 0 ||
        p224_modmul(&y3, &alpha, &t) != 0 ||
        p224_modmul(&t, &gamma, &gamma) != 0 ||
        mbedtls_mpi_mul_int(&t, &t, 8) != 0 || p224_mod(&t, &t) != 0 ||
        mbedtls_mpi_sub_mpi(&y3, &y3, &t) != 0 || p224_mod(&y3, &y3) != 0) {
        goto cleanup;
    }

    if (mbedtls_mpi_copy(&pt->X, &x3) != 0 || mbedtls_mpi_copy(&pt->Y, &y3) != 0 ||
        mbedtls_mpi_copy(&pt->Z, &z3) != 0) {
        goto cleanup;
    }
    rc = 0;

cleanup:
    mbedtls_mpi_free(&delta);
    mbedtls_mpi_free(&gamma);
    mbedtls_mpi_free(&beta);
    mbedtls_mpi_free(&alpha);
    mbedtls_mpi_free(&t);
    mbedtls_mpi_free(&x3);
    mbedtls_mpi_free(&y3);
    mbedtls_mpi_free(&z3);
    return rc;
}

/* Jacobian addition pt += q */
static int p224_add(p224_point *pt, const p224_point *q)
{
    mbedtls_mpi z1z1, z2z2, u1, u2, s1, s2, h, r, t, h2, h3, x3, y3, z3;
    int rc = -1;

    if (p224_is_infinity(q)) {
        return 0;
    }
    if (p224_is_infinity(pt)) {
        return p224_copy(pt, q);
    }

    mbedtls_mpi_init(&z1z1);
    mbedtls_mpi_init(&z2z2);
    mbedtls_mpi_init(&u1);
    mbedtls_mpi_init(&u2);
    mbedtls_mpi_init(&s1);
    mbedtls_mpi_init(&s2);
    mbedtls_mpi_init(&h);
    mbedtls_mpi_init(&r);
    mbedtls_mpi_init(&t);
    mbedtls_mpi_init(&h2);
    mbedtls_mpi_init(&h3);
    mbedtls_mpi_init(&x3);
    mbedtls_mpi_init(&y3);
    mbedtls_mpi_init(&z3);

    if (p224_modmul(&z1z1, &pt->Z, &pt->Z) != 0 ||
        p224_modmul(&z2z2, &q->Z, &q->Z) != 0 ||
        p224_modmul(&u1, &pt->X, &z2z2) != 0 ||
        p224_modmul(&u2, &q->X, &z1z1) != 0 ||
        p224_modmul(&s1, &pt->Y, &z2z2) != 0 ||
        p224_modmul(&s1, &s1, &q->Z) != 0 ||
        p224_modmul(&s2, &q->Y, &z1z1) != 0 ||
        p224_modmul(&s2, &s2, &pt->Z) != 0) {
        goto cleanup;
    }

    if (mbedtls_mpi_sub_mpi(&h, &u2, &u1) != 0 || p224_mod(&h, &h) != 0 ||
        mbedtls_mpi_sub_mpi(&r, &s2, &s1) != 0 || p224_mod(&r, &r) != 0) {
        goto cleanup;
    }

    if (mbedtls_mpi_cmp_int(&h, 0) == 0) {
        if (mbedtls_mpi_cmp_int(&r, 0) == 0) {
            rc = p224_double(pt);
        } else {
            rc = p224_set_infinity(pt);
        }
        goto cleanup;
    }

    if (p224_modmul(&h2, &h, &h) != 0 ||
        p224_modmul(&h3, &h2, &h) != 0 ||
        p224_modmul(&z3, &h, &pt->Z) != 0 ||
        p224_modmul(&z3, &z3, &q->Z) != 0 ||
        mbedtls_mpi_mul_int(&t, &u1, 2) != 0 ||
        p224_modmul(&t, &t, &h2) != 0 ||
        p224_modmul(&x3, &r, &r) != 0 ||
        mbedtls_mpi_sub_mpi(&x3, &x3, &h3) != 0 ||
        mbedtls_mpi_sub_mpi(&x3, &x3, &t) != 0 ||
        p224_mod(&x3, &x3) != 0 ||
        p224_modmul(&t, &u1, &h2) != 0 ||
        mbedtls_mpi_sub_mpi(&t, &t, &x3) != 0 ||
        p224_mod(&t, &t) != 0 ||
        p224_modmul(&y3, &r, &t) != 0 ||
        p224_modmul(&t, &s1, &h3) != 0 ||
        mbedtls_mpi_sub_mpi(&y3, &y3, &t) != 0 ||
        p224_mod(&y3, &y3) != 0) {
        goto cleanup;
    }

    if (mbedtls_mpi_copy(&pt->X, &x3) != 0 || mbedtls_mpi_copy(&pt->Y, &y3) != 0 ||
        mbedtls_mpi_copy(&pt->Z, &z3) != 0) {
        goto cleanup;
    }
    rc = 0;

cleanup:
    mbedtls_mpi_free(&z1z1);
    mbedtls_mpi_free(&z2z2);
    mbedtls_mpi_free(&u1);
    mbedtls_mpi_free(&u2);
    mbedtls_mpi_free(&s1);
    mbedtls_mpi_free(&s2);
    mbedtls_mpi_free(&h);
    mbedtls_mpi_free(&r);
    mbedtls_mpi_free(&t);
    mbedtls_mpi_free(&h2);
    mbedtls_mpi_free(&h3);
    mbedtls_mpi_free(&x3);
    mbedtls_mpi_free(&y3);
    mbedtls_mpi_free(&z3);
    return rc;
}

/* pt = scalar * base_point */
static int p224_mul_gen(p224_point *pt, const mbedtls_mpi *scalar)
{
    p224_point acc, base;
    mbedtls_mpi s;
    size_t bits;
    int rc = -1;

    p224_point_init(&acc);
    p224_point_init(&base);
    mbedtls_mpi_init(&s);

    if (mbedtls_mpi_copy(&s, scalar) != 0 ||
        mbedtls_mpi_read_string(&base.X, 16, P224_GX_HEX) != 0 ||
        mbedtls_mpi_read_string(&base.Y, 16, P224_GY_HEX) != 0 ||
        mbedtls_mpi_lset(&base.Z, 1) != 0 ||
        p224_set_infinity(&acc) != 0) {
        goto cleanup;
    }

    bits = mbedtls_mpi_bitlen(&s);
    for (size_t i = bits; i > 0; i--) {
        if (p224_double(&acc) != 0) {
            goto cleanup;
        }
        if (mbedtls_mpi_get_bit(&s, i - 1)) {
            if (p224_add(&acc, &base) != 0) {
                goto cleanup;
            }
        }
    }

    rc = p224_copy(pt, &acc);

cleanup:
    p224_point_free(&acc);
    p224_point_free(&base);
    mbedtls_mpi_free(&s);
    return rc;
}

/* ------------------------------------------------------------------ */
/* Key chain state (all persisted in NVS, delivered via UART pairing) */
/* ------------------------------------------------------------------ */

static uint8_t fm_master[FM_MASTER_LEN];
static uint8_t fm_skn[FM_SK_LEN];     /* initial SK of the primary chain */
static uint8_t fm_sk[FM_SK_LEN];      /* current SK of the primary chain */
static uint32_t fm_slot = 0;
static bool fm_paired = false;
static bool fm_debug = false;

/* Advisory timing parameters. */
static uint32_t fm_adv_ms = FM_ADV_MS_DEFAULT;
static uint32_t fm_rot_sec = FM_SLOT_SECONDS;
static uint32_t fm_dbg_sec = FM_DBG_SEC_DEFAULT;

/* Low-battery mode: engaged flag + slots to deep-sleep per active slot. */
static bool fm_lowbatt = false;
static uint32_t fm_loslots = FM_SKIP_SLOTS_DEFAULT;

/* Console PIN + wrong-UNLOCK counter (see findmy_keys.h). */
static char fm_pin[FM_PIN_LEN + 1] = FM_PIN_DEFAULT;
static uint32_t fm_pin_fail_count = 0;

/* Device name (see findmy_keys.h): "" until the first NAME command. */
static char fm_name[FM_NAME_LEN + 1] = "";

static uint32_t clamp_adv_ms(uint32_t v)
{
    if (v < FM_ADV_MS_MIN) return FM_ADV_MS_MIN;
    if (v > FM_ADV_MS_MAX) return FM_ADV_MS_MAX;
    return v;
}

static uint32_t clamp_rot_sec(uint32_t v)
{
    if (v < FM_ROT_SEC_MIN) return FM_ROT_SEC_MIN;
    if (v > FM_ROT_SEC_MAX) return FM_ROT_SEC_MAX;
    return v;
}

static uint32_t clamp_dbg_sec(uint32_t v)
{
    if (v == 0) return 0;
    if (v < FM_DBG_SEC_MIN || v > FM_DBG_SEC_MAX) return FM_DBG_SEC_DEFAULT;
    return v;
}

static uint32_t clamp_loslots(uint32_t v)
{
    if (v == 0) return FM_SKIP_SLOTS_DEFAULT;
    if (v > FM_SKIP_SLOTS_MAX) return FM_SKIP_SLOTS_MAX;
    return v;
}

static SemaphoreHandle_t fm_lock = NULL;

/* ANSI X9.63 KDF with SHA-256:
 * K(i) = SHA256(z || counter_be32(i) || info), blocks concatenated. */
static int x963_kdf_sha256(const uint8_t *z, size_t z_len,
                           const uint8_t *info, size_t info_len,
                           uint8_t *out, size_t out_len)
{
    mbedtls_sha256_context sha;
    uint32_t counter = 1;
    size_t done = 0;

    while (done < out_len) {
        uint8_t ctr[4] = {
            (uint8_t)(counter >> 24), (uint8_t)(counter >> 16),
            (uint8_t)(counter >> 8), (uint8_t)counter,
        };
        uint8_t blk[32];
        size_t take;

        mbedtls_sha256_init(&sha);
        if (mbedtls_sha256_starts(&sha, 0) != 0 ||
            mbedtls_sha256_update(&sha, z, z_len) != 0 ||
            mbedtls_sha256_update(&sha, ctr, 4) != 0 ||
            mbedtls_sha256_update(&sha, info, info_len) != 0 ||
            mbedtls_sha256_finish(&sha, blk) != 0) {
            mbedtls_sha256_free(&sha);
            return -1;
        }
        mbedtls_sha256_free(&sha);

        take = out_len - done < 32 ? out_len - done : 32;
        memcpy(out + done, blk, take);
        done += take;
        counter++;
    }
    return 0;
}

/* priv = (u * master + v) mod n, where u,v come from X963("diversify", 72) */
static int derive_scalar(const uint8_t master[FM_MASTER_LEN],
                         const uint8_t sk[FM_SK_LEN],
                         mbedtls_mpi *priv)
{
    uint8_t at[72];
    mbedtls_mpi u, v, m, n, nm1, t;
    int rc = -1;

    mbedtls_mpi_init(&u);
    mbedtls_mpi_init(&v);
    mbedtls_mpi_init(&m);
    mbedtls_mpi_init(&n);
    mbedtls_mpi_init(&nm1);
    mbedtls_mpi_init(&t);

    if (x963_kdf_sha256(sk, FM_SK_LEN, (const uint8_t *)"diversify", 9,
                        at, sizeof(at)) != 0) {
        goto cleanup;
    }
    if (mbedtls_mpi_read_binary(&u, at, 36) != 0 ||
        mbedtls_mpi_read_binary(&v, at + 36, 36) != 0 ||
        mbedtls_mpi_read_binary(&m, master, FM_MASTER_LEN) != 0 ||
        mbedtls_mpi_read_string(&n, 16, P224_N_HEX) != 0) {
        goto cleanup;
    }
    if (mbedtls_mpi_copy(&nm1, &n) != 0 ||
        mbedtls_mpi_sub_int(&nm1, &nm1, 1) != 0) {
        goto cleanup;
    }
    /* u = at[:36] % (n-1) + 1 ; v = at[36:] % (n-1) + 1 */
    if (mbedtls_mpi_mod_mpi(&u, &u, &nm1) != 0 ||
        mbedtls_mpi_add_int(&u, &u, 1) != 0 ||
        mbedtls_mpi_mod_mpi(&v, &v, &nm1) != 0 ||
        mbedtls_mpi_add_int(&v, &v, 1) != 0) {
        goto cleanup;
    }
    /* priv = (u * master + v) % n */
    if (mbedtls_mpi_mul_mpi(&t, &u, &m) != 0 ||
        mbedtls_mpi_add_mpi(&t, &t, &v) != 0 ||
        mbedtls_mpi_mod_mpi(priv, &t, &n) != 0) {
        goto cleanup;
    }

    rc = 0;

cleanup:
    mbedtls_mpi_free(&u);
    mbedtls_mpi_free(&v);
    mbedtls_mpi_free(&m);
    mbedtls_mpi_free(&n);
    mbedtls_mpi_free(&nm1);
    mbedtls_mpi_free(&t);
    return rc;
}

static int derive_pubkey_x(const mbedtls_mpi *priv, uint8_t x_out[28])
{
    p224_point Q;
    mbedtls_mpi zi, zi2;
    int rc = -1;

    p224_point_init(&Q);
    mbedtls_mpi_init(&zi);
    mbedtls_mpi_init(&zi2);

    if (p224_mul_gen(&Q, priv) != 0 || p224_is_infinity(&Q)) {
        goto cleanup;
    }

    /* affine x = X / Z^2 mod p */
    if (mbedtls_mpi_inv_mod(&zi, &Q.Z, &P224_P) != 0 ||
        p224_modmul(&zi2, &zi, &zi) != 0 ||
        p224_modmul(&zi2, &Q.X, &zi2) != 0 ||
        mbedtls_mpi_write_binary(&zi2, x_out, 28) != 0) {
        goto cleanup;
    }

    rc = 0;

cleanup:
    p224_point_free(&Q);
    mbedtls_mpi_free(&zi);
    mbedtls_mpi_free(&zi2);
    return rc;
}

static int nvs_write_all(void)
{
    nvs_handle_t h;
    uint8_t dbg = fm_debug;

    if (nvs_open("fmkeys", NVS_READWRITE, &h) != ESP_OK) {
        return -1;
    }
    if (fm_paired) {
        nvs_set_blob(h, "mk", fm_master, FM_MASTER_LEN);
        nvs_set_blob(h, "skn", fm_skn, FM_SK_LEN);
        nvs_set_blob(h, "sk", fm_sk, FM_SK_LEN);
        nvs_set_u32(h, "slot", fm_slot);
    } else {
        /* The key buffers are zeroed while unpaired. Writing them would
         * leave "all four present" in NVS and the next boot would report
         * paired=1 with a zero key chain - drop stale keys instead. */
        nvs_erase_key(h, "mk");
        nvs_erase_key(h, "skn");
        nvs_erase_key(h, "sk");
        nvs_erase_key(h, "slot");
    }
    nvs_set_blob(h, "dbg", &dbg, 1);
    nvs_set_u32(h, "advms", fm_adv_ms);
    nvs_set_u32(h, "rotsec", fm_rot_sec);
    nvs_set_u32(h, "dbgsec", fm_dbg_sec);
    nvs_set_u32(h, "loslots", fm_loslots);
    nvs_set_u8(h, "lomode", fm_lowbatt ? 1 : 0);
    nvs_commit(h);
    nvs_close(h);
    return 0;
}

/* An all-zero master key can never be produced by pairing, but it can be
 * left behind in NVS by an older build that persisted the zeroed buffers
 * of an unpaired device. Reject it instead of reporting paired=1. */
static bool master_key_usable(const uint8_t mk[FM_MASTER_LEN])
{
    uint8_t acc = 0;

    for (size_t i = 0; i < FM_MASTER_LEN; i++) {
        acc |= mk[i];
    }
    return acc != 0;
}

/* Deriving the P-224 public key takes ~2.2 s of CPU, so the result is
 * cached for the current slot/key chain. Only fm_advance_slot/fm_pair/
 * fm_unpair/fm_key_init change the inputs; they invalidate the cache. */
static bool pk_cache_valid = false;
static uint32_t pk_cache_slot;
static uint8_t pk_cache_x[28];

int fm_key_init(void)
{
    nvs_handle_t h;
    size_t len = 0;
    uint8_t mk[FM_MASTER_LEN], skn[FM_SK_LEN], sk[FM_SK_LEN], dbg = 0;

    if (fm_lock == NULL) {
        fm_lock = xSemaphoreCreateMutex();
        if (fm_lock == NULL) {
            return -1;
        }
    }
    if (p224_init_curve() != 0) {
        ESP_LOGE(TAG, "failed to load P-224 parameters");
        return -1;
    }
    pk_cache_valid = false;

    if (nvs_open("fmkeys", NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "no key state in NVS (unpaired)");
        return 0;
    }
    len = FM_MASTER_LEN;
    bool have_mk = nvs_get_blob(h, "mk", mk, &len) == ESP_OK;
    len = FM_SK_LEN;
    bool have_skn = nvs_get_blob(h, "skn", skn, &len) == ESP_OK;
    len = FM_SK_LEN;
    bool have_sk = nvs_get_blob(h, "sk", sk, &len) == ESP_OK;
    if (have_mk && have_skn && have_sk &&
        nvs_get_u32(h, "slot", &fm_slot) == ESP_OK && master_key_usable(mk)) {
        memcpy(fm_master, mk, FM_MASTER_LEN);
        memcpy(fm_skn, skn, FM_SK_LEN);
        memcpy(fm_sk, sk, FM_SK_LEN);
        fm_paired = true;
        ESP_LOGI(TAG, "resumed key chain at slot %u", fm_slot);
    } else {
        ESP_LOGI(TAG, "incomplete key state in NVS (unpaired)");
    }
    len = 1;
    if (nvs_get_blob(h, "dbg", &dbg, &len) == ESP_OK) {
        fm_debug = dbg != 0;
    }
    uint32_t adv_ms = 0, rot_sec = 0, dbg_sec = 0;
    if (nvs_get_u32(h, "advms", &adv_ms) == ESP_OK) {
        fm_adv_ms = clamp_adv_ms(adv_ms);
    }
    if (nvs_get_u32(h, "rotsec", &rot_sec) == ESP_OK) {
        fm_rot_sec = clamp_rot_sec(rot_sec);
    }
    if (nvs_get_u32(h, "dbgsec", &dbg_sec) == ESP_OK) {
        fm_dbg_sec = clamp_dbg_sec(dbg_sec);
    }
    uint32_t loslots = 0;
    uint8_t lomode = 0;
    if (nvs_get_u32(h, "loslots", &loslots) == ESP_OK) {
        fm_loslots = clamp_loslots(loslots);
    }
    if (nvs_get_u8(h, "lomode", &lomode) == ESP_OK) {
        fm_lowbatt = (lomode != 0);
    } else {
        fm_lowbatt = false;
    }
    /* Console PIN + failure counter: absent before the first PIN command
     * or after a wipe, in which case the factory defaults stay. */
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) == pdTRUE) {
        len = sizeof(fm_pin);
        if (nvs_get_str(h, "pin", fm_pin, &len) != ESP_OK) {
            memcpy(fm_pin, FM_PIN_DEFAULT, FM_PIN_LEN + 1);
        }
        if (nvs_get_u32(h, "pinfails", &fm_pin_fail_count) != ESP_OK) {
            fm_pin_fail_count = 0;
        }
        len = sizeof(fm_name);
        if (nvs_get_str(h, "name", fm_name, &len) != ESP_OK) {
            fm_name[0] = '\0';
        }
        xSemaphoreGive(fm_lock);
    }
    nvs_close(h);
    return 0;
}

bool fm_is_paired(void)
{
    return fm_paired;
}

uint32_t fm_current_slot(void)
{
    return fm_slot;
}

int fm_current_pubkey(uint8_t x_out[28])
{
    mbedtls_mpi priv;
    int rc;

    if (!fm_paired) {
        return -1;
    }
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    if (pk_cache_valid && pk_cache_slot == fm_slot) {
        memcpy(x_out, pk_cache_x, 28);
        xSemaphoreGive(fm_lock);
        return 0;
    }
    mbedtls_mpi_init(&priv);
    rc = derive_scalar(fm_master, fm_sk, &priv);
    if (rc == 0) {
        rc = derive_pubkey_x(&priv, x_out);
    }
    mbedtls_mpi_free(&priv);
    if (rc == 0) {
        memcpy(pk_cache_x, x_out, 28);
        pk_cache_slot = fm_slot;
        pk_cache_valid = true;
    }
    xSemaphoreGive(fm_lock);
    return rc;
}

/* Advance the SK chain by `n` slots without ever deriving a public key for
 * the skipped slots, then persist once. The X963/SHA-256 "update" KDF is
 * the whole cost (a few SHA-256 hashes per slot); the P-224 public key
 * derivation is deliberately left to one fm_current_pubkey() call on the
 * slot the caller actually advertises. */
int fm_advance_slots(uint32_t n)
{
    uint8_t next[FM_SK_LEN];

    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    if (!fm_paired) {
        xSemaphoreGive(fm_lock);
        return -1;
    }
    for (uint32_t k = 0; k < n; k++) {
        if (x963_kdf_sha256(fm_sk, FM_SK_LEN, (const uint8_t *)"update", 6,
                            next, FM_SK_LEN) != 0) {
            xSemaphoreGive(fm_lock);
            return -1;
        }
        memcpy(fm_sk, next, FM_SK_LEN);
        fm_slot++;
    }
    pk_cache_valid = false;
    if (nvs_write_all() != 0) {
        ESP_LOGE(TAG, "failed to persist key chain state");
        xSemaphoreGive(fm_lock);
        return -1;
    }
    xSemaphoreGive(fm_lock);
    return 0;
}

int fm_advance_slot(void)
{
    return fm_advance_slots(1);
}

bool fm_low_battery_on(void)
{
    return fm_lowbatt;
}

uint32_t fm_skip_slots(void)
{
    return fm_lowbatt ? fm_loslots : 0;
}

int fm_set_low_battery(bool on, uint32_t slots)
{
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    fm_lowbatt = on;
    fm_loslots = clamp_loslots(slots);
    int rc = nvs_write_all();
    xSemaphoreGive(fm_lock);
    return rc;
}

int fm_set_os_battery(bool low)
{
    return fm_set_low_battery(low, FM_SKIP_SLOTS_DEFAULT);
}

int fm_pair(const uint8_t master[FM_MASTER_LEN],
            const uint8_t skn[FM_SK_LEN])
{
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    memcpy(fm_master, master, FM_MASTER_LEN);
    memcpy(fm_skn, skn, FM_SK_LEN);
    memcpy(fm_sk, skn, FM_SK_LEN);
    fm_slot = 0;
    fm_paired = true;
    pk_cache_valid = false;
    if (nvs_write_all() != 0) {
        fm_paired = false;
        xSemaphoreGive(fm_lock);
        return -1;
    }
    xSemaphoreGive(fm_lock);
    return 0;
}

/* Factory reset: drop the key chain, the console PIN and the UNLOCK
 * failure counter, plus every stored advisory parameter (adv_ms/rot_sec/
 * dbg_sec/debug fall back to their compile-time defaults). */
int fm_unpair(void)
{
    nvs_handle_t h;

    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    if (nvs_open("fmkeys", NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_all(h);
        nvs_commit(h);
        nvs_close(h);
    }
    memset(fm_master, 0, sizeof(fm_master));
    memset(fm_skn, 0, sizeof(fm_skn));
    memset(fm_sk, 0, sizeof(fm_sk));
    fm_slot = 0;
    fm_paired = false;
    fm_debug = false;
    memcpy(fm_pin, FM_PIN_DEFAULT, FM_PIN_LEN + 1);
    fm_pin_fail_count = 0;
    fm_name[0] = '\0';
    pk_cache_valid = false;
    fm_adv_ms = FM_ADV_MS_DEFAULT;
    fm_rot_sec = FM_SLOT_SECONDS;
    fm_dbg_sec = FM_DBG_SEC_DEFAULT;
    fm_lowbatt = false;
    fm_loslots = FM_SKIP_SLOTS_DEFAULT;
    xSemaphoreGive(fm_lock);
    return 0;
}

const char *fm_get_pin(void)
{
    return fm_pin;
}

int fm_set_pin(const char *pin)
{
    nvs_handle_t h;

    if (pin == NULL || strlen(pin) != FM_PIN_LEN) {
        return -1;
    }
    for (int i = 0; i < FM_PIN_LEN; i++) {
        if (pin[i] < '0' || pin[i] > '9') {
            return -1;
        }
    }
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    memcpy(fm_pin, pin, FM_PIN_LEN + 1);
    int rc = (nvs_open("fmkeys", NVS_READWRITE, &h) != ESP_OK) ? -1
                                                                : 0;
    if (rc == 0) {
        if (nvs_set_str(h, "pin", fm_pin) != ESP_OK) {
            rc = -1;
        }
        nvs_commit(h);
        nvs_close(h);
    }
    xSemaphoreGive(fm_lock);
    return rc;
}

const char *fm_get_name(void)
{
    return fm_name;
}

/* Validation lives here (not in the console) so IDENT?/NAME and any future
 * caller see the same rules: 1..FM_NAME_LEN chars of [A-Za-z0-9_-]. */
int fm_set_name(const char *name)
{
    nvs_handle_t h;
    size_t n;

    if (name == NULL) {
        return -1;
    }
    n = strlen(name);
    if (n == 0 || n > FM_NAME_LEN) {
        return -1;
    }
    for (size_t i = 0; i < n; i++) {
        char c = name[i];
        if (!(c >= 'a' && c <= 'z') && !(c >= 'A' && c <= 'Z') &&
            !(c >= '0' && c <= '9') && c != '-' && c != '_') {
            return -1;
        }
    }
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -2;
    }
    memcpy(fm_name, name, n + 1);
    int rc = (nvs_open("fmkeys", NVS_READWRITE, &h) != ESP_OK) ? -2 : 0;
    if (rc == 0) {
        if (nvs_set_str(h, "name", fm_name) != ESP_OK) {
            rc = -2;
        }
        nvs_commit(h);
        nvs_close(h);
    }
    xSemaphoreGive(fm_lock);
    return rc;
}

/* Persist the failure counter: a power cycle must not clear a lockout. */
static int pin_fails_write(void)
{
    nvs_handle_t h;

    if (nvs_open("fmkeys", NVS_READWRITE, &h) != ESP_OK) {
        return -1;
    }
    int rc = (nvs_set_u32(h, "pinfails", fm_pin_fail_count) == ESP_OK) ? 0
                                                                       : -1;
    nvs_commit(h);
    nvs_close(h);
    return rc;
}

uint32_t fm_pin_fails(void)
{
    return fm_pin_fail_count;
}

int fm_pin_fail_add(void)
{
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    fm_pin_fail_count++;
    int rc = pin_fails_write();
    xSemaphoreGive(fm_lock);
    return rc;
}

int fm_pin_fail_reset(void)
{
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    fm_pin_fail_count = 0;
    int rc = pin_fails_write();
    xSemaphoreGive(fm_lock);
    return rc;
}

bool fm_debug_enabled(void)
{
    return fm_debug;
}
int fm_set_debug(bool enable)
{
    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    fm_debug = enable;
    int rc = nvs_write_all();
    xSemaphoreGive(fm_lock);
    return rc;
}

uint32_t fm_get_adv_ms(void)
{
    return fm_adv_ms;
}

uint32_t fm_get_rot_sec(void)
{
    return fm_rot_sec;
}

uint32_t fm_get_dbg_sec(void)
{
    return fm_dbg_sec;
}

int fm_set_config(uint32_t adv_ms, uint32_t rot_sec, uint32_t dbg_sec)
{
    uint32_t a = clamp_adv_ms(adv_ms);
    uint32_t r = clamp_rot_sec(rot_sec);
    uint32_t d = clamp_dbg_sec(dbg_sec);

    if (xSemaphoreTake(fm_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    fm_adv_ms = a;
    fm_rot_sec = r;
    fm_dbg_sec = d;
    int rc = nvs_write_all();
    xSemaphoreGive(fm_lock);
    return rc;
}
