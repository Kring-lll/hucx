"""Bounded original-OMB three-transport comparison; pure evidence rules."""
import csv
import json
import math
import re
import statistics

PROFILES = ('rc', 'rm', 'um')
MODES = {'rc': 2, 'rm': 1, 'um': 4}
BENCHMARKS = {
    'osu_multi_lat': ('pt2pt/osu_multi_lat', 'OSU MPI Multi Latency Test', 'multi latency'),
    'osu_alltoall': ('collective/osu_alltoall', 'All-to-All Personalized Exchange Latency Test', 'alltoall'),
    'osu_bw': ('pt2pt/osu_bw', 'OSU MPI Bandwidth Test', 'bandwidth'),
    'osu_bibw': ('pt2pt/osu_bibw', 'OSU MPI Bi-Directional Bandwidth Test', 'bidirectional bandwidth'),
}
GROUPS = (
    ('osu_multi_lat', 1, 8), ('osu_multi_lat', 4, 8),
    ('osu_alltoall', 1, 8), ('osu_alltoall', 4, 8),
    ('osu_bw', 1, 65536), ('osu_bibw', 1, 65536),
)


def plan(timing=True):
    return {'profiles': PROFILES, 'groups': GROUPS, 'gate_mpi': 3,
            'comparison_mpi': 126 if timing else 18,
            'validation': 18, 'timing': 90 if timing else 0,
            'resources': 18 if timing else 0, 'max_ppn': 4,
            'product_rebuild': False, 'standard_osu': '7.5.2',
            'native_read_write': 'PAUSED', 'fault_injection': False,
            'segment_policy': 'transport defaults, captured not equalized'}


def schedule(timing):
    for group, (benchmark, ppn, size) in enumerate(GROUPS):
        for profile in PROFILES:
            yield group, benchmark, ppn, size, 'validation', 0, profile
        if timing:
            for repeat in range(5):
                ordered = PROFILES[repeat % 3:] + PROFILES[:repeat % 3]
                for profile in ordered:
                    yield group, benchmark, ppn, size, 'timing', repeat, profile
            for profile in PROFILES:
                yield group, benchmark, ppn, size, 'resources', 0, profile


def arguments(benchmark, iterations, warmup, validation, message_bytes=8):
    if benchmark not in BENCHMARKS or message_bytes not in (8, 65536):
        raise ValueError('unplanned OSU workload')
    result = ['--message-size', '%d:%d' % (message_bytes, message_bytes),
              '--iterations', str(iterations), '--warmup', str(warmup)]
    if benchmark == 'osu_alltoall':
        result.append('--full')
    if benchmark in ('osu_bw', 'osu_bibw'):
        result += ['--window-size', '8', '--buffer-num', 'multiple']
    if validation:
        result.append('--validation')
    return result


def parse_output(text, benchmark, validation, iterations, message_bytes=8):
    if BENCHMARKS[benchmark][1] not in text or not re.search(r'(?<!\d)7\.5\.2(?!\d)', text):
        raise ValueError('original OSU header/version mismatch')
    if re.search(r'UCX\s+(ERROR|FATAL)|DATA VALIDATION ERROR|OSU_PROOF_ERROR|Caught signal|MPI_ERR_|\bFail(?:ed)?\b', text, re.I):
        raise ValueError('OSU/runtime failure')
    rows = [s.split() for s in text.splitlines() if re.match(r'^\s*\d+\s+', s)]
    count = (5 if benchmark == 'osu_alltoall' else 2) + int(validation)
    if len(rows) != 1 or len(rows[0]) != count or rows[0][0] != str(message_bytes):
        raise ValueError('missing/extra/duplicate or malformed OSU result')
    row = rows[0]
    value = float(row[1])
    if not math.isfinite(value) or value <= 0 or (validation and row[-1] != 'Pass'):
        raise ValueError('invalid measurement or missing validation Pass')
    result = {'value': value, 'unit': 'MB/s' if benchmark in ('osu_bw', 'osu_bibw') else 'us',
              'validation_enabled': validation, 'validation_pass': validation,
              'raw_row': ' '.join(row)}
    if benchmark == 'osu_alltoall':
        lower, upper = map(float, row[2:4])
        if not all(math.isfinite(v) and v >= 0 for v in (lower, upper)) or not lower <= value <= upper or int(row[4]) != iterations:
            raise ValueError('OSU rank range/iterations mismatch')
        result.update(rank_min_us=lower, rank_max_us=upper)
    return result


def inspect_trial(row, profile, phase):
    if not row.get('passed') or row.get('exit') != 0 or row.get('cleanup_error'):
        raise ValueError(row.get('error', 'OSU/rank/cleanup gate failed'))
    config = row['configuration']
    env = config['environment']
    expected = 'rc_urma,um_urma:aux,self' if profile == 'rc' else profile + '_urma,self'
    if (env['UCX_TLS'] != expected or env['UCX_PROTO_ENABLE'] != 'n'
            or env['UCX_RNDV_THRESH'] != 'inf' or env['UCX_ZCOPY_THRESH'] != 'inf'
            or env['UCX_LOG_LEVEL'] != 'warn' or config['resource_probe'] != (phase == 'resources')):
        raise ValueError('transport/path/diagnostic configuration mismatch')
    if any(k in env for k in ('HUCX_UM_SEND_FAULT', 'HUCX_UM_SEND_MODULE')):
        raise ValueError('fault observer present')
    preload = env.get('LD_PRELOAD', '')
    if any(s in preload for s in ('libum_send_probe', 'libsend_probe', 'liburma_resource_observer', 'failure.so')):
        raise ValueError('diagnostic observer leaked into base preload')
    if phase == 'validation' and not row['output']['validation_pass']:
        raise ValueError('missing OSU data validation')


