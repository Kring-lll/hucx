"""Mentor-confirmed scope: three two-rank OMBs; Alltoall alone scales."""
import csv
import json
import math
import statistics

import policy

LEVELS = (1, 8, 16, 20, 24, 32, 48, 64, 96, 128, 160)
PROFILES = policy.PROFILES
BENCHMARKS = {k: v for k, v in policy.BENCHMARKS.items() if k != 'osu_multi_lat'}
BENCHMARKS['osu_latency'] = ('pt2pt/osu_latency', 'OSU MPI Latency Test', 'two-rank latency')
GROUPS = (('osu_latency', 1, 8), ('osu_bw', 1, 65536), ('osu_bibw', 1, 65536)) + tuple(
    ('osu_alltoall', n, 8) for n in LEVELS)


def profiles(ppn):
    return PROFILES if ppn <= 24 else ('rm', 'um')


def tasks(timing=True, repeats=5):
    for group, (benchmark, ppn, size) in enumerate(GROUPS):
        modes = profiles(ppn)
        for profile in modes:
            yield group, benchmark, ppn, size, 'validation', 0, profile
        if timing:
            for repeat in range(repeats):
                shift = repeat % len(modes)
                for profile in modes[shift:] + modes[:shift]:
                    yield group, benchmark, ppn, size, 'timing', repeat, profile
            for profile in modes:
                yield group, benchmark, ppn, size, 'resources', 0, profile


def plan(timing=True):
    rows = list(tasks(timing))
    return {'groups': GROUPS, 'profiles': PROFILES, 'gate_mpi': 3,
            'comparison_mpi': len(rows), 'total_mpi': len(rows) + 3,
            'validation': sum(r[4] == 'validation' for r in rows),
            'timing': sum(r[4] == 'timing' for r in rows),
            'resources': sum(r[4] == 'resources' for r in rows),
            'timing_repeats': 5 if timing else 0, 'max_ppn': 160, 'rc_max_ppn': 24,
            'p2p_total_ranks': 2, 'alltoall_total_ranks': '2*PPN',
            'rc_omissions': [{'benchmark': 'osu_alltoall', 'ppn': p, 'profile': 'rc',
                              'status': 'NOT_PLANNED_RC_LIMIT_24'} for p in LEVELS if p > 24],
            'standard_osu': '7.5.2', 'product_rebuild': False,
            'product_changes': False, 'native_read_write': 'PAUSED',
            'psn_simplification': 'NOT_IN_THIS_STAGE', 'fault_injection': False,
            'queue_policy': 'fixed TX64/RX512, RM/UM window32/timeout10s; no automatic tuning',
            'segment_policy': 'RM4K, RC/UM8K defaults; captured not equalized',
            'failure_policy': 'stop entire batch; retain partial results; never substitute retries',
            'prior_samples_merged': False}


def arguments(benchmark, iterations, warmup, validation, message_bytes=8):
    if benchmark not in BENCHMARKS:
        raise ValueError('unplanned scale benchmark')
    # Reuse exact accepted OMB options; latency uses the same standard options.
    if benchmark == 'osu_latency':
        result = ['--message-size', '%d:%d' % (message_bytes, message_bytes),
                  '--iterations', str(iterations), '--warmup', str(warmup)]
        if message_bytes != 8:
            raise ValueError('latency size is fixed at 8B in this stage')
        if validation:
            result.append('--validation')
        return result
    return policy.arguments(benchmark, iterations, warmup, validation, message_bytes)


def parse_output(text, benchmark, validation, iterations, message_bytes=8):
    # policy parser resolves its module BENCHMARKS; avoid mutating the old policy
    # because the pinned prior-checkpoint recomputation still uses that policy.
    if benchmark == 'osu_latency':
        if BENCHMARKS[benchmark][1] not in text:
            raise ValueError('original osu_latency header missing')
        return policy.parse_output(text.replace('OSU MPI Latency Test',
                                   'OSU MPI Multi Latency Test'), 'osu_multi_lat',
                                   validation, iterations, message_bytes)
    return policy.parse_output(text, benchmark, validation, iterations, message_bytes)


