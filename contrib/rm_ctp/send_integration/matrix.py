"""Fixed SEND-only workload and strict, device-independent evidence checks."""
from decimal import Decimal
from pathlib import Path
import re

COMMIT = '97b50328718c3c7c1b613c2ee09a90c198666142'
MODULE = '3245bcca9bb5319230ed1d3e153191501fba4602a1529f405944992beb6eeec7'
AM_BCOPY = 3989
SINGLE_BOUNDARY = AM_BCOPY - 8
TWO_FRAGMENT_BOUNDARY = (AM_BCOPY - 24) + (AM_BCOPY - 16)
SIZES = (8, 3980, 3981, 3982, 7937, 7938, 7939, 16384, 65536, 1048576)
ALTERNATING = (8, 3981, 8, 3982, 64, 7938, 8, 7939, 117, 65536, 8, 1048576, 8)
INJECTED_SIZES = (8, 65536, 8)
PPNS = (1, 2, 4)
AM_CASES = ((1, 16383), (1, 16384), (1, 16385), (1, 65536), (1, 1048576), (4, 65536))


def probe_schedule(injection=False):
    return (INJECTED_SIZES, 1) if injection else (ALTERNATING, 2)


def plan():
    return {'source_commit': COMMIT, 'module_sha256': MODULE,
            'transport_changed': False, 'performance_claim': False,
            'standard_osu': {'benchmark': 'osu_multi_lat', 'version': '7.5.2',
                             'validation': True, 'ppn': list(PPNS), 'sizes': list(SIZES)},
            'am_rendezvous': {'threshold': 16384, 'cases': list(AM_CASES),
                              'actual_request_trace_required': True},
            'strict_alternation': {'ppn': list(PPNS), 'sizes': list(ALTERNATING), 'cycles': 2},
            'software_injection': {'ppn': 1, 'sizes': list(INJECTED_SIZES),
                                   'profile': 'eager_middle', 'hardware_network_loss': False},
            'total_mpi_tasks': len(PPNS) * (len(SIZES) + 1) + len(AM_CASES) + 1,
            'max_ppn': 4, 'timeout_per_task_seconds': 240,
            'am_bcopy_bytes': AM_BCOPY, 'single_application_boundary': SINGLE_BOUNDARY,
            'two_fragment_application_boundary': TWO_FRAGMENT_BOUNDARY,
            'native_rma': False, 'atomics': False, 'ppn192': False,
            'node_acceptance': 'PENDING'}


def reliable_environment(case, ppn, injection=False, path='eager'):
    if ppn not in PPNS or path not in ('eager', 'am_rndv'):
        raise ValueError('workload outside the fixed small-scale SEND matrix')
    if injection and (ppn != 1 or path != 'eager'):
        raise ValueError('injection is restricted to one eager pair')
    case = Path(case)
    peers = 2 * ppn
    required = peers * 10 + 2
    rx_depth = max(64, 1 << (required - 1).bit_length())
    threshold = '16384' if path == 'am_rndv' else 'inf'
    env = {
        'UCX_TLS': 'rm_urma,um_urma:aux,self', 'UCX_NET_DEVICES': 'udmac0d1e2:1',
        'UCX_PROTO_ENABLE': 'n', 'UCX_RNDV_THRESH': threshold,
        'UCX_RNDV_SEND_NBR_THRESH': threshold, 'UCX_RNDV_SCHEME': 'am',
        'UCX_ZCOPY_THRESH': 'inf', 'UCX_RM_URMA_LINK_MODE': 'reliable_v3',
        'UCX_RM_URMA_SEG_SIZE': '4096', 'UCX_RM_URMA_MAX_PEERS': str(peers),
        'UCX_RM_URMA_FC_ENABLE': 'y', 'UCX_RM_URMA_FC_WND_SIZE': '8',
        'UCX_RM_URMA_TX_DEPTH': '64', 'UCX_RM_URMA_RX_DEPTH': str(rx_depth),
        'UCX_RM_URMA_FC_SOFT_THRESH': '0.5', 'UCX_RM_URMA_FC_HARD_THRESH': '0.25',
        'UCX_RM_URMA_NATIVE_WRITE': 'n', 'UCX_RM_URMA_NATIVE_READ': 'n',
        'UCX_RM_URMA_NATIVE_RMA': 'n', 'UCX_RM_URMA_SEND_RTO': '100ms',
        'UCX_RM_URMA_SEND_TIMEOUT': '30s', 'UCX_RM_URMA_SEND_RETRIES': '256',
        'UCX_MODULE_DIR': str(case / 'install/lib/ucx'),
        'LD_LIBRARY_PATH': str(case / 'install/lib') + ':' + str(case / 'install/lib/ucx')}
    if injection:
        env['HUCX_SEND_INJECT_PROFILE'] = 'eager_middle'
    return env


