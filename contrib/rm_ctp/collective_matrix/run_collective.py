#!/usr/bin/env python3
"""Original OSU normal collective regression on the already tested c7ef install."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

from coll_matrix import (BENCHMARKS, ITERATIONS, PRODUCT_COMMIT, PRODUCT_MODULE,
                         SOURCE_BASE, WARMUP, cases, check_config, profile)

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'am_rndv'))
import run_rndv as am
from split_logs import collect, log_pattern

window = am.window
candidate = am.candidate
osu = am.osu
CASE = candidate.B_ROOT / 'r1_shared_c7ef02b51_vkSZ69'


def plan():
    return {'source_base': SOURCE_BASE, 'product_commit': PRODUCT_COMMIT,
            'module_sha256': PRODUCT_MODULE, 'case_dir': str(CASE),
            'standard_osu': '7.5.2', 'cases': cases(), 'task_count': 10,
            'size_rows': sum(len(case['sizes']) for case in cases()),
            'max_ppn': 4, 'max_message_bytes': 65536, 'iterations': ITERATIONS,
            'warmup': WARMUP, 'product_unchanged': True, 'rebuild_required': False,
            'size_alternation': 'NOT_COVERED_ORIGINAL_OSU_ASCENDING_LOOP',
            'actual_collective_request_path_proof': 'NOT_CLAIMED_CONFIGURATION_IS_NOT_PROOF',
            'full_B_acceptance': False, 'performance_accepted': False,
            'native_RMA_accepted': False, 'C_retry': 'DEFERRED'}


@contextmanager
def registry():
    previous = osu.BENCHMARKS
    osu.BENCHMARKS = BENCHMARKS
    try:
        yield
    finally:
        osu.BENCHMARKS = previous


def check_help(out):
    for benchmark in BENCHMARKS:
        text = (Path(out) / (benchmark + '_help.txt')).read_text()
        if any(option not in text for option in
               ('--full', '--message-size', '--iterations', '--warmup', '--validation')):
            raise ValueError('required original OSU collective options missing: ' + benchmark)


def finish(out, row, case, evidence, library):
    run = Path(out) / row['name']
    window.finish_trial(out, row, case, library)
    try:
        if not row['passed']:
            raise ValueError(row.get('error', 'data/maps/defaults/exit gate failed'))
        row['protocol_configuration'] = {
            host: check_config((run / (host + '_ucx_config.txt')).read_text(), case,
                               candidate.bytes_value) for host in ('node89', 'node97')}
        _, row['diagnostic_log_files'] = collect(out, row, evidence, osu.base)
        row['actual_request_path_proof'] = 'NOT_CLAIMED'
        if len(row['diagnostic_log_files']) != 2 * case['ppn']:
            raise ValueError('actual process log coverage differs')
    except Exception as exc:
        row['passed'] = False
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    osu.base.write_json(run / 'trial.json', row)
    return row


def main():
    parser = argparse.ArgumentParser(description='原版OSU集合大消息正常矩阵，默认仅计划')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 600:
        parser.error('每任务超时须60..600秒')
    if not (args.run or args.preflight_only):
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may start this matrix')
    build = candidate.validate_build(CASE)
    if build['source_commit'] != PRODUCT_COMMIT or build['module_sha256'] != PRODUCT_MODULE:
        raise ValueError('fixed node-tested product identity differs')
    out = Path(tempfile.mkdtemp(prefix='b_collective_', dir=CASE))
    report = {'plan': plan(), 'cases': [], 'status': 'PREFLIGHT',
              'performance_accepted': False, 'full_B_acceptance': False}
    print('B_COLLECTIVE_DIR=' + str(out), flush=True)

    def save():
        osu.base.write_json(out / 'results.json', report)

    save()
    try:
        with candidate.bind_candidate(osu, CASE, build), registry():
            osu.validate_package()
            commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                            (mode + '_run.json'), CASE / 'install')
                        for mode in ('rm', 'rc')}
            evidence = osu.preflight(out, commands)
            report['preflight'] = evidence
            check_help(out)
            if min(item['available_cores'] for item in evidence['inventory']) < 4:
                raise ValueError('fewer than four allowed physical cores on a node')
            env = osu.environment(commands['rm'][1], 'iface', CASE / 'install', osu.budget(4))
            report['defaults'] = candidate.default_capabilities(CASE / 'install', env, out)
            report['am_only_capabilities'] = {
                host: am.check_am_only_capability(
                    (out / (host + '_ucx_device.txt')).read_text(), am.parse_capability)
                for host in ('node89', 'node97')}
            print('预检=PASS 原版OSU/双端库/4KB默认；原生RMA与AM_ZCOPY关闭；最高PPN4', flush=True)
            if not args.run:
                report['status'] = 'PREFLIGHT_PASS_NO_TWO_NODE_MPI'
                return
            library = window.prepare_helpers(out, report)
            trial_args = argparse.Namespace(iterations=ITERATIONS, warmup=WARMUP, timeout=args.timeout)
            report['status'] = 'RUNNING'
            for index, case in enumerate(cases()):
                with profile(osu, case, out, index, log_pattern):
                    if case['mode'] == 'rm':
                        with window.traced_environment(library):
                            row = osu.one_trial(out, commands, evidence, case['benchmark'],
                                                'rm', case['ppn'], 'validation', index, trial_args,
                                                message_bytes=case['sizes'][0])
                    else:
                        row = osu.one_trial(out, commands, evidence, case['benchmark'],
                                            'rc', case['ppn'], 'validation', index, trial_args,
                                            message_bytes=case['sizes'][0])
                finish(out, row, case, evidence, library)
                report['cases'].append(row)
                save()
                if not row['passed']:
                    raise ValueError(row['name'] + ': ' + row.get('error', 'collective gate failed'))
                maximum = max((value['max_send_bytes'] for value in row.get('send_observations', [])), default=0)
                print('OSU=%s MODE=%s PPN=%d SIZE=%d..%d ROWS=%d DATA/MAPS/EXIT=PASS '
                      'SEND_MAX=%s' % (case['benchmark'], case['mode'].upper(), case['ppn'],
                                      case['sizes'][0], case['sizes'][-1], len(case['sizes']),
                                      str(maximum) + 'B' if maximum else '未观察'), flush=True)
            report['status'] = 'SELECTED_COLLECTIVE_NORMAL_MATRIX_PASS'
            report['validated_size_rows'] = sum(len(row['output']['rows']) for row in report['cases'])
            print('集合正常矩阵=10/10 PASS；递增扫描不是严格大小交替', flush=True)
            print('配置不等于实际协议路径证明；本轮不作性能结论，完整B/C/RMA仍未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('B_COLLECTIVE_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        save()
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
