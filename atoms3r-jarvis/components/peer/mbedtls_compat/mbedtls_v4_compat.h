/**
 * mbedTLS 4.x compatibility shims for libpeer (written for mbedTLS 2.x/3.x)
 *
 * ESP-IDF 6.1 ships mbedTLS 4.x where several APIs were removed or changed:
 * - mbedtls_ssl_conf_rng() removed (PSA handles RNG internally)
 * - mbedtls_ssl_cookie_setup() now takes 1 arg instead of 3
 * - mbedtls_ecp_gen_key() / mbedtls_pk_ec(): key gen now uses PSA internally
 * - mbedtls_entropy_*: made private, replaced by PSA/hardware RNG
 * - mbedtls_x509write_crt_pem(): 5 args → 3 args
 * - pk_setup no longer allocates sub-context (ctx_alloc_func = NULL)
 *
 * This header provides inline replacements so libpeer compiles unchanged.
 */
#ifndef MBEDTLS_V4_COMPAT_H
#define MBEDTLS_V4_COMPAT_H

#include <mbedtls/ssl.h>
#include <mbedtls/ssl_cookie.h>
/* pk_type_t (MBEDTLS_PK_RSA, MBEDTLS_PK_ECKEY) moved to private header in v4 */
#include <mbedtls/private/pk_private.h>

/* =========================================================================
 * 1. mbedtls_ssl_conf_rng() — removed in v4 (PSA handles RNG)
 * ========================================================================= */
static inline void mbedtls_ssl_conf_rng_compat(
    mbedtls_ssl_config *conf,
    int (*f_rng)(void *, unsigned char *, size_t),
    void *p_rng)
{
    (void)conf;
    (void)f_rng;
    (void)p_rng;
}
#define mbedtls_ssl_conf_rng mbedtls_ssl_conf_rng_compat

/* =========================================================================
 * 2. mbedtls_ssl_cookie_setup() — 3 args → 1 arg in v4
 * ========================================================================= */
static inline int mbedtls_ssl_cookie_setup_compat(
    mbedtls_ssl_cookie_ctx *ctx,
    int (*f_rng)(void *, unsigned char *, size_t),
    void *p_rng)
{
    (void)f_rng;
    (void)p_rng;
    return mbedtls_ssl_cookie_setup(ctx);
}
#define mbedtls_ssl_cookie_setup mbedtls_ssl_cookie_setup_compat

/* =========================================================================
 * 3. Entropy stubs — made private in v4, use ESP-IDF hardware RNG
 * ========================================================================= */
#include <mbedtls/private/entropy.h>
#include <esp_random.h>

static inline void mbedtls_entropy_init_compat(mbedtls_entropy_context *ctx)
{
    (void)ctx;
}
#define mbedtls_entropy_init mbedtls_entropy_init_compat

static inline void mbedtls_entropy_free_compat(mbedtls_entropy_context *ctx)
{
    (void)ctx;
}
#define mbedtls_entropy_free mbedtls_entropy_free_compat

static inline int mbedtls_entropy_func_compat(void *data, unsigned char *output, size_t len)
{
    (void)data;
    esp_fill_random(output, len);
    return 0;
}
#define mbedtls_entropy_func mbedtls_entropy_func_compat

/* =========================================================================
 * 4. PK setup + EC key generation — complete PSA-based replacement
 *
 * In mbedTLS 4.x:
 * - ctx_alloc_func is NULL for all pk_info_t entries (no sub-context alloc)
 * - EC signing uses pk->priv_id (PSA key ID), NOT pk->pk_ctx (ecp_keypair)
 * - pk->pub_raw, pk->pub_raw_len, pk->bits, pk->ec_family must be populated
 *
 * libpeer dtls_srtp.c does:
 *   mbedtls_pk_setup(&pkey, mbedtls_pk_info_from_type(MBEDTLS_PK_ECKEY));
 *   mbedtls_ecp_gen_key(SECP256R1, mbedtls_pk_ec(pkey), f_rng, p_rng);
 *
 * We override mbedtls_ecp_gen_key to generate via PSA and populate the PK
 * context properly. We save the PK context pointer from pk_setup so
 * ecp_gen_key_compat can find it.
 * ========================================================================= */
