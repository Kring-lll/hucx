/* Execute the actual shared MD code; URMA calls are CPU mocks, not device proof. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../../../src/uct/urma/base/urma_md.h"

static unsigned fail_alloc;
static unsigned unimports;
static void *test_calloc(size_t count, size_t size, const char *name);
#define ucs_calloc test_calloc
#include "../../../src/uct/urma/base/urma_md.c"
#undef ucs_calloc

static void *test_calloc(size_t count, size_t size, const char *name)
{
    if (fail_alloc) {
        --fail_alloc;
        return NULL;
    }
    return ucs_calloc(count, size, name);
}

urma_status_t urma_init(urma_init_attr_t *attr)
{
    return URMA_SUCCESS;
}

urma_status_t urma_uninit(void)
{
    return URMA_SUCCESS;
}

urma_status_t urma_unimport_seg(urma_target_seg_t *segment)
{
    assert(segment != NULL);
    ++unimports;
    return URMA_SUCCESS;
}

static uct_ub_seg_info_t packed_key(void)
{
    uct_ub_seg_info_t value = {0};
    value.eid.raw[0] = 1;
    value.eid.raw[15] = 2;
    value.uasid = 3;
    value.seg_va = UINT64_C(0x1234567800001000);
    value.seg_len = 4096;
    value.seg_token_id = 7;
    return value;
}

static uct_rkey_t unpack(const uct_ub_seg_info_t *packed)
{
    uct_rkey_t key = 0;
    void *handle = (void *)1;
    assert(uct_ub_rkey_unpack(NULL, packed, &key, &handle) == UCS_OK);
    assert(key != 0 && handle == NULL);
    return key;
}

static void release(uct_rkey_t key)
{
    assert(uct_ub_rkey_release(NULL, key, NULL) == UCS_OK);
}

static void test_unused(void)
{
    uct_ub_seg_info_t packed = packed_key();
    release(unpack(&packed));
    assert(kh_size(&g_uct_ub_rkey_hash) == 0);
    assert(unimports == 0);
}

static void test_identity(int eid)
{
    uct_ub_seg_info_t a = packed_key();
    uct_ub_seg_info_t b = a;
    uct_rkey_t first;
    uct_rkey_t second;
    if (eid) {
        b.eid.raw[0] ^= 0x80;
    } else {
        b.seg_va += UINT64_C(0x100000000);
    }
    first = unpack(&a);
    second = unpack(&b);
    assert(first != second);
    assert(kh_size(&g_uct_ub_rkey_hash) == 2);
    release(first);
    release(second);
    assert(kh_size(&g_uct_ub_rkey_hash) == 0);
}

static void test_allocation(void)
{
    uct_ub_seg_info_t packed = packed_key();
    uct_rkey_t key = 0;
    void *handle = NULL;
    fail_alloc = 1;
    assert(uct_ub_rkey_unpack(NULL, &packed, &key, &handle) == UCS_ERR_NO_MEMORY);
    assert(kh_size(&g_uct_ub_rkey_hash) == 0);
    release(unpack(&packed));
    assert(kh_size(&g_uct_ub_rkey_hash) == 0);
}

static void test_shared(void)
{
    uct_ub_seg_info_t packed = packed_key();
    urma_target_seg_t segment = {0};
    uct_rkey_t first = unpack(&packed);
    uct_rkey_t second = unpack(&packed);
    uct_ub_rkey_info_t *info = (uct_ub_rkey_info_t *)first;
    assert(first == second && info->ref_cnt == 2);
    info->import_tseg = &segment;
    release(first);
    assert(info->ref_cnt == 1 && unimports == 0);
    release(second);
    assert(kh_size(&g_uct_ub_rkey_hash) == 0 && unimports == 1);
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    assert(sizeof(uct_ub_seg_info_t) == 44);
    if (!strcmp(argv[1], "unused")) {
        test_unused();
    } else if (!strcmp(argv[1], "address")) {
        test_identity(0);
    } else if (!strcmp(argv[1], "eid")) {
        test_identity(1);
    } else if (!strcmp(argv[1], "allocation")) {
        test_allocation();
    } else if (!strcmp(argv[1], "shared")) {
        test_shared();
    } else {
        return 2;
    }
    printf("RKEY_CPU_CASE=%s PASS PACKED_BYTES=44 DEVICE_RMA=NOT_TESTED\n", argv[1]);
    return 0;
}
