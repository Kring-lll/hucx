/* Compile the actual RM wire codec; this is not a provider or network mock. */
#include <uct/urma/rm_urma/rm_urma_addr.h>

#include <assert.h>
#include <stdio.h>
#include <string.h>

static uct_rm_urma_iface_addr_v2_t test_address(void)
{
    uct_rm_urma_iface_addr_v2_t address = {0};

    address.segment_va     = UINT64_C(0x0102030405060708);
    address.segment_length = 64;
    address.jetty_id       = 7;
    address.segment_token  = 1;
    address.rx_depth       = 64;
    address.max_peers      = 4;
    address.initial_credit = 8;
    address.instance       = 11;
    return address;
}

static uct_rm_urma_ctrl_v2_t test_control(uct_rm_urma_ctrl_kind_t kind)
{
    uct_rm_urma_ctrl_v2_t control = {0};

    control.kind               = kind;
    control.credits            = 6;
    control.requester_instance = 11;
    control.responder_instance = 22;
    control.request_id         = 99;
    return control;
}

static void test_golden_and_unaligned(void)
{
    /* Independently derived from the specified byte layout. */
    const unsigned char golden[48] = {
        8,7,6,5,4,3,2,1, 64,0,0,0,0,0,0,0,
        7,0,0,0, 0,0,0,0, 1,0,0,0, 2,1,1,0,
        64,0,0,0, 4,0,8,0, 11,0,0,0,0,0,0,0
    };
    unsigned char storage[55];
    uct_rm_urma_iface_addr_v2_t input = test_address();
    uct_rm_urma_iface_addr_v2_t output;
    size_t offset;

    for (offset = 0; offset < 7; ++offset) {
        memset(storage, 0xa5, sizeof(storage));
        assert(uct_rm_urma_iface_addr_v2_pack(storage + offset, 48,
                                             &input) == UCS_OK);
        assert(memcmp(storage + offset, golden, 48) == 0);
        assert(uct_rm_urma_iface_addr_v2_unpack(storage + offset, 48,
                                               &output) == UCS_OK);
        assert(output.segment_va == input.segment_va);
        assert(output.instance == input.instance);
        assert(output.max_peers == input.max_peers);
        assert(storage[offset + 48] == 0xa5);
    }
}

static void test_invalid_address_does_not_publish(void)
{
    unsigned char wire[49];
    unsigned char changed[49];
    uct_rm_urma_iface_addr_v2_t input = test_address();
    uct_rm_urma_iface_addr_v2_t output;
    uct_rm_urma_iface_addr_v2_t sentinel;
    const size_t fields[] = {28, 29, 30, 31, 32, 36, 38, 40};
    size_t length;
    size_t i;

    assert(uct_rm_urma_iface_addr_v2_pack(wire, 48, &input) == UCS_OK);
    memset(&sentinel, 0xa5, sizeof(sentinel));
    for (length = 0; length <= 49; ++length) {
        if (length == 48) {
            continue;
        }
        output = sentinel;
        assert(uct_rm_urma_iface_addr_v2_unpack(wire, length,
                                               &output) != UCS_OK);
        assert(memcmp(&output, &sentinel, sizeof(output)) == 0);
    }
    for (i = 0; i < sizeof(fields) / sizeof(fields[0]); ++i) {
        memcpy(changed, wire, sizeof(wire));
        if (fields[i] == 31) {
            changed[31] = 1;
        } else {
            changed[fields[i]] = 0;
        }
        output = sentinel;
        assert(uct_rm_urma_iface_addr_v2_unpack(changed, 48,
                                               &output) != UCS_OK);
        assert(memcmp(&output, &sentinel, sizeof(output)) == 0);
    }
}

static void test_budget_overflow_and_pack_atomicity(void)
{
    unsigned char wire[48];
    unsigned char sentinel[48];
    uct_rm_urma_iface_addr_v2_t input = test_address();

    memset(wire, 0xa5, sizeof(wire));
    memcpy(sentinel, wire, sizeof(wire));
    input.rx_depth = UINT32_MAX;
    input.max_peers = UINT16_MAX;
    input.initial_credit = UINT16_MAX;
    assert(uct_rm_urma_iface_addr_v2_pack(wire, sizeof(wire),
                                         &input) != UCS_OK);
    assert(memcmp(wire, sentinel, sizeof(wire)) == 0);
    --input.initial_credit;
    assert(uct_rm_urma_iface_addr_v2_pack(wire, sizeof(wire),
                                         &input) == UCS_OK);
    input = test_address();
    memcpy(wire, sentinel, sizeof(wire));
    assert(uct_rm_urma_iface_addr_v2_pack(wire, sizeof(wire) - 1,
                                         &input) != UCS_OK);
    assert(memcmp(wire, sentinel, sizeof(wire)) == 0);
}

