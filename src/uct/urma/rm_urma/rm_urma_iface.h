/**
* Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
*
* See file LICENSE for terms.
*/

#ifndef UCT_RM_URMA_IFACE_H
#define UCT_RM_URMA_IFACE_H

#include <uct/api/uct.h>
#include <uct/base/uct_iface.h>
#include <uct/urma/base/urma_def.h>
#include <uct/urma/base/urma_md.h>
#include <uct/urma/base/urma_iface.h>
#include <uct/base/uct_iov.inl>
#include <ucs/datastruct/ptr_array.h>
#include <ucs/datastruct/list.h>
#include <ucs/datastruct/khash.h>

#include "rm_urma_addr.h"

#include <urma_api.h>

typedef struct uct_rm_urma_iface    uct_rm_urma_iface_t;
typedef struct uct_rm_urma_peer     uct_rm_urma_peer_t;
typedef struct uct_rm_urma_link_req uct_rm_urma_link_req_t;

static UCS_F_ALWAYS_INLINE khint32_t
uct_rm_urma_peer_hash(const uct_rm_urma_return_addr_t *address)
{
    khint32_t hash = kh_int64_hash_func(address->iface.instance);
    unsigned i;

    hash ^= kh_int_hash_func(address->iface.jetty_id ^ address->uasid);
    for (i = 0; i < sizeof(address->eid); ++i) {
        hash = hash * 31 + address->eid[i];
    }
    return hash;
}

static UCS_F_ALWAYS_INLINE int
uct_rm_urma_peer_equal(const uct_rm_urma_return_addr_t *a,
                       const uct_rm_urma_return_addr_t *b)
{
    return (a->iface.instance == b->iface.instance) &&
           (a->iface.jetty_id == b->iface.jetty_id) &&
           (a->uasid == b->uasid) && !memcmp(a->eid, b->eid, sizeof(a->eid));
}

KHASH_INIT(uct_rm_urma_peers, const uct_rm_urma_return_addr_t *,
           uct_rm_urma_peer_t *, 1, uct_rm_urma_peer_hash,
           uct_rm_urma_peer_equal)

KHASH_MAP_INIT_INT64(uct_rm_urma_requests, uct_rm_urma_peer_t *)

enum {
    UCT_RM_URMA_LINK_LEGACY,
    UCT_RM_URMA_LINK_IFACE_V2
};

typedef struct uct_rm_urma_jetty {
    urma_jetty_t                    *jetty;
    int16_t                         available;
} uct_rm_urma_jetty_t;

/* A target is reusable only when every URMA import parameter matches. */
typedef struct uct_rm_urma_target_key {
    urma_rjetty_t remote;
    urma_token_t  token;
} uct_rm_urma_target_key_t;

typedef struct uct_rm_urma_target {
    uct_rm_urma_target_key_t key;
    urma_target_jetty_t      *jetty;
    unsigned                references;
} uct_rm_urma_target_t;

static UCS_F_ALWAYS_INLINE khint32_t
uct_rm_urma_target_hash(const uct_rm_urma_target_key_t *key)
{
    khint32_t hash = kh_int_hash_func(key->remote.jetty_id.id ^
                                    key->remote.jetty_id.uasid);
    unsigned i;

    for (i = 0; i < sizeof(key->remote.jetty_id.eid.raw); ++i) {
        hash = hash * 31 + key->remote.jetty_id.eid.raw[i];
    }
    return hash;
}

static UCS_F_ALWAYS_INLINE int
uct_rm_urma_target_equal(const uct_rm_urma_target_key_t *a,
                         const uct_rm_urma_target_key_t *b)
{
    /* Compare fields, not structure padding or only the low EID bits. */
    return !memcmp(a->remote.jetty_id.eid.raw, b->remote.jetty_id.eid.raw,
                   sizeof(a->remote.jetty_id.eid.raw)) &&
           (a->remote.jetty_id.uasid == b->remote.jetty_id.uasid) &&
           (a->remote.jetty_id.id == b->remote.jetty_id.id) &&
           (a->remote.trans_mode == b->remote.trans_mode) &&
           (a->remote.policy == b->remote.policy) &&
           (a->remote.type == b->remote.type) &&
           (a->remote.flag.value == b->remote.flag.value) &&
           (a->remote.tp_type == b->remote.tp_type) &&
           (a->token.token == b->token.token);
}

KHASH_INIT(uct_rm_urma_targets, const uct_rm_urma_target_key_t *,
           uct_rm_urma_target_t *, 1, uct_rm_urma_target_hash,
           uct_rm_urma_target_equal)

enum {
    UCT_RM_URMA_IFACE_STAT_RX_DL_CR_ERR,
    UCT_RM_URMA_IFACE_STAT_TX_DL_CR_ERR,
    UCT_RM_URMA_IFACE_STAT_RX_FC_REQ,
    UCT_RM_URMA_IFACE_STAT_TX_FC_GRANT,
    UCT_RM_URMA_IFACE_STAT_TX_KA_SUCCESS,
    UCT_RM_URMA_IFACE_STAT_TX_KA_FAIL,
    UCT_RM_URMA_IFACE_STAT_NO_CQE,
    UCT_RM_URMA_IFACE_STAT_LAST
};