#include <mbedtls/pk.h>
#include <stdlib.h>
#include <psa/crypto.h>

#if defined(MBEDTLS_RSA_C)
#include <mbedtls/private/rsa.h>
#endif
#if defined(MBEDTLS_ECP_C)
#include <mbedtls/private/ecp.h>
#endif

/*
 * Thread-local-ish pointer: pk_setup saves the PK context here,
 * ecp_gen_key reads it. Only used within dtls_srtp_selfsign_cert()
 * which runs single-threaded per connection.
 */
static mbedtls_pk_context *_compat_last_pk_ctx = NULL;

static inline int mbedtls_pk_setup_compat(mbedtls_pk_context *ctx,
                                          const mbedtls_pk_info_t *info)
{
    int ret = mbedtls_pk_setup(ctx, info);
    if (ret != 0) return ret;

    mbedtls_pk_type_t type = mbedtls_pk_get_type(ctx);

    /* For EC keys: just save the context pointer; key gen will use PSA.
     * No need to allocate pk_ctx — signing uses priv_id in v4. */
    if (type == MBEDTLS_PK_ECKEY || type == MBEDTLS_PK_ECKEY_DH ||
        type == MBEDTLS_PK_ECDSA) {
        _compat_last_pk_ctx = ctx;
        return 0;
    }

#if defined(MBEDTLS_RSA_C)
    /* RSA still needs pk_ctx for mbedtls_pk_rsa() / mbedtls_rsa_gen_key() */
    if (type == MBEDTLS_PK_RSA && ctx->MBEDTLS_PRIVATE(pk_ctx) == NULL) {
        mbedtls_rsa_context *rsa = calloc(1, sizeof(mbedtls_rsa_context));
        if (!rsa) return MBEDTLS_ERR_PK_ALLOC_FAILED;
        mbedtls_rsa_init(rsa);
        ctx->MBEDTLS_PRIVATE(pk_ctx) = rsa;
    }
#endif

    return 0;
}
#define mbedtls_pk_setup mbedtls_pk_setup_compat

/*
 * mbedtls_ecp_gen_key() compat for mbedTLS 4.x:
 *
 * Old API: mbedtls_ecp_gen_key(grp_id, keypair, f_rng, p_rng)
 *   - Populated ecp_keypair struct directly
 *
 * mbedTLS 4.x: EC signing uses PSA opaque keys (pk->priv_id).
 * We generate the key via psa_generate_key() and populate the PK context
 * fields that the v4 sign/verify functions expect.
 *
 * The keypair argument (from mbedtls_pk_ec()) is ignored — we use
 * _compat_last_pk_ctx instead.
 */

/* Map mbedtls_ecp_group_id to PSA ECC family */
static inline psa_ecc_family_t _ecp_grp_to_psa_family(mbedtls_ecp_group_id grp_id)
{
    switch (grp_id) {
        case MBEDTLS_ECP_DP_SECP256R1:
        case MBEDTLS_ECP_DP_SECP384R1:
        case MBEDTLS_ECP_DP_SECP521R1:
        case MBEDTLS_ECP_DP_SECP192R1:
            return PSA_ECC_FAMILY_SECP_R1;
        case MBEDTLS_ECP_DP_BP256R1:
        case MBEDTLS_ECP_DP_BP384R1:
        case MBEDTLS_ECP_DP_BP512R1:
            return PSA_ECC_FAMILY_BRAINPOOL_P_R1;
        case MBEDTLS_ECP_DP_CURVE25519:
            return PSA_ECC_FAMILY_MONTGOMERY;
        case MBEDTLS_ECP_DP_CURVE448:
            return PSA_ECC_FAMILY_MONTGOMERY;
        default:
            return 0;
    }
}

/* Map mbedtls_ecp_group_id to key bit size */
static inline size_t _ecp_grp_to_bits(mbedtls_ecp_group_id grp_id)
{
    switch (grp_id) {
        case MBEDTLS_ECP_DP_SECP192R1: return 192;
        case MBEDTLS_ECP_DP_SECP256R1: return 256;
        case MBEDTLS_ECP_DP_SECP384R1: return 384;
        case MBEDTLS_ECP_DP_SECP521R1: return 521;
        case MBEDTLS_ECP_DP_BP256R1:   return 256;
        case MBEDTLS_ECP_DP_BP384R1:   return 384;
        case MBEDTLS_ECP_DP_BP512R1:   return 512;
        case MBEDTLS_ECP_DP_CURVE25519: return 255;
        case MBEDTLS_ECP_DP_CURVE448:  return 448;
        default: return 0;
    }
}

