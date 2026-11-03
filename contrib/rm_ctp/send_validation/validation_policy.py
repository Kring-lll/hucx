"""Pure workload, parsing and comparison rules for accepted reliable SEND."""
import math
import re
import statistics

BENCHMARKS = {
    'osu_multi_lat': ('pt2pt/osu_multi_lat', 'OSU MPI Multi Latency Test', 'us'),
    'osu_alltoall': ('collective/osu_alltoall', 'All-to-All Personalized Exchange Latency Test', 'us'),
    'osu_allgather': ('collective/osu_allgather', 'Allgather Latency Test', 'us'),
    'osu_bw': ('pt2pt/osu_bw', 'OSU MPI Bandwidth Test', 'MB/s'),
    'osu_bibw': ('pt2pt/osu_bibw', 'OSU MPI Bi-Directional Bandwidth Test', 'MB/s'),
}
MODES = ('rc', 'iface_v2', 'reliable_v3')
REPEATS = 5


def functional_cases():
    rows = []
    for benchmark in ('osu_alltoall', 'osu_allgather'):
        rows.append(dict(benchmark=benchmark, profile='rc', ppn=1, size=8, path='eager'))
        for ppn in (1, 2, 4):
            for size in (8, 3982, 65536):
                rows.append(dict(benchmark=benchmark, profile='reliable_v3', ppn=ppn,
                                 size=size, path='eager'))
        rows.append(dict(benchmark=benchmark, profile='reliable_v3', ppn=4,
                         size=16385, path='am_rndv'))
    return rows


def timing_cases():
    return [dict(benchmark=name, ppn=ppn, size=8, path='eager')
            for name in ('osu_multi_lat', 'osu_alltoall') for ppn in (1, 4)] + [
        dict(benchmark=name, ppn=1, size=65536, path='eager')
        for name in ('osu_bw', 'osu_bibw')]


def failure_cases():
    return [dict(limit=limit, fault=fault) for limit in ('retry_limit', 'deadline')
            for fault in ('none', 'drop_data', 'drop_ack')]


def plan():
    return dict(product='97b50328718c3c7c1b613c2ee09a90c198666142', max_ppn=4,
                collective_tasks=len(functional_cases()), original_osu_injection_tasks=1,
                direct_uct_exit_tasks=len(failure_cases()),
                timing_groups=len(timing_cases()), timing_repeats=REPEATS,
                performance_mpi_tasks=len(timing_cases()) * len(MODES) * (REPEATS + 2),
                timing='opt-in --timing after all functional gates pass',
                default_action='PLAN_ONLY', node_acceptance='PENDING',
                hardware_fault_recovery=False, default_product_mode_changed=False)


def arguments(benchmark, iterations, warmup, validation, message_bytes=8):
    if benchmark not in BENCHMARKS:
        raise ValueError('unknown original OSU program')
    result = ['--message-size', '%d:%d' % (message_bytes, message_bytes),
              '--iterations', str(iterations), '--warmup', str(warmup)]
    if benchmark in ('osu_alltoall', 'osu_allgather'):
        result.append('--full')
    if benchmark in ('osu_bw', 'osu_bibw'):
        result += ['--window-size', '8']
    if validation:
        result.append('--validation')
    return result


def parse_output(text, benchmark, validation, iterations, message_bytes=8):
    if (BENCHMARKS[benchmark][1] not in text or
            not re.search(r'(?<!\d)7\.5\.2(?!\d)', text) or
            not re.search(r'#\s*Datatype:\s*MPI_CHAR\.', text)):
        raise ValueError('original OSU program/version/datatype mismatch')
    if re.search(r'UCX\s+(ERROR|FATAL)|DATA VALIDATION ERROR|Caught signal|MPI_ERR_|'
                 r'OSU_PROOF_ERROR|Assertion.*failed|\bFail(?:ed)?\b', text, re.I):
        raise ValueError('OSU/runtime error in benchmark output')
    rows = [line.split() for line in text.splitlines() if re.match(r'^\s*\d+\s+', line)]
    collective = benchmark in ('osu_alltoall', 'osu_allgather')
    columns = (5 if collective else 2) + int(validation)
    if len(rows) != 1 or len(rows[0]) != columns or rows[0][0] != str(message_bytes):
        raise ValueError('missing/extra/duplicate/malformed OSU size row')
    row = rows[0]
    value = float(row[1])
    if not math.isfinite(value) or value <= 0 or (validation and row[-1] != 'Pass'):
        raise ValueError('nonpositive/nonfinite result or validation failed')
    result = dict(value=value, unit=BENCHMARKS[benchmark][2], raw_row=' '.join(row),
                  validation_enabled=validation, validation_pass=validation)
    if collective:
        lower, upper = map(float, row[2:4])
        if not (math.isfinite(lower) and math.isfinite(upper) and 0 <= lower <= value <= upper
                and int(row[4]) == iterations):
            raise ValueError('collective full statistics/iterations differ')
        result.update(rank_min_us=lower, rank_max_us=upper)
    return result


def summary(values):
    if not values or not all(math.isfinite(v) and v > 0 for v in values):
        raise ValueError('missing/nonfinite samples')
    return dict(samples=values, median=statistics.median(values), minimum=min(values), maximum=max(values))


def compare(trials, case):
    result = dict(case, profiles={})
    for profile in MODES:
        chosen = [r for r in trials if r['profile'] == profile and
                  all(r[k] == case[k] for k in ('benchmark', 'ppn', 'size'))]
        timing = sorted((r for r in chosen if r['phase'] == 'timing'), key=lambda r:r['repeat'])
        validation = [r for r in chosen if r['phase'] == 'validation']
        resource = [r for r in chosen if r['phase'] == 'resources']
        if (len(timing) != REPEATS or [r['repeat'] for r in timing] != list(range(REPEATS)) or
                len(validation) != 1 or len(resource) != 1 or not all(r['passed'] for r in chosen)
                or not validation[0]['output']['validation_pass']):
            raise ValueError('incomplete validation/timing/resource group')
        if any(r['configuration']['arguments'] != resource[0]['configuration']['arguments'] for r in timing):
            raise ValueError('timing and resource workloads differ')
        jettys = resource[0]['jettys'].values()
        result['profiles'][profile] = dict(
            metric=summary([r['output']['value'] for r in timing]),
            rss_mib=summary([r['rank_evidence']['before_finalize_rss_sum_mib'] for r in timing]),
            resource_rss_mib=resource[0]['rank_evidence']['before_finalize_rss_sum_mib'],
            jetty_peak_range=[min(r['peak_per_process'] for r in jettys),
                              max(r['peak_per_process'] for r in resource[0]['jettys'].values())],
            resource_trial=resource[0]['run'])
    reliable = result['profiles']['reliable_v3']
    result['reliable_changes_pct'] = {}
    for baseline in ('rc', 'iface_v2'):
        other = result['profiles'][baseline]
        result['reliable_changes_pct'][baseline] = {
            'metric': (reliable['metric']['median'] / other['metric']['median'] - 1) * 100,
            'rss': (reliable['rss_mib']['median'] / other['rss_mib']['median'] - 1) * 100}
    return result