enum {
    UCT_RM_URMA_FLUSH_MODE_RDMA_WRITE_0,
    UCT_RM_URMA_FLUSH_MODE_FLOW_CONTROL,
    UCT_RM_URMA_FLUSH_MODE_AUTO,
    UCT_RM_URMA_FLUSH_MODE_LAST
};

typedef struct uct_rm_urma_pending_req {
    uct_pending_req_t super;
    uct_ep_t          *ep;
} uct_rm_urma_pending_req_t;

/* A grant owns its destination independently of any business EP. The same
 * object stays in pending, then in the control sender's outstanding queue. */
typedef struct uct_rm_urma_fc_pending_req {
    uct_rm_urma_pending_req_t super;
    uct_urma_iface_send_op_t  send_op;
    uct_rm_urma_target_t     *target;
    uct_ub_iface_send_desc_t *desc;
    uct_rm_urma_ep_t         *legacy_ep; /* only while an old-format reply waits */
    uint16_t                 remote_ep_id;
} uct_rm_urma_fc_pending_req_t;

struct uct_rm_urma_iface {
    uct_urma_iface_t             super;
    uct_rm_urma_jetty_t          rm_urma_jetty; /* owned once per iface */
    khash_t(uct_rm_urma_targets) targets;
    ucs_callback_t             progress;
    uct_ub_iface_send_desc_t   *fc_desc; /* used when max_inline is zero */
    struct {
        /* Threshold to send "soft" FC credit request. The peer will try to
         * piggy-back credits grant to the counter AM, if any. */
        int16_t                fc_soft_thresh;
        uint8_t                flush_by_fc;
        unsigned               native_write;
    } config;
    struct {
        ucs_mpool_t            pending_mp; /* pool for FC grant and keepalive pending requests */
        ucs_arbiter_t          arbiter;
        uct_rm_urma_ep_t      *ctrl_ep; /* software completion owner, no Jetty */
        ucs_list_link_t        retired_eps;
        ucs_status_t          error;
        unsigned              progress_depth;
        /* Only this group may send past its own queued head callback. */
        ucs_arbiter_group_t   *pending_group;
    } tx;

    struct {
        unsigned mode;
        unsigned max_peers;
        unsigned peer_count;
        int creating_control;
        uint64_t instance;
        uint64_t sequence;
        uint32_t rx_free;
        uint32_t data_debt;
        uint32_t control_debt;
        uct_rm_urma_iface_addr_v2_t address;
        ucs_list_link_t peers;
        ucs_list_link_t pending;
        khash_t(uct_rm_urma_requests) requests;
        khash_t(uct_rm_urma_peers) peers_by_address;
    } link;

    UCS_STATS_NODE_DECLARE(stats)
};

typedef struct uct_rm_urma_iface_config {
    uct_urma_iface_config_t      super;
    unsigned                   flush_mode;
    unsigned                   tx_jfc_len;
    double                     soft_thresh;
    unsigned                   link_mode;
    unsigned                   max_peers;
    unsigned                   native_write;
} uct_rm_urma_iface_config_t;

static UCS_F_ALWAYS_INLINE int
uct_rm_urma_have_jfc_avail(uct_rm_urma_iface_t* iface)
{
    return iface->super.tx.jfc_available > 0;
}

static UCS_F_ALWAYS_INLINE ucs_status_t
uct_rm_urma_iface_invoke_pending_cb(uct_rm_urma_iface_t *iface,
                                    ucs_arbiter_group_t *group,
                                    uct_pending_req_t *req)
{
    ucs_arbiter_group_t *previous_group = iface->tx.pending_group;
    ucs_status_t status;

    ucs_trace_data("Progressing pending request");
    iface->tx.pending_group = group;
    status = req->func(req);
    iface->tx.pending_group = previous_group;
    ucs_trace_data("Status returned from progress pending: %s",
                   ucs_status_string(status));

    return status;
}

static UCS_F_ALWAYS_INLINE int
uct_rm_urma_iface_pending_can_send(uct_rm_urma_iface_t *iface,
                                    ucs_arbiter_group_t *group)
{
    return (iface->tx.pending_group == group) ||
           ucs_arbiter_group_is_empty(group);
}

static UCS_F_ALWAYS_INLINE void
uct_rm_urma_iface_check_pending(uct_rm_urma_iface_t *iface, ucs_arbiter_group_t *arb_group)
{
    ucs_assert(uct_rm_urma_iface_pending_can_send(iface, arb_group));
}

void uct_rm_urma_available_set(uct_rm_urma_iface_t *iface, uint8_t jfs_idx, int16_t val);
void uct_rm_urma_iface_set_failed(uct_rm_urma_iface_t *iface, ucs_status_t status);
void uct_rm_urma_iface_gc(uct_rm_urma_iface_t *iface);
ucs_status_t uct_rm_urma_iface_target_get(uct_rm_urma_iface_t *iface,
                                         const urma_rjetty_t *remote,
                                         const urma_token_t *token,
                                         uct_rm_urma_target_t **target_p);
void uct_rm_urma_iface_target_put(uct_rm_urma_iface_t *iface,
                                 uct_rm_urma_target_t *target);
ucs_status_t uct_rm_urma_iface_send_fc_grant(uct_rm_urma_iface_t *iface,
                                           const urma_rjetty_t *remote,
                                           const urma_token_t *token,
                                           uint32_t remote_ep_id);
#endif
