"""Strict standard OMB 7.5.2 parsing and observer accounting; no invented data."""
import csv
import json
import math
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'a89'))
from support import check_maps

BENCHMARKS = {
    'osu_multi_lat': ('pt2pt/osu_multi_lat', 'OSU MPI Multi Latency Test', '8B多对平均单向时延'),
    'osu_alltoall': ('collective/osu_alltoall', 'All-to-All Personalized Exchange Latency Test', '8B Alltoall集合平均耗时')}


def parse_output(text, benchmark, validation, iterations):
    if BENCHMARKS[benchmark][1] not in text or not re.search(r'(?<!\d)7\.5\.2(?!\d)', text):
        raise ValueError('OSU benchmark/version header mismatch')
    if re.search(r'UCX\s+(ERROR|FATAL)|OSU_PROOF_ERROR|DATA VALIDATION ERROR|Caught signal|MPI_ERR_|\bFail(?:ed)?\b', text, re.I):
        raise ValueError('terminal/validation error in OSU log')
    candidates = [line.split() for line in text.splitlines() if re.match(r'^\s*8\s+', line)]
    if len(candidates) != 1:
        raise ValueError('one and only one 8B result row is required')
    values = candidates[0]
    count = (6 if validation else 5) if benchmark == 'osu_alltoall' else (3 if validation else 2)
    if len(values) != count:
        raise ValueError('unexpected OSU column count: ' + str(values))
    latency = float(values[1])
    if not math.isfinite(latency) or latency <= 0:
        raise ValueError('OSU latency is not positive finite')
    if validation and values[-1] != 'Pass':
        raise ValueError('OSU validation Pass marker missing')
    result = {'latency_us': latency, 'validation_enabled': validation,
              'validation_pass': validation and values[-1] == 'Pass', 'raw_row': ' '.join(values)}
    if benchmark == 'osu_alltoall':
        minimum, maximum = map(float, values[2:4])
        if (not all(math.isfinite(v) and v >= 0 for v in (minimum, maximum)) or
                not minimum <= latency <= maximum or int(values[4]) != iterations):
            raise ValueError('OSU full statistics/iterations mismatch')
        result.update(rank_min_us=minimum, rank_max_us=maximum, iterations=int(values[4]))
    return result