def budget(ppn):
    if ppn not in LEVELS:
        raise ValueError('PPN outside planned levels')
    return {'ppn': ppn, 'tx_depth': 64, 'rx_depth': 512, 'window': 32}


def capacity(inventory, ppn, disks):
    """Host safety guard, not a claim about device TP/Jetty capacity."""
    budget(ppn)
    if len(inventory) != 2 or len(disks) != 2:
        raise ValueError('capacity evidence needs both nodes')
    required_mem = ppn * 128 * 1024 + 1024 * 1024
    required_fd = 4 * ppn + 256
    checks = []
    for row, disk in zip(inventory, disks):
        problems = []
        if row['hostname'] != disk['hostname']:
            raise ValueError('disk/inventory host mismatch')
        if row['available_cores'] < ppn:
            problems.append('physical CPUs')
        if row['mem_available_kib'] < required_mem:
            problems.append('available memory')
        for key in ('nofile_soft', 'nproc_soft'):
            if row[key] != -1 and row[key] < required_fd:
                problems.append(key)
        if disk['free_bytes'] < 8 * 1024**3:
            problems.append('free experiment disk <8GiB')
        checks.append({'hostname': row['hostname'], 'ppn': ppn,
                       'memory_floor_kib': required_mem, 'fd_nproc_floor': required_fd,
                       'resource_floor_is_estimate': True, 'inventory': row, 'disk': disk,
                       'problems': problems})
    return {'passed': not any(r['problems'] for r in checks), 'nodes': checks,
            'scope': 'CPU/memory/rlimit/disk staging only; device capacity is not inferred'}


def summarize(rows, group, repeats=5):
    benchmark, ppn, size = GROUPS[group]
    # Generalize the accepted summarizer to the modes actually planned at PPN.
    result = {'benchmark': benchmark, 'ppn': ppn, 'message_bytes': size, 'profiles': {}}
    expected_profiles = profiles(ppn)
    selected_group = [r for r in rows if r['group'] == group]
    if len(selected_group) != len(expected_profiles) * (repeats + 2):
        raise ValueError('incomplete/extra scale group')
    for profile in expected_profiles:
        selected = [r for r in selected_group if r['profile'] == profile]
        timings = sorted((r for r in selected if r['phase'] == 'timing'), key=lambda r: r['repeat'])
        valid = [r for r in selected if r['phase'] == 'validation']
        resource = [r for r in selected if r['phase'] == 'resources']
        if len(valid) != 1 or len(resource) != 1 or [r['repeat'] for r in timings] != list(range(repeats)):
            raise ValueError('scale group phase/repeat mismatch')
        for row in selected:
            policy.inspect_trial(row, profile, row['phase'])
            if (row['benchmark'], row['ppn'], row['message_bytes']) != (benchmark, ppn, size):
                raise ValueError('scale trial workload mismatch')
        if any(r['configuration']['arguments'] != resource[0]['configuration']['arguments'] for r in timings):
            raise ValueError('resource/timing arguments differ')
        values = [r['output']['value'] for r in timings]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError('nonfinite/nonpositive scale sample')
        peaks = [r['peak_per_process'] for r in resource[0]['jettys'].values()]
        if len(peaks) != 2 * ppn:
            raise ValueError('scale Jetty rank coverage differs')
        rss = [r['rank_evidence']['before_finalize_rss_sum_mib'] for r in timings]
        result['profiles'][profile] = {
            'unit': timings[0]['output']['unit'], 'samples': values,
            'median': statistics.median(values), 'mean': statistics.mean(values),
            'minimum': min(values), 'maximum': max(values), 'stdev': statistics.stdev(values),
            'timing_rss_samples_mib': rss, 'timing_rss_median_mib': statistics.median(rss),
            'resource_rss_mib': resource[0]['rank_evidence']['before_finalize_rss_sum_mib'],
            'jetty_peak_range': [min(peaks), max(peaks)],
            'effective_config': resource[0]['effective_config'],
            'resource_trial': resource[0]['name']}
    result['rm_improvement_pct'] = {}
    rm = result['profiles']['rm']
    for profile in ('rc', 'um'):
        if profile in result['profiles']:
            reference = result['profiles'][profile]['median']
            delta = reference - rm['median'] if rm['unit'] == 'us' else rm['median'] - reference
            result['rm_improvement_pct'][profile] = delta / reference * 100
    if ppn > 24:
        result['rc_status'] = 'NOT_PLANNED_RC_LIMIT_24'
    return result