static inline int mbedtls_ecp_gen_key_compat(
    mbedtls_ecp_group_id grp_id,
    mbedtls_ecp_keypair *key,  /* ignored in v4 — we use _compat_last_pk_ctx */
    int (*f_rng)(void *, unsigned char *, size_t),
    void *p_rng)
{
    (void)key;
    (void)f_rng;
    (void)p_rng;

    mbedtls_pk_context *pk = _compat_last_pk_ctx;
    if (!pk) return MBEDTLS_ERR_PK_BAD_INPUT_DATA;

    psa_ecc_family_t family = _ecp_grp_to_psa_family(grp_id);
    size_t bits = _ecp_grp_to_bits(grp_id);
    if (family == 0 || bits == 0) return MBEDTLS_ERR_ECP_FEATURE_UNAVAILABLE;

    /* Generate EC key pair via PSA */
    psa_key_attributes_t attributes = PSA_KEY_ATTRIBUTES_INIT;
    psa_set_key_type(&attributes, PSA_KEY_TYPE_ECC_KEY_PAIR(family));
    psa_set_key_bits(&attributes, bits);
    psa_set_key_usage_flags(&attributes,
        PSA_KEY_USAGE_SIGN_HASH | PSA_KEY_USAGE_SIGN_MESSAGE |
        PSA_KEY_USAGE_EXPORT);
    psa_set_key_algorithm(&attributes,
        PSA_ALG_ECDSA(PSA_ALG_ANY_HASH));

    mbedtls_svc_key_id_t key_id = MBEDTLS_SVC_KEY_ID_INIT;
    psa_status_t status = psa_generate_key(&attributes, &key_id);
    psa_reset_key_attributes(&attributes);

    if (status != PSA_SUCCESS) {
        return MBEDTLS_ERR_ECP_BAD_INPUT_DATA;
    }

    /* Populate PK context fields for mbedTLS 4.x sign/verify */
    pk->MBEDTLS_PRIVATE(priv_id) = key_id;
    pk->MBEDTLS_PRIVATE(bits) = bits;
#if defined(PSA_WANT_KEY_TYPE_ECC_PUBLIC_KEY)
    pk->MBEDTLS_PRIVATE(ec_family) = family;
#endif

    /* Export public key into pk->pub_raw for fingerprint/verify operations */
    size_t pub_len = 0;
    status = psa_export_public_key(key_id,
        pk->MBEDTLS_PRIVATE(pub_raw),
        sizeof(pk->MBEDTLS_PRIVATE(pub_raw)),
        &pub_len);
    if (status != PSA_SUCCESS) {
        psa_destroy_key(key_id);
        pk->MBEDTLS_PRIVATE(priv_id) = MBEDTLS_SVC_KEY_ID_INIT;
        return MBEDTLS_ERR_ECP_BAD_INPUT_DATA;
    }
    pk->MBEDTLS_PRIVATE(pub_raw_len) = pub_len;

    _compat_last_pk_ctx = NULL;  /* consumed */
    return 0;
}
#define mbedtls_ecp_gen_key mbedtls_ecp_gen_key_compat

/* =========================================================================
 * 5. mbedtls_x509write_crt_pem() — 5 args → 3 args in v4
 * ========================================================================= */
#include <mbedtls/x509_crt.h>

static inline int mbedtls_x509write_crt_pem_compat(
    mbedtls_x509write_cert *crt,
    unsigned char *buf, size_t size,
    int (*f_rng)(void *, unsigned char *, size_t),
    void *p_rng)
{
    (void)f_rng;
    (void)p_rng;
    return mbedtls_x509write_crt_pem(crt, buf, size);
}
#define mbedtls_x509write_crt_pem mbedtls_x509write_crt_pem_compat

#endif /* MBEDTLS_V4_COMPAT_H */
