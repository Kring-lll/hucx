"""Pure planning/validation helpers shared by the runner and CPU tests."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics

PRODUCT_COMMIT = '2a8e3b771211e5af491c8ca483ebcc01a03c2f01'
PRODUCT_MODULE = '26b9dac631af27622d0f04f3f86758332bb59874f4cf21743d2820c5783e3aa6'
PRE_IFACE_COMMIT = '4b77878ce65994387a1bf9e9a1c283e676c7f786'
PRE_IFACE_MODULE = 'ed196bd68a22353067c73fc5f1a7a2e177123f9139beebc532ce767aebc4591c'
MINIMAL_MODULE = '58c4efdf20773d0a60bd23594ca579392e968d04d0198933e3f47343ea2185ca'
LAB = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019')
DEFAULT_CASE = LAB / 'r1_shared_2a8e3b771_ps2tIz'
PRE_IFACE = LAB / 'r1_shared_4b77878ce_DbsqE3/install'
MINIMAL = LAB / 'rm_minimal_d58a141bc_zMBCk1/install'
A8_LEVELS = (1, 2, 4, 8, 12, 16, 20)
A9_LEVELS = (32, 48, 64, 96, 128, 160, 192)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def budget(ppn, window=8):
    if not 1 <= ppn <= 192 or window != 8:
        raise ValueError('unsupported PPN/window')
    peers = 2 * ppn
    required = peers * (window + 2) + 2
    rx_depth = max(64, 1 << (required - 1).bit_length())
    return {'ppn': ppn, 'max_peers': peers, 'window': window,
            'tx_depth': 64, 'rx_depth': rx_depth, 'required_rx': required}


def levels(capacity, requested=192):
    limit = min(capacity, requested, 192)
    result = [n for n in A9_LEVELS if n <= limit]
    # Test the actual usable endpoint if it lies between standard steps.
    if limit > 20 and limit not in result:
        result.append(limit)
    return sorted(result)


def saved_spec(path, install):
    record = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    command = record['command']
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        raise ValueError('saved command must be an argv list/string')
    environment = record.get('environment', {})
    if isinstance(environment, list):
        environment = dict(value.split('=', 1) for value in environment)
    environment = {str(k): str(v).replace(str(MINIMAL), str(install))
                   for k, v in environment.items()}
    command = [value.replace(str(MINIMAL), str(install)) for value in command]
    for item in command:
        key, separator, value = item.partition('=')
        if separator and re.fullmatch(r'(?:UCX_|LD_|OMPI_)[A-Za-z0-9_]+', key):
            environment[key] = value
    if '-c' not in command or not any(Path(x).name == 'osu_latency' for x in command):
        raise ValueError('saved validated OSU command is required')
    if str(install) not in shlex.join(command) + str(environment):
        raise ValueError('saved command does not pin the expected install')
    environment['UCX_MODULE_DIR'] = str(install / 'lib/ucx')
    return command, environment


def launcher(command, ppn, hosts, wrapper, config):
    """Retain the pinned MPI binary/prefix/MCA. Replace only placement/app/env.

    Fail closed for unrecognized options instead of silently dropping MCA args.
    The saved command may start with env and a launcher timeout; the new runner
    owns a longer launcher timeout and each rank has an independent deadline.
    """
    position = next(i for i, x in enumerate(command)
                    if Path(x).name in ('mpirun', 'mpiexec'))
    application = next(i for i, x in enumerate(command) if Path(x).name == 'osu_latency')
    source = command[position + 1:application]
    retained = []
    index = 0
    placement = {'-n', '-np', '--np', '--n', '--host', '-H', '--map-by', '-map-by',
                 '--rank-by', '--bind-to', '--hostfile', '-hostfile', '--machinefile', '-x'}
    one_value = {'--prefix', '--wdir', '-wdir', '--wd', '-wd'}
    flags = {'--allow-run-as-root', '--report-bindings', '--tag-output', '--timestamp-output',
             '--oversubscribe', '--nooversubscribe', '--display-map'}
    while index < len(source):
        item = source[index]
        if item in ('--mca', '-mca', '--gmca', '-gmca'):
            if index + 2 >= len(source):
                raise ValueError('incomplete MCA option')
            retained.extend(source[index:index + 3])
            index += 3
        elif item in placement:
            if index + 1 >= len(source):
                raise ValueError('incomplete placement/export option')
            index += 2
        elif item in one_value:
            retained.extend(source[index:index + 2])
            index += 2
        elif item in flags:
            if item not in ('--oversubscribe', '--nooversubscribe'):
                retained.append(item)
            index += 1
        elif re.fullmatch(r'-n(?:p)?\d+', item):
            index += 1
        else:
            raise ValueError('unrecognized saved launcher option: ' + item)
    return [command[position]] + retained + [
        '-np', str(2 * ppn), '--host', '%s:%d,%s:%d' % (hosts[0], ppn, hosts[1], ppn),
        '--map-by', 'ppr:%d:node' % ppn, '--rank-by', 'slot', '--bind-to', 'core',
        '--nooversubscribe', '/usr/bin/python3', str(wrapper), str(config)]


def environment(saved, mode, install, configuration):
    env = {key: value for key, value in saved.items()
           if not key.startswith('UCX_') and key not in ('LD_PRELOAD', 'LD_DEBUG')}
    # Keep device/protocol choices from the measured baseline, then override
    # only the documented fair-comparison parameters below.
    env.update({key: value for key, value in saved.items() if key.startswith('UCX_')})
    kind = 'RC' if mode == 'rc' else 'RM'
    prefix = 'UCX_' + kind + '_URMA_'
    env.update({prefix + key: value for key, value in {
        'FC_ENABLE': 'y', 'FC_WND_SIZE': str(configuration['window']),
        'TX_DEPTH': str(configuration['tx_depth']), 'RX_DEPTH': str(configuration['rx_depth']),
        'FC_SOFT_THRESH': '0.5', 'FC_HARD_THRESH': '0.25'
    }.items()})
    env.update(UCX_TLS=('rc' if mode == 'rc' else 'rm') + '_urma,um_urma:aux,self',
               UCX_RNDV_THRESH='inf', UCX_ZCOPY_THRESH='inf', UCX_LOG_LEVEL='warn',
               UCX_PROTO_ENABLE='n', UCX_MODULE_DIR=str(install / 'lib/ucx'),
               LD_LIBRARY_PATH=':'.join(dict.fromkeys([str(install / 'lib')] +
                       saved.get('LD_LIBRARY_PATH', '').split(':'))).rstrip(':'),
               LD_PRELOAD=':'.join(str(install / 'lib' / ('lib' + lib + '.so.0'))
                                   for lib in ('ucp', 'uct', 'ucs', 'ucm')),
               OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    env.pop('UCX_RM_URMA_LINK_MODE', None)
    env.pop('UCX_RM_URMA_MAX_PEERS', None)
    if mode == 'iface':
        env['UCX_RM_URMA_LINK_MODE'] = 'iface_v2'
        env['UCX_RM_URMA_MAX_PEERS'] = str(configuration['max_peers'])
    return env


def check_maps(text, install):
    root = str(Path(install).resolve())
    for library in ('libucp.so', 'libuct.so', 'libucs.so', 'libucm.so', 'libuct_ub.so'):
        rows = [line for line in text.splitlines() if library in line]
        if not rows or any(not line.split()[-1].startswith(root + '/lib/') for line in rows):
            raise ValueError('missing/mixed library mapping: ' + library)
    return True


def resource_counts(directory, expected_ranks, mode, install, depth):
    results = {}
    for path in sorted(Path(directory).glob('rank*_pid*.jsonl')):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        starts = [row for row in rows if row.get('event') == 'start']
        if len(starts) != 1:
            raise ValueError('observer start missing/duplicated')
        rank = starts[0]['rank']
        if rank < 0:
            continue
        if str(rank) in results:
            raise ValueError('multiple observer processes for one rank')
        live = {}
        counts = {}
        peaks = {}
        depths = set()
        for row in rows:
            if row.get('valid', 1) != 1:
                raise ValueError('observer invalid')
            if row.get('kind') != 'jetty' or row.get('rc') != 0:
                continue
            pointer = row['ptr']
            if row['event'] == 'acquire' and row.get('mode') == mode:
                if pointer in live:
                    raise ValueError('duplicate live Jetty')
                context = row['ctx']
                live[pointer] = context
                counts[context] = counts.get(context, 0) + 1
                peaks[context] = max(peaks.get(context, 0), counts[context])
                depths.add(row['depth'])
            elif row['event'] == 'release' and pointer in live:
                context = live.pop(pointer)
                counts[context] -= 1
        if not peaks or live or depths != {depth}:
            raise ValueError('missing Jetty/wrong depth/leak: rank ' + str(rank))
        if mode == 1 and any(value != 1 for value in peaks.values()):
            raise ValueError('RM shared Jetty peak is not one per context')
        check_maps(path.with_suffix('.maps').read_text(), install)
        results[str(rank)] = {'peaks_per_context': peaks, 'tx_depth': depth, 'live_at_exit': 0}
    if set(map(int, results)) != set(expected_ranks):
        raise ValueError('observer rank coverage incomplete')
    return results


def summarize(rows, ppn):
    if sorted(row['rank'] for row in rows) != list(range(2 * ppn)):
        raise ValueError('rank coverage incomplete/duplicate')
    rounds = {row['rounds'] for row in rows}
    if len(rounds) != 1:
        raise ValueError('rounds mismatch')
    for row in rows:
        if row['ppn'] != ppn or row['data_validation'] is not True or row['finalized'] is not True:
            raise ValueError('payload/finalize failed')
        for key in ('init_s', 'first_all_peers_s', 'steady_s', 'burst64_s', 'burst1024_s', 'finalize_s'):
            if not math.isfinite(row[key]) or row[key] <= 0:
                raise ValueError('invalid duration')
        if any(row[key] <= 0 for key in ('init_rss_kib', 'first_rss_kib', 'steady_rss_kib')):
            raise ValueError('invalid memory metric')
    count = next(iter(rounds))
    maximum = lambda key: max(row[key] for row in rows)
    return {
        'init_max_ms': maximum('init_s') * 1000,
        'first_all_peers_max_ms': maximum('first_all_peers_s') * 1000,
        'exchange8_max_rank_us': maximum('steady_s') * 1e6 / (count * ppn),
        'burst64_payload_MBps': 2 * ppn * ppn * count * 64 / maximum('burst64_s') / 1e6,
        'burst1024_payload_MBps': 2 * ppn * ppn * count * 1024 / maximum('burst1024_s') / 1e6,
        'finalize_max_ms': maximum('finalize_s') * 1000,
        'init_rss_sum_mib': sum(row['init_rss_kib'] for row in rows) / 1024,
        'first_rss_sum_mib': sum(row['first_rss_kib'] for row in rows) / 1024,
        'steady_rss_sum_mib': sum(row['steady_rss_kib'] for row in rows) / 1024,
        'peak_rss_sum_mib': sum(row['peak_rss_kib'] for row in rows) / 1024
    }


def statistics_for(samples):
    if not samples or any(not math.isfinite(value) for value in samples):
        raise ValueError('no finite complete samples')
    return {'samples': samples, 'median': statistics.median(samples),
            'minimum': min(samples), 'maximum': max(samples)}