def write_tables(out, report):
    lines = ['# 原版 OSU：点对点与 Alltoall 规模对照', '',
             'latency/bw/bibw 均为两节点各一进程；仅 Alltoall 按每节点PPN扩展，总进程2×PPN。',
             'RC最高PPN24是本轮约定范围，超过24为未计划，不是实测失败或零性能。',
             '片长RM4K、RC/UM8K；TX64/RX512，RM/UM窗口32、超时10s。',
             '独立数据校验、五轮轮换计时、独立资源轮；全部样本保留，不合并旧批次。',
             '计时无WR/故障/资源探针，仅相同Init/Finalize身份取证；不包装数据调用和MPI_Wtime。',
             'RSS是两节点各rank Finalize前进程RSS合计，非同步瞬时内存，不等于设备内存。',
             'Jetty是资源轮每rank目标模式峰值范围，RC辅助UM不混计。',
             '负载记录覆盖整个启动器区间，不能排除OSU计时区间的短时干扰。', '',
             '| OSU | PPN | Bytes | Unit | RC | RM | UM | RM优于RC% | RM优于UM% |',
             '|---|---:|---:|---|---:|---:|---:|---:|---:|']
    csv_rows = []
    for item in report['comparisons']:
        profiles_ = item['profiles']
        def cell(profile):
            return '%.3f' % profiles_[profile]['median'] if profile in profiles_ else 'NOT_PLANNED'
        delta = item['rm_improvement_pct']
        lines.append('| %s | %d | %d | %s | %s | %s | %s | %s | %+.2f |' % (
            item['benchmark'], item['ppn'], item['message_bytes'], profiles_['rm']['unit'],
            cell('rc'), cell('rm'), cell('um'),
            '%+.2f' % delta['rc'] if 'rc' in delta else 'N/A', delta['um']))
        for profile, value in profiles_.items():
            csv_rows.append({'benchmark': item['benchmark'], 'ppn': item['ppn'],
                             'bytes': item['message_bytes'], 'profile': profile, 'status': 'COMPLETE',
                             **{k: value[k] for k in ('unit', 'median', 'mean', 'minimum', 'maximum', 'stdev',
                                'timing_rss_median_mib', 'resource_rss_mib')},
                             **{k: json.dumps(value[k], sort_keys=True) for k in
                                ('samples', 'jetty_peak_range', 'effective_config')}})
    for row in report['plan']['rc_omissions']:
        csv_rows.append({'benchmark': row['benchmark'], 'ppn': row['ppn'], 'bytes': 8,
                         'profile': 'rc', 'status': row['status']})
    lines += ['', '| OSU / PPN | 模式 | 全部计时样本 | RSS MiB中位数 | Jetty/rank |',
              '|---|---|---|---:|---|']
    for item in csv_rows:
        if item['status'] == 'COMPLETE':
            lines.append('| %s / %d | %s | %s | %.2f | %s |' % (
                item['benchmark'], item['ppn'], item['profile'], item['samples'],
                item['timing_rss_median_mib'], item['jetty_peak_range']))
    lines += ['', '状态：' + report['status'], '只汇总完整组；部分成功及失败见results.json。正常SEND测量不等于故障恢复性能或完整需求验收。']
    if report.get('error'):
        lines.append('停止原因：' + report['error'][:1500])
    (out/'comparison.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    fields = ['benchmark', 'ppn', 'bytes', 'profile', 'status', 'unit', 'median', 'mean',
              'minimum', 'maximum', 'stdev', 'samples', 'timing_rss_median_mib',
              'resource_rss_mib', 'jetty_peak_range', 'effective_config']
    with (out/'comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_rows)