def summarize(rows, group):
    benchmark, ppn, size = GROUPS[group]
    result = {'benchmark': benchmark, 'ppn': ppn, 'message_bytes': size, 'profiles': {}}
    for profile in PROFILES:
        selected = [r for r in rows if r['group'] == group and r['profile'] == profile]
        timing = sorted((r for r in selected if r['phase'] == 'timing'), key=lambda r: r['repeat'])
        valid = [r for r in selected if r['phase'] == 'validation']
        resource = [r for r in selected if r['phase'] == 'resources']
        if (len(selected) != 7 or len(valid) != 1 or len(resource) != 1
                or [r['repeat'] for r in timing] != list(range(5))):
            raise ValueError('incomplete comparison group')
        for row in selected:
            inspect_trial(row, profile, row['phase'])
        if any(r['configuration']['arguments'] != resource[0]['configuration']['arguments'] for r in timing):
            raise ValueError('timing and resource workloads differ')
        samples = [r['output']['value'] for r in timing]
        rss = [r['rank_evidence']['before_finalize_rss_sum_mib'] for r in timing]
        peaks = [v['peak_per_process'] for v in resource[0]['jettys'].values()]
        result['profiles'][profile] = {
            'unit': timing[0]['output']['unit'], 'samples': samples,
            'median': statistics.median(samples), 'minimum': min(samples), 'maximum': max(samples),
            'timing_rss_samples_mib': rss, 'timing_rss_median_mib': statistics.median(rss),
            'resource_rss_mib': resource[0]['rank_evidence']['before_finalize_rss_sum_mib'],
            'jetty_peak_range': [min(peaks), max(peaks)],
            'effective_config': resource[0]['effective_config'],
            'resource_trial': resource[0]['name'],
        }
    rm = result['profiles']['rm']
    result['rm_improvement_pct'] = {}
    for reference in ('rc', 'um'):
        other = result['profiles'][reference]['median']
        result['rm_improvement_pct'][reference] = ((other - rm['median']) if rm['unit'] == 'us' else (rm['median'] - other)) / other * 100
    return result


def write_tables(out, report):
    lines = ['# RC / RM / UM 原版 OSU 对照', '',
             '本表只展示完成全部校验、5轮计时及资源取证的组；失败和未完成任务保留在 results.json。',
             '计时关闭故障/WR/Jetty探针；三模式保留相同的MPI Init/Finalize身份记录，不包装通信调用或MPI_Wtime。',
             'RSS：计时轮各rank Finalize前进程RSS之和的中位数，两节点合计MiB，非同步瞬时设备内存。',
             'Jetty：独立资源轮目标传输模式的每rank峰值范围；RC辅助UM不计入RC Jetty。',
             '片长保留各传输默认值，不是强制等片长实验。无丢包恢复性能、硬件故障或全规模验收结论。', '',
             '| OSU | PPN | Bytes | Unit | RC | RM | UM | RM优于RC% | RM优于UM% |',
             '|---|---:|---:|---|---:|---:|---:|---:|---:|']
    csv_rows = []
    for item in report['comparisons']:
        p = item['profiles']
        lines.append('| %s | %d | %d | %s | %.3f | %.3f | %.3f | %+.2f | %+.2f |' % (
            item['benchmark'], item['ppn'], item['message_bytes'], p['rm']['unit'],
            p['rc']['median'], p['rm']['median'], p['um']['median'],
            item['rm_improvement_pct']['rc'], item['rm_improvement_pct']['um']))
        for profile, value in p.items():
            csv_rows.append({'benchmark': item['benchmark'], 'ppn': item['ppn'], 'bytes': item['message_bytes'],
                             'profile': profile, **{k: value[k] for k in ('unit', 'median', 'minimum', 'maximum', 'timing_rss_median_mib', 'resource_rss_mib')},
                             'samples': json.dumps(value['samples']), 'jetty_peak_range': json.dumps(value['jetty_peak_range']),
                             'effective_config': json.dumps(value['effective_config'], sort_keys=True)})
    lines += ['', '| OSU / PPN | 模式 | 五轮样本 | 范围 | RSS MiB | Jetty / rank |', '|---|---|---|---|---:|---|']
    for row in csv_rows:
        lines.append('| %s / %s | %s | %s | %s..%s | %.2f | %s |' % (
            row['benchmark'], row['ppn'], row['profile'], row['samples'], row['minimum'], row['maximum'],
            row['timing_rss_median_mib'], row['jetty_peak_range']))
    (out/'comparison.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    fields = ['benchmark','ppn','bytes','profile','unit','median','minimum','maximum','timing_rss_median_mib','resource_rss_mib','samples','jetty_peak_range','effective_config']
    with (out/'comparison.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(csv_rows)
