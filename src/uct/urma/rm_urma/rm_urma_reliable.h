/**
 * Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
 * See file LICENSE for terms.
 */
#ifndef UCT_RM_URMA_RELIABLE_H
#define UCT_RM_URMA_RELIABLE_H

#include "rm_urma_link.h"
#include <ucs/time/time.h>

/* Wire bodies follow the existing, packed three-byte UB AM header. */
#define UCT_RM_URMA_REL_PREFIX 36u
#define UCT_RM_URMA_REL_HEADER 104u
#define UCT_RM_URMA_REL_DATA 1u
#define UCT_RM_URMA_REL_ACK 2u

typedef struct uct_rm_urma_replay {
    ucs_list_link_t list;
    uct_urma_iface_send_op_t op;
    uct_rm_urma_peer_t *peer;
    uct_rm_urma_ep_t *owner;
    uct_ub_iface_send_desc_t *desc;
    uint64_t psn;
    ucs_time_t posted_at;
    ucs_time_t accepted_at;
    unsigned length;
    unsigned retries;
    unsigned in_flight;
    unsigned acknowledged;
    unsigned kind;
    unsigned quarantined;
} uct_rm_urma_replay_t;

void uct_rm_urma_reliable_peer_init(uct_rm_urma_peer_t *peer);
int uct_rm_urma_reliable_can_send(uct_rm_urma_ep_t *ep);
ucs_status_t uct_rm_urma_reliable_short(uct_rm_urma_ep_t *ep, uint8_t id,
                      uint64_t header, const void *buffer, unsigned length);
ssize_t uct_rm_urma_reliable_bcopy(uct_rm_urma_ep_t *ep, uint8_t id,
                      uct_pack_callback_t pack, void *arg);
ucs_status_t uct_rm_urma_reliable_receive(uct_rm_urma_iface_t *iface,
                      const void *buffer, size_t length);
void uct_rm_urma_reliable_progress(uct_rm_urma_iface_t *iface, ucs_time_t now);
void uct_rm_urma_reliable_complete(uct_urma_iface_send_op_t *op,
                      const void *response);
/* Called only after deleting the Jetty AND purging the control EP CQ ops. */
void uct_rm_urma_reliable_cleanup(uct_rm_urma_iface_t *iface);
ucs_status_t uct_rm_urma_ep_reliable_post(uct_rm_urma_iface_t *iface,
                      uct_rm_urma_replay_t *entry);
#endif
