"""Pinned normal AM rendezvous workload and request-level path evidence."""
from contextlib import contextmanager
from split_logs import log_pattern
import re

BASE = '9e1a3a6b5ed4074ac162258397065fd0f54831e0'
THRESHOLD = 16384
ITERATIONS = 20
WARMUP = 2
SELECTION = re.compile(r'select tag request\((0x[0-9a-f]+)\).*?length=(\d+) '
                       r'.*?rndv_thresh=(\d+) ', re.I)
REQUEST = re.compile(r'\breq (0x[0-9a-f]+): (.*)', re.I)
IDENTITY = re.compile(r'\[([A-Za-z0-9_.-]+):(\d+)\s*:\d+\]')


def osu_text(text):
    # REQ logging includes ordinary allocation fallback messages containing
    # "failed". Keep fatal checks on the full log, then parse original OSU rows.
    if re.search(r'UCX\s+(ERROR|FATAL)|DATA VALIDATION ERROR|Caught signal|'
                 r'MPI_ERR_|OSU_PROOF_ERROR', text, re.I):
        raise ValueError('fatal/runtime/data error in complete log')
    return '\n'.join(line for line in text.splitlines()
                     if not (IDENTITY.search(line) and re.search(r'\bUCX\s+\w+\b', line)))


def cases():
    rows = []

    def add(name, size, path, ppn=1, mode='rm', threshold=THRESHOLD):
        rows.append({'benchmark': name, 'mode': mode, 'ppn': ppn,
                     'sizes': (size,), 'window': 8 if name in ('osu_bw', 'osu_bibw') else None,
                     'path': path, 'threshold': threshold})

    add('osu_bw', 8, 'gate', mode='rc', threshold=None)
    add('osu_bw', 8, 'gate', threshold=None)
    for size in (16383, 16384, 16385):
        add('osu_bw', size, 'eager' if size < THRESHOLD else 'am_rndv')
    add('osu_bw', 65536, 'eager', threshold=None)
    add('osu_bw', 65536, 'am_rndv')
    add('osu_bw', 1048576, 'am_rndv')
    add('osu_bibw', 65536, 'am_rndv')
    add('osu_multi_lat', 65536, 'am_rndv', ppn=4)
    return rows


def arguments(case):
    if case not in cases():
        raise ValueError('case is outside the fixed normal AM rendezvous matrix')
    size = case['sizes'][0]
    result = ['--message-size', '%d:%d' % (size, size), '--iterations', str(ITERATIONS),
              '--warmup', str(WARMUP), '--validation']
    if case['window'] is not None:
        result += ['--window-size', str(case['window']), '--buffer-num', 'multiple']
    if case['benchmark'] == 'osu_alltoall':
        result.append('--full')
    return result


def settings(case):
    return {'UCX_RNDV_THRESH': str(case['threshold']) if case['threshold'] else 'inf',
            'UCX_RNDV_SEND_NBR_THRESH': str(case['threshold']) if case['threshold'] else 'inf',
            'UCX_RNDV_SCHEME': 'am', 'UCX_ZCOPY_THRESH': 'inf', 'UCX_PROTO_ENABLE': 'n',
            'UCX_LOG_LEVEL': 'warn' if case['path'] == 'gate' else 'req'}


def check_config(text, case, bytes_value):
    parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
    for key, expected in settings(case).items():
        actual = parsed.get(key, '')
        if key in ('UCX_RNDV_THRESH', 'UCX_RNDV_SEND_NBR_THRESH') and expected != 'inf':
            same = bytes_value(actual) == int(expected)
        else:
            same = actual.lower() == expected.lower()
        if not same:
            raise ValueError('effective protocol configuration differs: ' + key + '=' + actual)
    return {key: parsed[key] for key in settings(case)}


def check_am_only_capability(text, parse_capability, device='udmac0d1e2:1'):
    if parse_capability(text, device) != 4093:
        raise ValueError('unexpected RM AM bcopy capability')
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if re.search(r'\bDevice:\s*' + re.escape(device) + r'\s*$', line))
    end = next((i for i in range(start + 1, len(lines))
                if re.search(r'\b(?:Device|Transport):', lines[i])), len(lines))
    block = '\n'.join(lines[start:end])
    if re.search(r'^\s*#?\s*(?:put_short|put_bcopy|put_zcopy|get_short|get_bcopy|get_zcopy|am_zcopy)\s*:',
                 block, re.M):
        raise ValueError('unaccepted native RMA or AM zcopy capability is exposed')
    return {'am_bcopy_bytes': 4093, 'native_RMA_and_AM_zcopy_exposed': False}


