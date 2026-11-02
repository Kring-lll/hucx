"""Strict, streaming evidence checks for MPI windows and original OSU RMA."""
import math
import json
from pathlib import Path
import re

SIZES = (8, 65536, 64, 16385, 4096, 8193, 8, 65536)
BENCHMARKS = {
    'osu_put_latency': ('one-sided/osu_put_latency', 'OSU MPI_Put', 'latency_us', 'put'),
    'osu_get_latency': ('one-sided/osu_get_latency', 'OSU MPI_Get', 'latency_us', 'get'),
    'osu_put_bw': ('one-sided/osu_put_bw', 'OSU MPI_Put', 'bandwidth_MB_s', 'put'),
    'osu_get_bw': ('one-sided/osu_get_bw', 'OSU MPI_Get', 'bandwidth_MB_s', 'get'),
}
ERROR = re.compile(r'UCX\s+(ERROR|FATAL)|Assertion.*failed|Caught signal|'
                   r'MPI_RMA_(ERROR|MISMATCH)|OSU_PROOF_ERROR|MPI_ERR_|'
                   r'DATA VALIDATION ERROR', re.I)


def probe_output(text, sync):
    if ERROR.search(text):
        raise ValueError('MPI validation/runtime error')
    if sync == 'atomics':
        rows = re.findall(r'^MPI_RMA_AMO_PASS rank=([01]) FAA32=PASS FAA64=PASS '
                          r'CAS32=PASS CAS64=PASS CAS_MISS=PASS GUARDS=PASS$', text, re.M)
        if sorted(rows) != ['0', '1']:
            raise ValueError('missing/duplicate atomic validation result')
        return {'FAA32': True, 'FAA64': True, 'CAS32': True, 'CAS64': True,
                'compare_miss': True, 'guards': True}
    expression = (r'^MPI_RMA_ROUND rank=(\d+) generation=(\d+) round=(\d+) '
                  r'op=(PUT|GET) bytes=(\d+) origin=(\d+) sync=' + re.escape(sync) +
                  r' DATA=PASS GUARDS=PASS$')
    actual = re.findall(expression, text, re.M)
    expected = [(str(rank), str(generation), str(round_id),
                 'PUT' if round_id < 8 else 'GET', str(SIZES[round_id % 8]),
                 str(generation)) for rank in range(2) for generation in range(2)
                for round_id in range(16)]
    if sorted(actual) != sorted(expected) or text.count('MPI_RMA_ROUND ') != 64:
        raise ValueError('missing/duplicate/mismatched MPI data round')
    for rank in range(2):
        own = [(row[1], row[2]) for row in actual if row[0] == str(rank)]
        if own != [(str(g), str(r)) for g in range(2) for r in range(16)]:
            raise ValueError('out-of-order per-rank MPI rounds')
    final = re.findall(r'^MPI_RMA_DATA_PASS rank=([01]) sync=' + re.escape(sync) +
                       r' ROUNDS=32 WINDOWS=2$', text, re.M)
    if sorted(final) != ['0', '1']:
        raise ValueError('MPI window cleanup/result incomplete')
    return {'rounds_per_rank': 32, 'window_generations': 2,
            'directions': 2, 'data': True, 'full_guards': True}


def osu_output(text, benchmark, size):
    _, header, metric, _ = BENCHMARKS[benchmark]
    if (header not in text or not re.search(r'(?<!\d)7\.5\.2(?!\d)', text) or
            ERROR.search(text) or re.search(r'\b(?:Fail|Failed)\b', text)):
        raise ValueError('OSU RMA version/header/runtime error')
    expected_unit = 'Latency (us)' if metric == 'latency_us' else 'Bandwidth (MB/s)'
    if expected_unit not in text:
        raise ValueError('OSU RMA metric header mismatch')
    rows = [line.split() for line in text.splitlines() if re.match(r'^\s*\d+\s+', line)]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][0] != str(size):
        raise ValueError('missing/extra/duplicate OSU RMA row')
    value = float(rows[0][1])
    if not math.isfinite(value) or value <= 0:
        raise ValueError('nonpositive/nonfinite OSU RMA metric')
    return {metric: value, 'raw_row': ' '.join(rows[0]),
            'OSU_builtin_data_validation': False,
            'correctness_source': 'separate guarded MPI window probe'}


def scan_ucx(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing/nonregular actual application UCX log')
    operations = {}
    sw_amo = False
    rkey_protocols = {}
    with path.open(errors='replace') as stream:
        for line in stream:
            if ERROR.search(line):
                raise ValueError('UCX runtime error: ' + line[:350])
            match = re.search(r'RM native RMA post kind=(\w+) length=(\d+)', line)
            if match:
                name, size = match[1], int(match[2])
                if name not in ('put_short', 'put_bcopy', 'put_zcopy', 'get_bcopy', 'get_zcopy'):
                    raise ValueError('unknown native RMA operation')
                if not 1 <= size <= 4096:
                    raise ValueError('native WR length outside validated 1..4096')
                operations[name] = operations.get(name, 0) + 1
            key = re.search(r'\brkey (0x[0-9a-f]+) ep .*?\b(basic_rma|sw_rma): lane\[', line)
            if key:
                rkey_protocols.setdefault(key[1], []).append(key[2])
            sw_amo |= bool(re.search(r'\bsw_amo:\s+lane\[', line))
    return {'operations': operations, 'rkey_protocols': rkey_protocols, 'sw_amo_rkey_selected': sw_amo,
            'bytes': path.stat().st_size}


def require_paths(scans, kinds=(), atomic=False):
    observed = {name for scan in scans for name in scan['operations']}
    for kind in kinds:
        if not any(name == kind or name.startswith(kind + '_') for name in observed):
            raise ValueError('native path not observed: ' + kind)
    if atomic and not any(scan['sw_amo_rkey_selected'] for scan in scans):
        raise ValueError('software AMO rkey path not observed')


def correlate_mpi_data(path, scan, rank, pid, expected_kinds):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing actual MPI-to-UCP diagnostic log')
    rows = []
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            operation = row['ucp_op']
            if (row['rank'] != rank or row['pid'] != pid or row['target'] != 1-rank or
                    row['mpi_op'] != operation.upper() or operation not in ('put', 'get') or
                    not isinstance(row['length'], int) or row['length'] <= 0 or
                    row['status'] not in (0, 1)):
                raise ValueError('MPI-to-UCP record identity/operation/status mismatch')
            protocols = scan['rkey_protocols'].get(row['rkey'], [])
            if not protocols or set(protocols) != {'basic_rma'}:
                raise ValueError('MPI business rkey native RMA not proved; initialization posts insufficient')
            rows.append(row)
    if not rows or not set(expected_kinds) <= {row['ucp_op'] for row in rows}:
        raise ValueError('requested MPI business PUT/GET path not observed')
    return {'calls': len(rows), 'operations': sorted({row['ucp_op'] for row in rows}),
            'bytes': sum(row['length'] for row in rows), 'all_business_rkeys_basic_rma': True}
