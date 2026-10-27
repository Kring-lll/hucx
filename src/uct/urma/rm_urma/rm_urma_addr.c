/**
 * Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
 * See file LICENSE for terms.
 */

#include "rm_urma_addr.h"

#include <endian.h>
#include <string.h>

/* The UB header leaves the following control body unaligned. */
static void uct_rm_urma_addr_put16(void *buffer, size_t offset, uint16_t value)
{
    value = htole16(value);
    memcpy((uint8_t *)buffer + offset, &value, sizeof(value));
}

static void uct_rm_urma_addr_put32(void *buffer, size_t offset, uint32_t value)
{
    value = htole32(value);
    memcpy((uint8_t *)buffer + offset, &value, sizeof(value));
}

static void uct_rm_urma_addr_put64(void *buffer, size_t offset, uint64_t value)
{
    value = htole64(value);
    memcpy((uint8_t *)buffer + offset, &value, sizeof(value));
}

static uint16_t uct_rm_urma_addr_get16(const void *buffer, size_t offset)
{
    uint16_t value;

    memcpy(&value, (const uint8_t *)buffer + offset, sizeof(value));
    return le16toh(value);
}

static uint32_t uct_rm_urma_addr_get32(const void *buffer, size_t offset)
{
    uint32_t value;

    memcpy(&value, (const uint8_t *)buffer + offset, sizeof(value));
    return le32toh(value);
}

static uint64_t uct_rm_urma_addr_get64(const void *buffer, size_t offset)
{
    uint64_t value;

    memcpy(&value, (const uint8_t *)buffer + offset, sizeof(value));
    return le64toh(value);
}

static ucs_status_t
uct_rm_urma_iface_addr_v2_validate(const uct_rm_urma_iface_addr_v2_t *address)
{
    uint64_t required;

    if ((address->instance == 0) || (address->max_peers == 0) ||
        (address->initial_credit == 0)) {
        return UCS_ERR_INVALID_PARAM;
    }

    /* K covers the union of incoming and outgoing peers, not EP count.
     * Use a wide intermediate: the boundary values exceed a 32-bit sum. */
    required = (uint64_t)address->max_peers * address->initial_credit;
    required += 2ull * address->max_peers + 2;
    return (required <= address->rx_depth) ? UCS_OK : UCS_ERR_INVALID_PARAM;
}

ucs_status_t
uct_rm_urma_iface_addr_v2_pack(void *buffer, size_t capacity,
                              const uct_rm_urma_iface_addr_v2_t *address)
{
    uint8_t wire[UCT_RM_URMA_IFACE_ADDR_V2_SIZE] = {0};
    ucs_status_t status;

    if ((buffer == NULL) || (address == NULL) || (capacity < sizeof(wire))) {
        return UCS_ERR_INVALID_PARAM;
    }
    status = uct_rm_urma_iface_addr_v2_validate(address);
    if (status != UCS_OK) {
        return status;
    }

    uct_rm_urma_addr_put64(wire, 0, address->segment_va);
    uct_rm_urma_addr_put64(wire, 8, address->segment_length);
    uct_rm_urma_addr_put32(wire, 16, address->jetty_id);
    uct_rm_urma_addr_put32(wire, 20, address->segment_flags);
    uct_rm_urma_addr_put32(wire, 24, address->segment_token);
    wire[28] = UCT_RM_URMA_ADDR_VERSION;
    wire[29] = UCT_RM_URMA_ADDR_MODE_RM;
    uct_rm_urma_addr_put16(wire, 30, UCT_RM_URMA_ADDR_PEER_FC);
    uct_rm_urma_addr_put32(wire, 32, address->rx_depth);
    uct_rm_urma_addr_put16(wire, 36, address->max_peers);
    uct_rm_urma_addr_put16(wire, 38, address->initial_credit);
    uct_rm_urma_addr_put64(wire, 40, address->instance);
    memcpy(buffer, wire, sizeof(wire));
    return UCS_OK;
}

ucs_status_t
uct_rm_urma_iface_addr_v2_unpack(const void *buffer, size_t length,
                                uct_rm_urma_iface_addr_v2_t *address)
{
    const uint8_t *wire = buffer;
    uct_rm_urma_iface_addr_v2_t candidate = {0};
    ucs_status_t status;

    if ((buffer == NULL) || (address == NULL) ||
        (length != UCT_RM_URMA_IFACE_ADDR_V2_SIZE)) {
        return UCS_ERR_INVALID_PARAM;
    }
    if ((wire[28] != UCT_RM_URMA_ADDR_VERSION) ||
        (wire[29] != UCT_RM_URMA_ADDR_MODE_RM) ||
        (uct_rm_urma_addr_get16(wire, 30) != UCT_RM_URMA_ADDR_PEER_FC)) {
        return UCS_ERR_UNSUPPORTED;
    }

    candidate.segment_va     = uct_rm_urma_addr_get64(wire, 0);
    candidate.segment_length = uct_rm_urma_addr_get64(wire, 8);
    candidate.jetty_id       = uct_rm_urma_addr_get32(wire, 16);
    candidate.segment_flags  = uct_rm_urma_addr_get32(wire, 20);
    candidate.segment_token  = uct_rm_urma_addr_get32(wire, 24);
    candidate.rx_depth       = uct_rm_urma_addr_get32(wire, 32);
    candidate.max_peers      = uct_rm_urma_addr_get16(wire, 36);
    candidate.initial_credit = uct_rm_urma_addr_get16(wire, 38);
    candidate.instance       = uct_rm_urma_addr_get64(wire, 40);
    status = uct_rm_urma_iface_addr_v2_validate(&candidate);
    if (status != UCS_OK) {
        return status;
    }
    *address = candidate;
    return UCS_OK;
}

