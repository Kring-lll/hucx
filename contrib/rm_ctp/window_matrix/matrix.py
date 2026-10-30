"""Bounded original OMB 7.5.2 window/sweep cases and strict output checks."""
from contextlib import contextmanager
import math
import re

SOURCE_BASE = '4a151952ae070c096ec19d6ac7c843fc9b172b57'
PRODUCT_COMMIT = '80b28a25b5ea0e245bca88db44be79d15d06127f'
PRODUCT_MODULE = '8d9ed4133c71c4ca77ad900e60433e9781cfef2adf33dc396f3f642b2cc9821c'
SWEEP = tuple(8 * 2 ** index for index in range(12))
BENCHMARKS = {
    'osu_bw': ('pt2pt/osu_bw', 'OSU MPI Bandwidth Test', '单向窗口带宽'),
    'osu_bibw': ('pt2pt/osu_bibw', 'OSU MPI Bi-Directional Bandwidth Test', '双向窗口带宽'),
    'osu_multi_lat': ('pt2pt/osu_multi_lat', 'OSU MPI Multi Latency Test', '多对时延'),
    'osu_alltoall': ('collective/osu_alltoall', 'All-to-All Personalized Exchange Latency Test', '集合耗时')}


def cases():
    result = []
    for benchmark in ('osu_bw', 'osu_bibw'):
        result.append({'benchmark': benchmark, 'mode': 'rc', 'ppn': 1,
                       'sizes': (8,), 'window': 8, 'purpose': 'RC_8B_GATE'})
    for benchmark in ('osu_bw', 'osu_bibw'):
        result.append({'benchmark': benchmark, 'mode': 'rm', 'ppn': 1,
                       'sizes': SWEEP, 'window': 8, 'purpose': 'ONE_MPI_SIZE_SWEEP'})
        for size in (4086, 8147):
            result.append({'benchmark': benchmark, 'mode': 'rm', 'ppn': 1,
                           'sizes': (size,), 'window': 32, 'purpose': 'NONALIGNED_WINDOW'})
    for benchmark in ('osu_multi_lat', 'osu_alltoall'):
        result.append({'benchmark': benchmark, 'mode': 'rm', 'ppn': 4,
                       'sizes': SWEEP, 'window': None, 'purpose': 'MULTIRANK_SIZE_SWEEP'})
    return result


def arguments(case, iterations, warmup):
    sizes = tuple(case['sizes'])
    if sizes != SWEEP and sizes not in ((8,), (4086,), (8147,)):
        raise ValueError('unsupported size sequence')
    if case['benchmark'] not in BENCHMARKS or iterations != 100 or warmup != 10:
        raise ValueError('functional matrix must keep its pinned workload')
    value = '%d:%d' % (sizes[0], sizes[-1])
    if len(sizes) > 1:
        value += ':2'
    result = ['--message-size', value, '--iterations', str(iterations),
              '--warmup', str(warmup), '--validation']
    if case['benchmark'] in ('osu_bw', 'osu_bibw'):
        if case['ppn'] != 1 or case['window'] not in (8, 32):
            raise ValueError('bandwidth cases require two ranks and bounded windows')
        result += ['--window-size', str(case['window']), '--buffer-num', 'multiple']
    elif case['ppn'] != 4 or case['window'] is not None:
        raise ValueError('multi-rank cases must use PPN4 without bandwidth options')
    if case['benchmark'] == 'osu_alltoall':
        result.append('--full')
    return result


def parse_output(text, case, iterations):
    benchmark = case['benchmark']
    if (BENCHMARKS[benchmark][1] not in text
            or not re.search(r'(?<!\d)7\.5\.2(?!\d)', text)
            or not re.search(r'#\s*Datatype:\s*MPI_CHAR\.', text)):
        raise ValueError('original OSU benchmark/version/datatype mismatch')
    if re.search(r'UCX\s+(ERROR|FATAL)|DATA VALIDATION ERROR|Caught signal|MPI_ERR_|'
                 r'OSU_PROOF_ERROR|\bFail(?:ed)?\b', text, re.I):
        raise ValueError('OSU/runtime reports a failure')
    rows = [line.split() for line in text.splitlines() if re.match(r'^\s*\d+\s+', line)]
    if [int(row[0]) for row in rows] != list(case['sizes']):
        raise ValueError('missing/extra/duplicate/out-of-order OSU size row')
    result = []
    for row in rows:
        expected_columns = 6 if benchmark == 'osu_alltoall' else 3
        if len(row) != expected_columns or row[-1] != 'Pass':
            raise ValueError('missing validation Pass or wrong column count')
        value = float(row[1])
        if not math.isfinite(value) or value <= 0:
            raise ValueError('invalid OSU numeric result')
        if benchmark == 'osu_alltoall':
            lower, upper = map(float, row[2:4])
            if (not all(math.isfinite(v) and v >= 0 for v in (lower, upper))
                    or not lower <= value <= upper or int(row[4]) != iterations):
                raise ValueError('Alltoall full statistics/iterations mismatch')
        result.append({'size_bytes': int(row[0]), 'value': value, 'validation': 'Pass',
                       'unit': 'MB/s' if benchmark in ('osu_bw', 'osu_bibw') else 'us',
                       'raw_row': ' '.join(row)})
    return {'rows': result, 'validation_pass': True, 'performance_accepted': False,
            'size_order': 'ASCENDING_NOT_ALTERNATING' if len(rows) > 1 else 'FIXED'}


@contextmanager
def adapt_trial(osu, case):
    """Reuse launcher, maps, rank/PID and cleanup; replace only OSU argv/parser."""
    previous = osu.arguments, osu.parse_output

    def make_arguments(benchmark, iterations, warmup, validation, message_bytes=8):
        if benchmark != case['benchmark'] or not validation:
            raise ValueError('this package only permits the selected validation case')
        return arguments(case, iterations, warmup)

    def parse(text, benchmark, validation, iterations, message_bytes=8):
        if benchmark != case['benchmark'] or not validation:
            raise ValueError('unexpected benchmark or phase')
        return parse_output(text, case, iterations)

    osu.arguments = make_arguments
    osu.parse_output = parse
    try:
        yield
    finally:
        osu.arguments, osu.parse_output = previous
