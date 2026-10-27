#!/usr/bin/env python3
"""RM-only standard OSU expansion, gated by observed PPN20 comparisons.

No high-scale RC launch is possible in this entry point. XX means unmeasured.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

import analysis
import baselines
import run as runner
from support import DEFAULT_CASE, LAB, PRODUCT_COMMIT, PRODUCT_MODULE, levels, saved_spec


def planned_levels(capacity, requested):
    if not 21 <= requested <= 192:
        raise ValueError('RM target PPN must be 21..192')
    return levels(capacity, requested)


def summarize_rm(rows, benchmark, ppn):
    selected = [r for r in rows if r['benchmark'] == benchmark and r['ppn'] == ppn]
    if any(r['mode'] != 'rm' or r['passed'] is not True for r in selected):
        raise ValueError('RM-only expansion contains RC or a failed sample')
    timings = sorted([r for r in selected if r['phase'] == 'timing'], key=lambda r: r['repeat'])
    checked = [r for r in selected if r['phase'] == 'validation']
    probes = [r for r in selected if r['phase'] == 'resources']
    if (len(timings) != 5 or [r['repeat'] for r in timings] != list(range(5)) or
            len(checked) != 1 or len(probes) != 1 or not checked[0]['output']['validation_pass']):
        raise ValueError('RM-only expansion sample coverage incomplete')
    probe = probes[0]
    if probe['output']['validation_enabled'] or any(
            r['configuration']['arguments'] != probe['configuration']['arguments'] or r['output']['validation_enabled']
            for r in timings):
        raise ValueError('RM timing/resource configuration mismatch')
    resources = probe['jettys']
    return {'benchmark': benchmark, 'ppn': ppn, 'message_bytes': 8,
            'modes': {'rm': {'latency_us': analysis.stats([r['output']['latency_us'] for r in timings]),
                'before_finalize_rss_sum_mib': analysis.stats([r['rank_evidence']['before_finalize_rss_sum_mib'] for r in timings]),
                'jetty_process_peak_range': [min(r['peak_per_process'] for r in resources.values()),
                                            max(r['peak_per_process'] for r in resources.values())],
                'jetty_context_peak_values': sorted({v for r in resources.values() for v in r['peak_per_context'].values()}),
                'resource_trial': probe['name']}},
            'rm_latency_reduction_pct': None, 'rc_status': 'XX_UNMEASURED_NO_SAME_SCALE_BASELINE'}


def write_tables(out, report):
    lines = ['# 标准OSU对照及RM扩展', '',
             '8B；各档5轮无URMA观察器计时中位数；多对单向时延与Alltoall集合耗时分表。',
             'Jetty为目标模式每进程同时存活峰值范围；RSS为各rank Finalize前RSS之和的多轮中位数，两节点合计MiB（非同步瞬时总量）。',
             '**XX：本轮未做同规模RC对照，不代表RC已运行失败；无基线时不计算相对收益。**',
             'PPN1与PPN8/16/20来自两个原始批次，高规模来自本次扩展；不混合重算历史中位数。', '']
    csv_rows = []
    for benchmark in analysis.BENCHMARKS:
        lines += ['## ' + benchmark, '',
                  '| PPN | RC/μs | RM/μs | RM耗时降低/% | RC Jetty/进程 | RM Jetty/进程 | RC RSS/MiB | RM RSS/MiB |',
                  '|---:|---:|---:|---:|---|---|---:|---:|']
        for row in sorted(report['comparisons'] + report['rm_levels'], key=lambda r: r['ppn']):
            if row['benchmark'] != benchmark:
                continue
            rm = row['modes']['rm']
            rc = row['modes'].get('rc')
            values = [row['ppn'], '%.3f' % rc['latency_us']['median'] if rc else 'XX',
                      '%.3f' % rm['latency_us']['median'],
                      '%+.2f' % row['rm_latency_reduction_pct'] if rc else 'XX',
                      str(rc['jetty_process_peak_range']) if rc else 'XX', str(rm['jetty_process_peak_range']),
                      '%.2f' % rc['before_finalize_rss_sum_mib']['median'] if rc else 'XX',
                      '%.2f' % rm['before_finalize_rss_sum_mib']['median']]
            lines.append('| ' + ' | '.join(map(str, values)) + ' |')
            csv_rows.append([benchmark] + values)
        lines.append('')
    lines += ['最高完整通过PPN（两个用例共同通过）：' + str(report['highest_common_passed_ppn']),
              '各用例最高完整通过PPN：' + json.dumps(report['highest_by_benchmark']),
              '状态：' + report['status'], '最高已通过规模不等于协议硬上限；到192通过也只称已验证至192。',
              '产品仍已测2a8e，重传C延期；本表不自动关闭A/B/C全部需求。']
    if report.get('error'):
        lines += ['', '停止原因：' + report['error'][:1000]]
    (out / 'comparison.md').write_text('\n'.join(lines) + '\n')
    with (out / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['OSU用例','PPN','RC耗时us','RM耗时us','RM耗时降低%',
                         'RC每进程Jetty峰值范围','RM每进程Jetty峰值范围',
                         'RC两节点Finalize前RSS合计MiB','RM两节点Finalize前RSS合计MiB'])
        writer.writerows(csv_rows)


def run_level(out, commands, evidence, ppn, args, report, save, execute=runner.one_trial):
    for benchmark in analysis.BENCHMARKS:
        schedule = [('validation', 0)] + [('timing', i) for i in range(5)] + [('resources', 0)]
        for phase, repeat in schedule:
            row = execute(out, commands, evidence, benchmark, 'rm', ppn, phase, repeat, args)
            report['trials'].append(row)
            save()
            if not row['passed']:
                raise RuntimeError('RM扩展停止：' + row['name'] + '；' +
                                   row.get('error', row.get('cleanup_error', 'unknown'))[:600])
        summary = summarize_rm(report['trials'], benchmark, ppn)
        summary['source_report'] = str(out / 'results.json')
        report['rm_levels'].append(summary)
        report['highest_by_benchmark'][benchmark] = ppn
        save()
        mode = summary['modes']['rm']
        print('%s RM_ONLY PPN=%d 8B=%.3fus JETTY_PER_PROCESS=%s RSS=%.2fMiB DATA=PASS EXIT=PASS RC=XX' %
              (benchmark, ppn, mode['latency_us']['median'], mode['jetty_process_peak_range'],
               mode['before_finalize_rss_sum_mib']['median']), flush=True)
    report['highest_common_passed_ppn'] = ppn
    save()


def main():
    parser = argparse.ArgumentParser(description='标准OSU仅RM扩展；默认仅复核历史并合表，不启动MPI')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true', help='逐级运行仅RM扩展，会使用高规模双节点资源')
    action.add_argument('--merge-only', action='store_true', help='只复核两个原始批次并汇总完整PPN1/8/16/20表')
    action.add_argument('--plan', action='store_true', help='只校验本地工具及计划，不SSH/不启动MPI')
    parser.add_argument('--max-ppn', type=int, default=192)
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    if not 21 <= args.max_ppn <= 192 or args.timeout < 180:
        parser.error('max-ppn须21..192，timeout至少180秒')
    manifest = runner.validate_package()
    if args.plan:
        print(json.dumps({'mode': 'RM_ONLY', 'rc_launched': False,
            'requested_max_ppn': args.max_ppn, 'ppn_if_capacity_192': planned_levels(192, args.max_ppn),
            'benchmarks': list(analysis.BENCHMARKS), 'message_bytes': 8, 'timing_repeats': 5,
            'product_commit': PRODUCT_COMMIT, 'module_sha256': PRODUCT_MODULE,
            'node_tests': 'NOT_RUN'}, ensure_ascii=False, indent=2))
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise ValueError('本工具应由root在node89执行')
    out = Path(tempfile.mkdtemp(prefix='osu_rm_scale_', dir=DEFAULT_CASE))
    print('OSU_RM_SCALE_DIR=' + str(out), flush=True)
    report = {'manifest': manifest, 'status': 'CHECKING_BASELINES', 'comparisons': [],
              'rm_levels': [], 'trials': [], 'highest_common_passed_ppn': 0,
              'highest_by_benchmark': {b: 0 for b in analysis.BENCHMARKS},
              'full_feature_acceptance': False, 'reliability_C': 'DEFERRED',
              'started': time.strftime('%F %T %Z')}

    def save():
        runner.base.write_json(out / 'results.json', report)
        write_tables(out, report)

    save()
    try:
        old = baselines.load_baselines(DEFAULT_CASE, manifest)
        report['comparisons'] = old['comparisons']
        report['baseline_sources'] = old['sources']
        report['highest_common_passed_ppn'] = 20
        report['highest_by_benchmark'] = {b: 20 for b in analysis.BENCHMARKS}
        report['status'] = 'HISTORICAL_OSU_TABLES_REVALIDATED'
        save()
        print('原始OSU证据复核=通过 PPN=1/8/16/20；未重跑、未混算历史样本', flush=True)
        for row in report['comparisons']:
            rc, rm = row['modes']['rc']['latency_us'], row['modes']['rm']['latency_us']
            print('%s PPN=%d RC中位数[范围]=%.3f[%.3f,%.3f] RM=%.3f[%.3f,%.3f]us' %
                  (row['benchmark'],row['ppn'],rc['median'],rc['minimum'],rc['maximum'],
                   rm['median'],rm['minimum'],rm['maximum']), flush=True)
        if args.run:
            commands = {kind: saved_spec(LAB / 'osu_minimal8_ff_14ns8' / (kind + '_run.json'),
                                         DEFAULT_CASE / 'install') for kind in ('rm', 'rc')}
            current = runner.preflight(out, commands)
            baselines.assert_same_runtime(old['preflight'], current)
            report['preflight'] = current
            capacity = min(row['available_cores'] for row in current['inventory'])
            requested = planned_levels(capacity, args.max_ppn)
            if not requested:
                raise ValueError('no allowed physical-core capacity above PPN20')
            report['requested_max_ppn'] = args.max_ppn
            report['actual_capacity_ppn'] = capacity
            report['actual_plan'] = requested
            report['target_clipped_to_capacity'] = requested[-1] < args.max_ppn
            args.iterations, args.warmup = 5000, 1000
            shutil.copy2(runner.ROOT / 'osu_rank.py', out / 'osu_rank.py')
            shutil.copy2(runner.A89 / 'rank_exec.py', out / 'a89_rank_exec.py')
            runner.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
            for path in (out / 'osu_rank.py', out / 'a89_rank_exec.py', out / 'libosu_identity.so'):
                runner.base.copy_remote([path], str(out) + '/')
                if runner.base.ssh('sha256sum "$1"\n', path).split()[0] != runner.digest(path):
                    raise ValueError('peer helper binary/hash mismatch')
            report['status'] = 'RM_ONLY_RUNNING'
            save()
            print('RM_ONLY计划=' + str(requested) + '；高规模不启动RC，CPU许可不等于空闲', flush=True)
            for ppn in requested:
                run_level(out, commands, current, ppn, args, report, save)
            report['status'] = 'REQUESTED_RM_OSU_LEVELS_PASS_REVIEW_SAMPLES'
            save()
        else:
            print('MERGE_ONLY=YES；本次无SSH/编译/新MPI任务，高规模尚未测', flush=True)
        print('HIGHEST_COMMON_PASSED_PPN=' + str(report['highest_common_passed_ppn']), flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
        print('TABLE_MD=' + str(out / 'comparison.md'), flush=True)
        print('TABLE_CSV=' + str(out / 'comparison.csv'), flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        print('RM_OSU_STOPPED=' + report['error'][:800], file=sys.stderr, flush=True)
        print('HIGHEST_COMMON_PASSED_PPN=' + str(report['highest_common_passed_ppn']), file=sys.stderr, flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