def time_value(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(ns|us|ms|s|m)', value.strip())
    if match is None:
        raise ValueError('unexpected time value: ' + value)
    scale = {'ns': '0.000000001', 'us': '0.000001', 'ms': '0.001', 's': '1', 'm': '60'}
    return Decimal(match[1]) * Decimal(scale[match[2]])


def check_config(text, env, bytes_value):
    parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
    names = [key for key in env if key.startswith('UCX_RM_URMA_')]
    names += ['UCX_TLS', 'UCX_NET_DEVICES', 'UCX_PROTO_ENABLE', 'UCX_RNDV_THRESH',
              'UCX_RNDV_SEND_NBR_THRESH', 'UCX_RNDV_SCHEME', 'UCX_ZCOPY_THRESH']
    for key in names:
        expected = env[key]
        actual = parsed.get(key)
        if actual is None:
            raise ValueError('effective configuration missing: ' + key)
        if key.endswith(('SEND_RTO', 'SEND_TIMEOUT')):
            same = time_value(actual) == time_value(expected)
        elif key.endswith('SEG_SIZE') or (key in ('UCX_RNDV_THRESH', 'UCX_RNDV_SEND_NBR_THRESH') and expected != 'inf'):
            same = bytes_value(actual) == int(expected)
        elif key.endswith('_THRESH') and key.startswith('UCX_RM_URMA_'):
            same = Decimal(actual) == Decimal(expected)
        else:
            same = actual.lower() == expected.lower()
        if not same:
            raise ValueError('effective configuration differs: %s=%s expected=%s' % (key, actual, expected))
    return {key: parsed[key] for key in names}


def inspect_probe(rows, ppn, pids, nonce, injection=False):
    sizes, cycles = probe_schedule(injection)
    expected = len(sizes) * cycles
    expected_bytes = sum(sizes) * cycles
    if len(rows) != 2 * ppn or sorted(row.get('rank', -1) for row in rows) != list(range(2 * ppn)):
        raise ValueError('MPI proof rank coverage missing/duplicated')
    for row in rows:
        rank = row['rank']
        peer = rank + ppn if rank < ppn else rank - ppn
        if (row.get('world') != 2 * ppn or row.get('peer') != peer or
                row.get('pid') != pids[rank] or row.get('nonce') != nonce or
                row.get('sent') != expected or row.get('received') != expected or
                row.get('send_bytes') != expected_bytes or row.get('receive_bytes') != expected_bytes or
                row.get('cycles') != cycles or row.get('sizes') != list(sizes) or
                row.get('injection') is not injection or row.get('finalized') is not True or
                row.get('unexpected_messages') != 0 or
                any(row.get(name) != 'PASS' for name in ('content', 'order', 'guards'))):
            raise ValueError('MPI actual PID/count/content/order/full-guard/finalize failed: rank %d' % rank)
    return {'ranks': 2 * ppn, 'messages_each_direction_per_pair': expected,
            'bytes_each_direction_per_pair': expected_bytes, 'sizes': list(sizes), 'cycles': cycles,
            'unexpected_messages': 0, 'scope': 'observed application traffic, not arbitrarily late duplicate proof'}


def inspect_injection(rows, pids, nonce):
    if len(rows) != 2 or sorted(r.get('rank', -1) for r in rows) != [0, 1]:
        raise ValueError('missing actual injection process evidence')
    for row in rows:
        if (row.get('pid') != pids[row['rank']] or row.get('nonce') != nonce or
                row.get('calls', 0) <= 0 or row.get('data_drops') != 1 or
                row.get('ack_drops') != 2 or row.get('duplicates') != 1 or
                row.get('total_bytes') != 65536 or
                not 0 < row.get('duplicate_offset', 0) < row.get('drop_offset', 0) < 65536 or
                not 0 < row.get('duplicate_psn', 0) < row.get('drop_psn', 0)):
            raise ValueError('interior-fragment drop/duplicate/ACK profile incomplete or wrong PID')
    return {'data_drops': 2, 'ack_drops': 4, 'duplicates': 2,
            'details': rows, 'hardware_network_loss': False}
