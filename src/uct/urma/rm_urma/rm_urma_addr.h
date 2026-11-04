/**
 * Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
 * See file LICENSE for terms.
 */

#ifndef UCT_RM_URMA_ADDR_H
#define UCT_RM_URMA_ADDR_H

#include <ucs/type/status.h>
#include <stddef.h>
#include <stdint.h>
#include <endian.h>
#include <string.h>

#define UCT_RM_URMA_IFACE_ADDR_V2_SIZE 48
#define UCT_RM_URMA_CTRL_HDR_V2_SIZE   32
#define UCT_RM_URMA_RETURN_DEV_SIZE    20
#define UCT_RM_URMA_FC_REQ_V2_SIZE    100
#define UCT_RM_URMA_ADDR_VERSION       2
#define UCT_RM_URMA_ADDR_MODE_RM       1
#define UCT_RM_URMA_ADDR_PEER_FC       1

typedef enum {
    UCT_RM_URMA_CTRL_REQUEST = 1,
    UCT_RM_URMA_CTRL_GRANT   = 2
} uct_rm_urma_ctrl_kind_t;

/* Host representation only. Padding and process pointers never go on wire. */
typedef struct uct_rm_urma_iface_addr_v2 {
    uint64_t segment_va;
    uint64_t segment_length;
    uint32_t jetty_id;
    uint32_t segment_flags;
    uint32_t segment_token;
    uint32_t rx_depth;
    uint16_t max_peers;
    uint16_t initial_credit;
    uint64_t instance;
} uct_rm_urma_iface_addr_v2_t;

typedef struct uct_rm_urma_ctrl_v2 {
    uct_rm_urma_ctrl_kind_t kind;
    uint32_t credits;
    uint64_t requester_instance;
    uint64_t responder_instance;
    uint64_t request_id;
} uct_rm_urma_ctrl_v2_t;

typedef struct uct_rm_urma_return_addr {
    uint8_t eid[16];
    uint32_t uasid;
    uct_rm_urma_iface_addr_v2_t iface;
} uct_rm_urma_return_addr_t;

/* UB bodies may be unaligned. Share byte-order helpers with reliable SEND. */
static inline void uct_rm_urma_addr_put32(void *buffer, size_t offset, uint32_t value)
{
    value = htole32(value);
    memcpy((uint8_t *)buffer + offset, &value, sizeof(value));
}

static inline void uct_rm_urma_addr_put64(void *buffer, size_t offset, uint64_t value)
{
    value = htole64(value);
    memcpy((uint8_t *)buffer + offset, &value, sizeof(value));
}

static inline uint32_t uct_rm_urma_addr_get32(const void *buffer, size_t offset)
{
    uint32_t value;

    memcpy(&value, (const uint8_t *)buffer + offset, sizeof(value));
    return le32toh(value);
}

static inline uint64_t uct_rm_urma_addr_get64(const void *buffer, size_t offset)
{
    uint64_t value;

    memcpy(&value, (const uint8_t *)buffer + offset, sizeof(value));
    return le64toh(value);
}

/* These routines do not allocate, import targets, or change credit state.
 * A decoder publishes output only after the whole message is validated. */
ucs_status_t
uct_rm_urma_iface_addr_v2_pack(void *buffer, size_t capacity,
                              const uct_rm_urma_iface_addr_v2_t *address);
ucs_status_t
uct_rm_urma_iface_addr_v2_unpack(const void *buffer, size_t length,
                                uct_rm_urma_iface_addr_v2_t *address);
ucs_status_t
uct_rm_urma_ctrl_v2_pack(void *buffer, size_t capacity,
                        const uct_rm_urma_ctrl_v2_t *control,
                        const uct_rm_urma_return_addr_t *return_address);
ucs_status_t
uct_rm_urma_ctrl_v2_unpack(const void *buffer, size_t length,
                          uint64_t local_instance,
                          uct_rm_urma_ctrl_v2_t *control,
                          uct_rm_urma_return_addr_t *return_address);

/* Reliable mode uses the same address layout with distinct version/flags. */
ucs_status_t uct_rm_urma_reliable_address_pack(void *buffer, size_t capacity,
                      const uct_rm_urma_iface_addr_v2_t *address);
ucs_status_t uct_rm_urma_reliable_address_unpack(const void *buffer, size_t length,
                      uct_rm_urma_iface_addr_v2_t *address);

#endif