def inspect_ranks(run, ppn, inventory, install, nonce, mpi_library):
    rows = []
    for rank in range(2 * ppn):
        row = json.loads((run / ('rank%d.json' % rank)).read_text())
        wrapper = json.loads((run / ('pid_rank%d.json' % rank)).read_text())
        host = inventory[rank // ppn]
        if (row['rank'] != rank or row['size'] != 2 * ppn or row['ppn'] != ppn or
                row['hostname'] != host['hostname'] or row['finalized'] is not True or
                row['proof_error'] != 0 or wrapper['rank'] != rank or
                wrapper['nonce'] != nonce or wrapper['hostname'] != host['hostname']):
            raise ValueError('rank/host/finalize/probe identity mismatch: rank ' + str(rank))
        affinity = set(row['cpu_affinity'])
        if (not affinity or affinity != set(wrapper['cpu_affinity']) or
                not affinity <= set(host['allowed_cpus'])):
            raise ValueError('actual OSU application CPU binding mismatch')
        if row['init_rss_kib'] <= 0 or row['before_finalize_rss_kib'] <= 0:
            raise ValueError('RSS evidence missing')
        for phase in ('after_init', 'before_finalize'):
            text = (run / ('rank%d_%s.maps' % (rank, phase))).read_text()
            check_maps(text, install)
            paths = [line.split()[-1] for line in text.splitlines() if re.search(r'/libmpi\.so(?:\.|$)', line)]
            if not paths or any(str(Path(path).resolve()) != mpi_library for path in paths):
                raise ValueError('actual MPI library mapping differs from pinned preflight')
        rows.append(row)
    for host in range(2):
        affinities = [set(row['cpu_affinity']) for row in rows[host * ppn:(host + 1) * ppn]]
        if any(a & b for i, a in enumerate(affinities) for b in affinities[i + 1:]):
            raise ValueError('OSU rank CPU binding overlap')
    return {'before_finalize_rss_sum_mib': sum(row['before_finalize_rss_kib'] for row in rows) / 1024,
            'init_rss_sum_mib': sum(row['init_rss_kib'] for row in rows) / 1024,
            'pids': {row['rank']: row['pid'] for row in rows}}


def inspect_jettys(run, ppn, mode, expected_pids, depth):
    results = {}
    for path in sorted(run.glob('rank*_pid*.jsonl')):
        match = re.fullmatch(r'rank(\d+)_pid(\d+)\.jsonl', path.name)
        if match is None:
            raise ValueError('invalid observer filename')
        rank, pid = map(int, match.groups())
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        starts = [row for row in rows if row.get('event') == 'start']
        if len(starts) != 1 or starts[0]['rank'] != rank or expected_pids.get(rank) != pid or rank in results:
            raise ValueError('observer rank/PID/duplicate mismatch')
        live = {}
        counts = {}
        peaks = {}
        peak_process = 0
        depths = set()
        for row in rows:
            if row.get('valid', 1) != 1:
                raise ValueError('observer reports invalid state')
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
                peak_process = max(peak_process, len(live))
                depths.add(row['depth'])
            elif row['event'] == 'release' and pointer in live:
                counts[live.pop(pointer)] -= 1
        if not peaks or live or depths != {depth}:
            raise ValueError('missing Jetty/wrong TX depth/leak: rank ' + str(rank))
        if mode == 1 and any(peak != 1 for peak in peaks.values()):
            raise ValueError('shared RM Jetty peak is not one per context')
        results[rank] = {'pid': pid, 'peak_per_process': peak_process,
                         'peak_per_context': peaks, 'live_at_exit': 0, 'tx_depth': depth}
    if set(results) != set(range(2 * ppn)):
        raise ValueError('observer rank coverage incomplete')
    return results


def stats(values):
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError('incomplete/nonfinite positive samples')
    return {'samples': values, 'median': statistics.median(values),
            'minimum': min(values), 'maximum': max(values)}


def compare(trials, benchmark, ppn, repeats):
    result = {'benchmark': benchmark, 'ppn': ppn, 'message_bytes': 8, 'modes': {}}
    for mode in ('rc', 'rm'):
        selected = [r for r in trials if r['benchmark'] == benchmark and r['ppn'] == ppn and r['mode'] == mode]
        timings = sorted([r for r in selected if r['phase'] == 'timing'], key=lambda r: r['repeat'])
        validations = [r for r in selected if r['phase'] == 'validation']
        probes = [r for r in selected if r['phase'] == 'resources']
        if (len(timings) != repeats or [r['repeat'] for r in timings] != list(range(repeats)) or
                len(validations) != 1 or len(probes) != 1 or not all(r['passed'] for r in selected)):
            raise ValueError('comparison requires all timing/validation/resource trials')
        if (not validations[0]['output']['validation_pass'] or
                probes[0]['output']['validation_enabled'] or
                any(row['configuration']['arguments'] != probes[0]['configuration']['arguments'] for row in timings)):
            raise ValueError('comparison lacks validation or timing/resource arguments differ')
        resources = probes[0]['jettys']
        result['modes'][mode] = {
            'latency_us': stats([r['output']['latency_us'] for r in timings]),
            'before_finalize_rss_sum_mib': stats([r['rank_evidence']['before_finalize_rss_sum_mib'] for r in timings]),
            'jetty_process_peak_range': [min(r['peak_per_process'] for r in resources.values()),
                                        max(r['peak_per_process'] for r in resources.values())],
            'jetty_context_peak_values': sorted({v for r in resources.values() for v in r['peak_per_context'].values()}),
            'resource_trial': probes[0]['name']}
    rc = result['modes']['rc']['latency_us']['median']
    rm = result['modes']['rm']['latency_us']['median']
    result['rm_latency_reduction_pct'] = (rc - rm) / rc * 100
    return result


def write_tables(out, report):
    all_rows = []
    lines = ['# 标准OSU 7.5.2 新RM/RC对照', '',
             'OSU程序未修改；数据校验、无URMA观察器计时、Jetty观察分别运行。',
             '身份探针只包装MPI_Init/Init_thread/Finalize，在OSU计时循环之外保存maps和RSS；不包装数据操作或MPI_Wtime。',
             'Jetty为目标RM/RC模式每进程峰值的全rank范围；UM辅助Jetty未混入。',
             'RSS为计时轮各进程MPI_Finalize前RSS之和的多轮中位数（两节点合计MiB，非同步瞬时总量），不是Jetty/设备独占内存。', '']
    for benchmark in BENCHMARKS:
        lines += ['## ' + benchmark + ' — ' + BENCHMARKS[benchmark][2], '',
                  '| PPN | RC/μs | RM/μs | 时延降低/% | RC Jetty/进程 | RM Jetty/进程 | RC RSS/MiB | RM RSS/MiB |',
                  '|---:|---:|---:|---:|---|---|---:|---:|']
        for row in report['comparisons']:
            if row['benchmark'] != benchmark:
                continue
            rc, rm = row['modes']['rc'], row['modes']['rm']
            values = [row['ppn'], rc['latency_us']['median'], rm['latency_us']['median'],
                      row['rm_latency_reduction_pct'], str(rc['jetty_process_peak_range']),
                      str(rm['jetty_process_peak_range']), rc['before_finalize_rss_sum_mib']['median'],
                      rm['before_finalize_rss_sum_mib']['median']]
            lines.append('| %d | %.3f | %.3f | %+.2f | %s | %s | %.2f | %.2f |' % tuple(values))
            all_rows.append([benchmark] + values)
        lines.append('')
    lines += ['变化正数表示RM耗时更低；负数和所有异常样本完整保留。多对时延与集合耗时不能混算。',
              '当前状态：' + report['status'], '产品传输库保持节点已测2a8e，C重传延期；本报告不自动验收A/B/C全部需求。']
    if report.get('error'):
        lines += ['', '停止原因：' + report['error'][:1000]]
    (out / 'comparison.md').write_text('\n'.join(lines) + '\n')
    with (out / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['OSU用例', 'PPN', 'RC耗时us', 'RM耗时us', 'RM耗时降低%',
                         'RC每进程Jetty峰值范围', 'RM每进程Jetty峰值范围',
                         'RC两节点Finalize前RSS合计MiB', 'RM两节点Finalize前RSS合计MiB'])
        writer.writerows(all_rows)