def inspect_trace(text, case, identities):
    """Associate current request pointer, payload length and actual host/PID.

    Pointer reuse is tracked by the latest selection, never by a global set.
    This identifies the selected UCP request path; it is not a packet/ACK count.
    """
    if case['path'] == 'gate':
        return {'status': 'NOT_REQUIRED_8B_GATE'}
    expected = {0} if case['benchmark'] == 'osu_bw' else set(identities)
    by_process = {(value['host'], value['pid']): rank for rank, value in identities.items()}
    active = {}
    selections = {rank: 0 for rank in expected}
    am = {rank: 0 for rank in expected}
    size = case['sizes'][0]
    threshold = case['threshold']
    examples = []
    lines = text.splitlines() if isinstance(text, str) else text
    for line_number, line in enumerate(lines, 1):
        identity = IDENTITY.search(line)
        if identity is None:
            continue
        rank = by_process.get((identity[1], int(identity[2])))
        if rank not in expected:
            continue
        selected = SELECTION.search(line)
        if selected:
            key = (rank, selected[1].lower())
            length, actual_threshold = int(selected[2]), int(selected[3])
            active[key] = length == size
            if length == size:
                if ((threshold is not None and actual_threshold != threshold)
                        or (threshold is None and actual_threshold <= size)):
                    raise ValueError('actual request rendezvous threshold differs')
                selections[rank] += 1
            continue
        request = REQUEST.search(line)
        if request is None or not active.get((rank, request[1].lower())):
            continue
        message = request[2]
        if 'rndv_put completed' in message or 'rndv_get completed' in message:
            raise ValueError('payload request used an unaccepted RMA rendezvous path')
        if 'using rndv_data protocol' in message:
            am[rank] += 1
            if len(examples) < 4:
                examples.append({'rank': rank, 'line': line_number, 'text': line.strip()})
    if not all(selections.values()):
        raise ValueError('payload request selection missing for actual sending rank')
    if case['path'] == 'am_rndv' and not all(am.values()):
        raise ValueError('AM rendezvous data path missing for actual sending rank')
    if case['path'] == 'eager' and any(am.values()):
        raise ValueError('eager control unexpectedly used AM rendezvous')
    return {'status': 'PASS', 'path': case['path'], 'payload_bytes': size,
            'sending_ranks': sorted(expected), 'request_selections': selections,
            'am_rndv_transitions': am, 'examples': examples,
            'scope': 'ACTUAL_UCP_REQUEST_SELECTION_NOT_NETWORK_PACKETS_OR_RETRY'}


@contextmanager
def trial_profile(osu, case, parse_output, log_directory=None):
    previous = osu.arguments, osu.parse_output, osu.environment

    def make_arguments(benchmark, iterations, warmup, validation, message_bytes=8):
        if (benchmark != case['benchmark'] or not validation
                or iterations != ITERATIONS or warmup != WARMUP):
            raise ValueError('unexpected matrix workload')
        return arguments(case)

    def parse(text, benchmark, validation, iterations, message_bytes=8):
        if benchmark != case['benchmark'] or not validation:
            raise ValueError('unexpected output source')
        return parse_output(osu_text(text), case, iterations)

    def environment(*args, **kwargs):
        value = previous[2](*args, **kwargs)
        value.pop('UCX_LOG_FILE', None)
        if log_directory is not None:
            value['UCX_LOG_FILE'] = log_pattern(log_directory)
            value['UCX_LOG_FILE_SIZE'] = 'inf'
            value['UCX_LOG_FILE_ROTATE'] = '0'
        value.update(settings(case))
        return value

    osu.arguments = make_arguments
    osu.parse_output = parse
    osu.environment = environment
    try:
        yield
    finally:
        osu.arguments, osu.parse_output, osu.environment = previous
