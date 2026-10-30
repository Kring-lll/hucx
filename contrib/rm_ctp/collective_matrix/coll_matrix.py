"""Bounded, unmodified OMB collectives: size sweep is not alternation."""
from contextlib import contextmanager
import math
from pathlib import Path
import re

SOURCE_BASE = 'e272050e17bfcc8923eceaf08f1f3a2747849d12'
PRODUCT_COMMIT = 'c7ef02b51d274e4ff3151457b82cc34f493419d5'
PRODUCT_MODULE = 'cc12a6ed9299f87f36f927b9d4f8f8043810ec3273a96a55734a4cc89b2848f2'
ITERATIONS = 20
WARMUP = 2
SWEEP = tuple(8 * 2 ** index for index in range(14))
BENCHMARKS = {
    'osu_alltoall': ('collective/osu_alltoall',
                     'All-to-All Personalized Exchange Latency Test', 'Alltoall'),
    'osu_allgather': ('collective/osu_allgather',
                      'Allgather Latency Test', 'Allgather')}


def cases():
    rows = []
    for benchmark in BENCHMARKS:
        for mode in ('rc', 'rm'):
            rows.append({'benchmark': benchmark, 'mode': mode, 'ppn': 1,
                         'sizes': (8,), 'threshold': 16384, 'purpose': '8B_GATE'})
    for benchmark in BENCHMARKS:
        for sizes, threshold, purpose in (
                (SWEEP, 16384, 'ASCENDING_SMALL_TO_LARGE'),
                ((16385,), 16384, 'NONALIGNED_LARGE'),
                ((65536,), None, 'EAGER_CONFIGURATION_CONTROL')):
            rows.append({'benchmark': benchmark, 'mode': 'rm', 'ppn': 4,
                         'sizes': sizes, 'threshold': threshold, 'purpose': purpose})
    return rows


def arguments(case, iterations=ITERATIONS, warmup=WARMUP):
    if case not in cases() or iterations != ITERATIONS or warmup != WARMUP:
        raise ValueError('workload is outside the pinned collective matrix')
    sizes = case['sizes']
    interval = '%d:%d' % (sizes[0], sizes[-1])
    if len(sizes) > 1:
        interval += ':2'
    return ['--message-size', interval, '--iterations', str(iterations),
            '--warmup', str(warmup), '--validation', '--full']


def settings(case):
    threshold = str(case['threshold']) if case['threshold'] else 'inf'
    return {'UCX_RNDV_THRESH': threshold, 'UCX_RNDV_SEND_NBR_THRESH': threshold,
            'UCX_RNDV_SCHEME': 'am', 'UCX_ZCOPY_THRESH': 'inf',
            'UCX_PROTO_ENABLE': 'n', 'UCX_LOG_LEVEL': 'info'}


def check_config(text, case, bytes_value):
    parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
    for key, expected in settings(case).items():
        actual = parsed.get(key, '')
        same = actual.lower() == expected.lower()
        if key in ('UCX_RNDV_THRESH', 'UCX_RNDV_SEND_NBR_THRESH') and expected != 'inf':
            same = bytes_value(actual) == int(expected)
        if not same:
            raise ValueError('effective collective configuration differs: ' + key + '=' + actual)
    return {key: parsed[key] for key in settings(case)}


def parse_output(text, case, iterations):
    if case not in cases() or iterations != ITERATIONS:
        raise ValueError('unknown collective output source')
    if (BENCHMARKS[case['benchmark']][1] not in text
            or not re.search(r'(?<!\d)7\.5\.2(?!\d)', text)
            or not re.search(r'#\s*Datatype:\s*MPI_CHAR\.', text)):
        raise ValueError('OSU benchmark/version/datatype differs')
    if re.search(r'UCX\s+(ERROR|FATAL)|DATA VALIDATION ERROR|Caught signal|MPI_ERR_|'
                 r'OSU_PROOF_ERROR|Assertion.*failed|\bFail(?:ed)?\b', text, re.I):
        raise ValueError('OSU or runtime reported a failure')
    rows = [line.split() for line in text.splitlines() if re.match(r'^\s*\d+\s+', line)]
    if [int(row[0]) for row in rows] != list(case['sizes']):
        raise ValueError('missing/extra/duplicate/out-of-order OSU size row')
    result = []
    for row in rows:
        if len(row) != 6 or row[-1] != 'Pass' or int(row[4]) != iterations:
            raise ValueError('OSU full statistics/iterations/validation differ')
        average, lower, upper = map(float, row[1:4])
        if (not all(math.isfinite(value) and value >= 0 for value in (average, lower, upper))
                or average <= 0 or not lower <= average <= upper):
            raise ValueError('invalid OSU collective statistics')
        result.append({'size_bytes': int(row[0]), 'value': average, 'unit': 'us',
                       'validation': 'Pass', 'raw_row': ' '.join(row)})
    return {'rows': result, 'validation_pass': True, 'performance_accepted': False,
            'size_order': 'ASCENDING_NOT_ALTERNATING' if len(rows) > 1 else 'FIXED'}


def trial_directory(out, case, index):
    name = '%s_%s_p%d_validation_%d' % (case['benchmark'], case['mode'], case['ppn'], index)
    if case['sizes'][0] != 8:
        name += '_b%d' % case['sizes'][0]
    return Path(out) / name


@contextmanager
def profile(osu, case, out, index, log_pattern):
    previous = osu.arguments, osu.parse_output, osu.environment

    def make_arguments(benchmark, iterations, warmup, validation, message_bytes=8):
        if benchmark != case['benchmark'] or not validation or message_bytes != case['sizes'][0]:
            raise ValueError('unexpected collective workload')
        return arguments(case, iterations, warmup)

    def parse(text, benchmark, validation, iterations, message_bytes=8):
        if benchmark != case['benchmark'] or not validation or message_bytes != case['sizes'][0]:
            raise ValueError('unexpected collective output')
        return parse_output(text, case, iterations)

    def environment(*args, **kwargs):
        value = previous[2](*args, **kwargs)
        value.update(settings(case))
        value.update(UCX_LOG_FILE=log_pattern(trial_directory(out, case, index)),
                     UCX_LOG_FILE_SIZE='inf', UCX_LOG_FILE_ROTATE='0')
        return value

    osu.arguments, osu.parse_output, osu.environment = make_arguments, parse, environment
    try:
        yield
    finally:
        osu.arguments, osu.parse_output, osu.environment = previous