static size_t uct_rm_urma_ctrl_v2_length(uct_rm_urma_ctrl_kind_t kind)
{
    if (kind == UCT_RM_URMA_CTRL_REQUEST) {
        return UCT_RM_URMA_FC_REQ_V2_SIZE;
    }
    return (kind == UCT_RM_URMA_CTRL_GRANT) ? UCT_RM_URMA_CTRL_HDR_V2_SIZE : 0;
}

ucs_status_t
uct_rm_urma_ctrl_v2_pack(void *buffer, size_t capacity,
                        const uct_rm_urma_ctrl_v2_t *control,
                        const uct_rm_urma_return_addr_t *return_address)
{
    uint8_t wire[UCT_RM_URMA_FC_REQ_V2_SIZE] = {0};
    size_t length;
    ucs_status_t status;

    if ((buffer == NULL) || (control == NULL)) {
        return UCS_ERR_INVALID_PARAM;
    }
    length = uct_rm_urma_ctrl_v2_length(control->kind);
    if ((length == 0) || (capacity < length) || (control->credits == 0) ||
        (control->request_id == 0) || (control->requester_instance == 0) ||
        (control->responder_instance == 0)) {
        return UCS_ERR_INVALID_PARAM;
    }
    if (control->kind == UCT_RM_URMA_CTRL_REQUEST) {
        if ((return_address == NULL) ||
            (return_address->iface.instance != control->requester_instance)) {
            return UCS_ERR_INVALID_PARAM;
        }
        status = uct_rm_urma_iface_addr_v2_pack(wire + 52, 48,
                                               &return_address->iface);
        if (status != UCS_OK) {
            return status;
        }
        memcpy(wire + 32, return_address->eid, sizeof(return_address->eid));
        uct_rm_urma_addr_put32(wire, 48, return_address->uasid);
    }

    wire[0] = UCT_RM_URMA_ADDR_VERSION;
    wire[1] = control->kind;
    uct_rm_urma_addr_put16(wire, 2, length);
    uct_rm_urma_addr_put32(wire, 4, control->credits);
    uct_rm_urma_addr_put64(wire, 8, control->requester_instance);
    uct_rm_urma_addr_put64(wire, 16, control->responder_instance);
    uct_rm_urma_addr_put64(wire, 24, control->request_id);
    memcpy(buffer, wire, length);
    return UCS_OK;
}

ucs_status_t
uct_rm_urma_ctrl_v2_unpack(const void *buffer, size_t length,
                          uint64_t local_instance,
                          uct_rm_urma_ctrl_v2_t *control,
                          uct_rm_urma_return_addr_t *return_address)
{
    const uint8_t *wire = buffer;
    uct_rm_urma_ctrl_v2_t candidate = {0};
    uct_rm_urma_return_addr_t return_candidate = {0};
    uint64_t recipient;
    size_t expected;
    ucs_status_t status;

    if ((buffer == NULL) || (control == NULL) || (local_instance == 0) ||
        (length < UCT_RM_URMA_CTRL_HDR_V2_SIZE)) {
        return UCS_ERR_INVALID_PARAM;
    }
    if (wire[0] != UCT_RM_URMA_ADDR_VERSION) {
        return UCS_ERR_UNSUPPORTED;
    }
    candidate.kind = wire[1];
    expected = uct_rm_urma_ctrl_v2_length(candidate.kind);
    if ((expected == 0) || (length != expected) ||
        (uct_rm_urma_addr_get16(wire, 2) != expected)) {
        return UCS_ERR_INVALID_PARAM;
    }

    candidate.credits            = uct_rm_urma_addr_get32(wire, 4);
    candidate.requester_instance = uct_rm_urma_addr_get64(wire, 8);
    candidate.responder_instance = uct_rm_urma_addr_get64(wire, 16);
    candidate.request_id         = uct_rm_urma_addr_get64(wire, 24);
    if ((candidate.credits == 0) || (candidate.request_id == 0) ||
        (candidate.requester_instance == 0) ||
        (candidate.responder_instance == 0)) {
        return UCS_ERR_INVALID_PARAM;
    }
    recipient = (candidate.kind == UCT_RM_URMA_CTRL_REQUEST) ?
                candidate.responder_instance : candidate.requester_instance;
    if (recipient != local_instance) {
        return UCS_ERR_NO_ELEM;
    }

    if (candidate.kind == UCT_RM_URMA_CTRL_REQUEST) {
        if (return_address == NULL) {
            return UCS_ERR_INVALID_PARAM;
        }
        status = uct_rm_urma_iface_addr_v2_unpack(wire + 52, 48,
                                                 &return_candidate.iface);
        if (status != UCS_OK) {
            return status;
        }
        if (return_candidate.iface.instance != candidate.requester_instance) {
            return UCS_ERR_INVALID_PARAM;
        }
        memcpy(return_candidate.eid, wire + 32, sizeof(return_candidate.eid));
        return_candidate.uasid = uct_rm_urma_addr_get32(wire, 48);
        *return_address = return_candidate;
    }
    *control = candidate;
    return UCS_OK;
}
