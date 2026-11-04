"""Exact, hash-guarded instrumentation of an isolated a2b source copy."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE_COMMIT = 'a2b4b07c12be5401afaf427877aadd6752871810'
BASE_MODULE = '2543923f6b2cae2a37365d293e715bc63fefa539ecc8b146880b6270b0351aa0'
ARCHIVE_SHA = 'f0d11b1066f735cfa23ceaa94c2700a9187f674dfe15d5d02fb0e9a5dcf38bae'
ACCEPTED = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-c-send-reliability-dev/r1_shared_a2b4b07c1_Z1tIOO')
EVENTS = ('peer_scanned', 'replay_scanned', 'rx_post_calls', 'rx_buffers_posted',
          'rx_post_errors', 'ack_received', 'data_received', 'data_delivered',
          'data_discarded', 'data_alloc_attempts', 'ack_alloc_attempts',
          'data_released', 'ack_released', 'reliable_check_no_resource',
          'reliable_post_success', 'reliable_retry_success',
          'rx_batch_1', 'rx_batch_2_7', 'rx_batch_8_31', 'rx_batch_32_plus')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError('instrumentation anchor changed: ' + old[:100])
    return text.replace(old, new)


def transform(name, text):
    include = '#include <uct/urma/base/rm_send_cost_diag.h>\n'
    if 'rm_send_cost_diag.h' in text:
        raise ValueError('source already instrumented')
    # Insert after config inclusion, through an existing local header include.
    anchor = next(line for line in text.splitlines(True)
                  if line.startswith('#include ') and 'config.h' not in line)
    text = replace(text, anchor, anchor + include)
    def call(old, label, func, args, void=False, count=1):
        nonlocal text
        wrapper = 'COST_VOID' if void else 'COST_CALL'
        text = replace(text, old, '%s("%s", %s, %s)' % (wrapper, label, func, args), count)
    if name.endswith('rm_urma_reliable.c'):
        text = replace(text, '    if (!uct_rm_urma_reliable_can_send(ep)) {\n        return UCS_ERR_NO_RESOURCE;',
                       '    if (!uct_rm_urma_reliable_can_send(ep)) {\n        COST_ADD(13, 1);\n        return UCS_ERR_NO_RESOURCE;')
        call('ucs_calloc(1, sizeof(*entry), "rm_send_replay")', 'rmc_alloc_metadata',
             'ucs_calloc', '1, sizeof(*entry), "rm_send_replay"')
        call('ucs_mpool_get(&iface->super.tx.mp)', 'rmc_alloc_tx_buffer',
             'ucs_mpool_get', '&iface->super.tx.mp')
        call('ucs_free(entry)', 'rmc_free_metadata', 'ucs_free', 'entry', True, 6)
        call('ucs_mpool_put(entry->desc)', 'rmc_free_tx_buffer', 'ucs_mpool_put',
             'entry->desc', True, 5)
        call('rel_alloc(iface, ep->peer, UCT_RM_URMA_REL_DATA)', 'rmc_data_alloc',
             'rel_alloc', 'iface, ep->peer, UCT_RM_URMA_REL_DATA', count=2)
        text = replace(text, '    entry = COST_CALL("rmc_data_alloc",',
                       '    COST_ADD(9, 1);\n    entry = COST_CALL("rmc_data_alloc",', 2)
        text = replace(text, '    status = rel_check(ep);',
                       '    (void)COST_CALL("rmc_empty", hucx_cost_empty);\n    status = rel_check(ep);', 2)
        call('rel_alloc(iface, peer, UCT_RM_URMA_REL_ACK)', 'rmc_ack_alloc',
             'rel_alloc', 'iface, peer, UCT_RM_URMA_REL_ACK')
        text = replace(text, '            entry = COST_CALL("rmc_ack_alloc",',
                       '            COST_ADD(10, 1);\n            entry = COST_CALL("rmc_ack_alloc",')
        call('rel_header(iface, entry, id, length)', 'rmc_data_header', 'rel_header',
             'iface, entry, id, length', True)
        call('rel_header(iface, entry, 0, 0)', 'rmc_ack_header', 'rel_header',
             'iface, entry, 0, 0', True)
        old = 'uct_rm_urma_ep_reliable_post(iface, entry)'
        if text.count(old) != 3:
            raise ValueError('reliable DATA/ACK/retry post sites changed')
        for label in ('rmc_data_post_total', 'rmc_ack_post_total', 'rmc_retry_post_total'):
            text = text.replace(old, 'COST_CALL("%s", uct_rm_urma_ep_reliable_post, iface, entry)' % label, 1)
        call('memcpy(data, &header, sizeof(header))', 'rmc_copy_short_header', 'memcpy',
             'data, &header, sizeof(header)')
        call('memcpy(data + sizeof(header), buffer, length)', 'rmc_copy_short_payload',
             'memcpy', 'data + sizeof(header), buffer, length')
        call('uct_rm_urma_reliable_address_unpack(wire + 56, 48, &remote.iface)',
             'rmc_decode_address', 'uct_rm_urma_reliable_address_unpack',
             'wire + 56, 48, &remote.iface')
        call('uct_rm_urma_link_peer_get(iface, &remote, &peer)', 'rmc_data_peer_get',
             'uct_rm_urma_link_peer_get', 'iface, &remote, &peer')
        call('rel_release(iface, entry)', 'rmc_retire_entry', 'rel_release',
             'iface, entry', True, 3)
        call('uct_iface_invoke_am(&iface->super.super, wire[6],\n                    (void *)(wire + UCT_RM_URMA_REL_HEADER), payload, 0)',
             'rmc_deliver_ucp', 'uct_iface_invoke_am',
             '&iface->super.super, wire[6], (void *)(wire + UCT_RM_URMA_REL_HEADER), payload, 0')
        text = replace(text, '        ucs_list_del(&entry->list);',
                       '        COST_ADD(11, 1);\n        ucs_list_del(&entry->list);')
        text = replace(text, '        entry->peer->reliable.ack_in_flight = 0;',
                       '        COST_ADD(12, 1);\n        entry->peer->reliable.ack_in_flight = 0;')
        text = replace(text, '        ucs_list_for_each(peer, &iface->link.peers, list) {',
                       '        COST_ADD(5, 1);\n        ucs_list_for_each(peer, &iface->link.peers, list) {')
        text = replace(text, '    memcpy(remote.eid, wire + 36, 16);',
                       '    COST_ADD(6, 1);\n    memcpy(remote.eid, wire + 36, 16);')
        text = replace(text, '        ++iface->reliable.discarded;',
                       '        COST_ADD(8, 1);\n        ++iface->reliable.discarded;')
        text = replace(text, '    ++iface->reliable.delivered;',
                       '    COST_ADD(7, 1);\n    ++iface->reliable.delivered;')
        text = replace(text, '    ucs_list_for_each(peer, &iface->link.peers, list) {\n        if (peer->reliable.ack_in_flight',
                       '    ucs_list_for_each(peer, &iface->link.peers, list) {\n        COST_ADD(0, 1);\n        if (peer->reliable.ack_in_flight')
        text = replace(text, '        ucs_list_for_each_safe(entry, next, &peer->reliable.replay, list) {\n            if (now',
                       '        ucs_list_for_each_safe(entry, next, &peer->reliable.replay, list) {\n            COST_ADD(1, 1);\n            if (now')
        text = replace(text, '            ++iface->reliable.retries;',
                       '            COST_ADD(15, 1);\n            ++iface->reliable.retries;')
    elif name.endswith('rm_urma_link.c'):
        for variable in ('existing', 'incoming'):
            args = '%s, sizeof(%s), &%s' % (variable, variable,
                    'peer->remote.iface' if variable == 'existing' else 'remote->iface')
            call('uct_rm_urma_iface_addr_v2_pack(' + args + ')', 'rmc_peer_pack_' + variable,
                 'uct_rm_urma_iface_addr_v2_pack', args)
        call('memcmp(existing, incoming, sizeof(existing))', 'rmc_peer_address_compare',
             'memcmp', 'existing, incoming, sizeof(existing)')
    elif name.endswith('rm_urma_ep.c'):
        call('uct_rm_urma_reliable_short(ep, id, hdr, buffer, length)', 'rmc_short_total',
             'uct_rm_urma_reliable_short', 'ep, id, hdr, buffer, length')
        # Only the reliable post call site, not the other native/link post paths.
        anchor = 'result = urma_post_jetty_send_wr(ep->rm_urma_jetty->jetty, wr, &bad);'
        start = text.index('ucs_status_t uct_rm_urma_ep_reliable_post(')
        text = text[:start] + replace(text[start:], anchor,
                       'result = COST_CALL("rmc_reliable_hw_post", urma_post_jetty_send_wr, ep->rm_urma_jetty->jetty, wr, &bad);')
        text = replace(text, '    entry->in_flight = 1;',
                       '    COST_ADD(14, 1);\n    entry->in_flight = 1;')
    elif name.endswith('rm_urma_iface.c'):
        call('uct_rm_urma_reliable_progress(iface, ucs_get_time())', 'rmc_reliable_progress',
             'uct_rm_urma_reliable_progress', 'iface, ucs_get_time()', True)
        original = 'uct_rm_urma_reliable_receive(iface, hdr + 1,\n                                           cr->completion_len - sizeof(*hdr))'
        receive = 'uct_rm_urma_reliable_receive, iface, hdr + 1, cr->completion_len - sizeof(*hdr)'
        text = replace(text, original,
            '((cr->completion_len >= sizeof(*hdr) + 36u && ((const uint8_t *)(hdr + 1))[5] == 2u) ? '
            'COST_CALL("rmc_ack_receive", '+receive+') : COST_CALL("rmc_data_receive", '+receive+'))')
        call('uct_rm_urma_iface_poll_tx(iface)', 'rmc_rm_poll_tx',
             'uct_rm_urma_iface_poll_tx', 'iface')
    elif name.endswith('urma_iface.c'):
        call('urma_post_jfr_wr(iface->rx.jfr, g_wrs, &bad_wr)', 'rmc_rx_hw_post',
             'urma_post_jfr_wr', 'iface->rx.jfr, g_wrs, &bad_wr')
        text = replace(text, '    status = COST_CALL("rmc_rx_hw_post",',
                       '    COST_ADD(2, 1);\n    status = COST_CALL("rmc_rx_hw_post",')
        text = replace(text, '    if (ucs_unlikely(status != URMA_SUCCESS)) {\n        if (result != NULL)',
                       '    if (ucs_unlikely(status != URMA_SUCCESS)) {\n        COST_ADD(4, 1);\n        if (result != NULL)')
        text = replace(text, '    if (result != NULL) {\n        result->posted += tmp_count;',
                       '    COST_ADD(3, tmp_count);\n    COST_ADD(tmp_count == 1 ? 16 : tmp_count < 8 ? 17 : tmp_count < 32 ? 18 : 19, 1);\n    if (result != NULL) {\n        result->posted += tmp_count;')
    return text


FILES = ('src/uct/urma/rm_urma/rm_urma_reliable.c',
         'src/uct/urma/rm_urma/rm_urma_link.c',
         'src/uct/urma/rm_urma/rm_urma_ep.c',
         'src/uct/urma/rm_urma/rm_urma_iface.c',
         'src/uct/urma/base/urma_iface.c')


def apply(source):
    source = Path(source)
    header = source / 'src/uct/urma/base/rm_send_cost_diag.h'
    if header.exists():
        raise ValueError('diagnostic header already exists')
    expected = json.loads((ROOT / 'source_identity.json').read_text())
    outputs = {}
    for name in FILES:
        path = source / name
        if digest(path) != expected[name]:
            raise ValueError('source is not accepted a2b: ' + name)
        outputs[name] = transform(name, path.read_text())
    # All anchors checked before touching any file.
    for name, value in outputs.items():
        (source / name).write_text(value)
    header.write_bytes((ROOT / 'diag.h').read_bytes())
    changed = {name:digest(source/name) for name in FILES}
    changed[str(header.relative_to(source))] = digest(header)
    manifest = source / 'contrib/rm_ctp/r1/candidate_manifest.json'
    value = json.loads(manifest.read_text())
    for name in FILES:
        value['files'][name] = changed[name]
    value['diagnostic_only_derived_from'] = BASE_COMMIT
    manifest.write_text(json.dumps(value, indent=2) + '\n')
    return changed
