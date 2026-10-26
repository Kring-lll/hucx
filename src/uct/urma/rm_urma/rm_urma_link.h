/**
 * Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
 * See file LICENSE for terms.
 */
#ifndef UCT_RM_URMA_LINK_H
#define UCT_RM_URMA_LINK_H

#include "rm_urma_iface.h"

struct uct_rm_urma_peer {
    ucs_list_link_t list;
    ucs_list_link_t eps;
    uct_rm_urma_return_addr_t remote;
    uct_rm_urma_target_t *target;
    urma_target_seg_t *segment;
    uint32_t credits;
    uint32_t hard_threshold;
    uint32_t spent;
    uint32_t requested;
    uint64_t active_id;
    uint64_t received_id;
    int request_posted;
    int grant_pending;
};

struct uct_rm_urma_link_req {
    ucs_list_link_t list;
    uct_urma_iface_send_op_t op;
    uct_rm_urma_peer_t *peer;
    uct_rm_urma_target_t *target;
    uct_ub_iface_send_desc_t *desc;
    uint64_t id;
    uint32_t credits;
    unsigned kind;
    unsigned length;
    unsigned char payload[UCT_RM_URMA_FC_REQ_V2_SIZE];
};

void uct_rm_urma_link_init(uct_rm_urma_iface_t *iface,
                          const uct_rm_urma_iface_config_t *config);
ucs_status_t uct_rm_urma_link_ready(uct_rm_urma_iface_t *iface);
ucs_status_t uct_rm_urma_link_connect(uct_rm_urma_ep_t *ep,
                                     const uct_device_addr_t *device,
                                     const uct_iface_addr_t *address);
ucs_status_t uct_rm_urma_link_get_address(uct_iface_h tl_iface,
                                         uct_iface_addr_t *address);
ucs_status_t uct_rm_urma_link_check(uct_rm_urma_ep_t *ep);
void uct_rm_urma_link_sent(uct_rm_urma_ep_t *ep);
int uct_rm_urma_link_can_send(uct_rm_urma_ep_t *ep);
ucs_status_t uct_rm_urma_link_receive(uct_rm_urma_iface_t *iface,
                                     const void *buffer, size_t length);
void uct_rm_urma_link_progress(uct_rm_urma_iface_t *iface);
void uct_rm_urma_link_cleanup(uct_rm_urma_iface_t *iface);
void uct_rm_urma_link_complete(uct_urma_iface_send_op_t *op, const void *response);
void uct_rm_urma_link_account_rx(uct_rm_urma_iface_t *iface,
                                unsigned data, unsigned control,
                                unsigned posted);
ucs_status_t uct_rm_urma_ep_link_post(uct_rm_urma_iface_t *iface,
                                     uct_rm_urma_link_req_t *req);
#endif