static void test_control_golden_and_return_identity(void)
{
    const unsigned char grant_golden[32] = {
        2,2,32,0, 6,0,0,0, 11,0,0,0,0,0,0,0,
        22,0,0,0,0,0,0,0, 99,0,0,0,0,0,0,0
    };
    unsigned char storage[104] = {0};
    unsigned char *wire = storage + 3;
    uct_rm_urma_return_addr_t address = {0};
    uct_rm_urma_return_addr_t output_address;
    uct_rm_urma_ctrl_v2_t input = test_control(UCT_RM_URMA_CTRL_GRANT);
    uct_rm_urma_ctrl_v2_t output;
    unsigned i;

    assert(uct_rm_urma_ctrl_v2_pack(wire, 100, &input, NULL) == UCS_OK);
    assert(memcmp(wire, grant_golden, sizeof(grant_golden)) == 0);
    assert(uct_rm_urma_ctrl_v2_unpack(wire, 32, 11,
                                     &output, NULL) == UCS_OK);
    assert(output.request_id == 99);
    assert(output.responder_instance == 22);

    input.kind = UCT_RM_URMA_CTRL_REQUEST;
    address.iface = test_address();
    address.uasid = UINT32_C(0x01020304);
    for (i = 0; i < sizeof(address.eid); ++i) {
        address.eid[i] = i;
    }
    assert(uct_rm_urma_ctrl_v2_pack(wire, 100, &input, &address) == UCS_OK);
    assert(wire[2] == 100);
    assert(wire[48] == 4);
    assert(wire[51] == 1);
    assert(uct_rm_urma_ctrl_v2_unpack(wire, 100, 22, &output,
                                     &output_address) == UCS_OK);
    assert(output_address.uasid == address.uasid);
    assert(memcmp(output_address.eid, address.eid, 16) == 0);
    assert(output_address.iface.instance == 11);
    assert(uct_rm_urma_ctrl_v2_unpack(wire, 100, 11, &output,
                                     &output_address) == UCS_ERR_NO_ELEM);
}

static void test_malformed_control_does_not_publish(void)
{
    unsigned char wire[101] = {0};
    unsigned char changed[101];
    uct_rm_urma_ctrl_v2_t input = test_control(UCT_RM_URMA_CTRL_REQUEST);
    uct_rm_urma_ctrl_v2_t output;
    uct_rm_urma_ctrl_v2_t sentinel;
    uct_rm_urma_return_addr_t address = {0};
    uct_rm_urma_return_addr_t output_address;
    uct_rm_urma_return_addr_t address_sentinel;
    const size_t corrupt[] = {0, 1, 2, 4, 8, 16, 24, 92};
    size_t length;
    size_t i;

    address.iface = test_address();
    memset(&sentinel, 0xa5, sizeof(sentinel));
    memset(&address_sentinel, 0xa5, sizeof(address_sentinel));
    assert(uct_rm_urma_ctrl_v2_pack(wire, 100, &input, &address) == UCS_OK);
    for (length = 0; length <= 101; ++length) {
        if (length == 100) {
            continue;
        }
        output = sentinel;
        output_address = address_sentinel;
        assert(uct_rm_urma_ctrl_v2_unpack(wire, length, 22, &output,
                                         &output_address) != UCS_OK);
        assert(memcmp(&output, &sentinel, sizeof(output)) == 0);
        assert(memcmp(&output_address, &address_sentinel,
                      sizeof(output_address)) == 0);
    }
    for (i = 0; i < sizeof(corrupt) / sizeof(corrupt[0]); ++i) {
        memcpy(changed, wire, sizeof(wire));
        changed[corrupt[i]] = 0;
        output = sentinel;
        output_address = address_sentinel;
        assert(uct_rm_urma_ctrl_v2_unpack(changed, 100, 22, &output,
                                         &output_address) != UCS_OK);
        assert(memcmp(&output, &sentinel, sizeof(output)) == 0);
        assert(memcmp(&output_address, &address_sentinel,
                      sizeof(output_address)) == 0);
    }
    assert(uct_rm_urma_ctrl_v2_unpack(wire, 100, 22,
                                     &output, NULL) != UCS_OK);
}

int main(void)
{
    test_golden_and_unaligned();
    test_invalid_address_does_not_publish();
    test_budget_overflow_and_pack_atomicity();
    test_control_golden_and_return_identity();
    test_malformed_control_does_not_publish();
    puts("RM实际C地址与控制编解码：5组测试通过；未进行网络或性能验证");
    return 0;
}
