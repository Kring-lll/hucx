#!/usr/bin/env python3
"""One original-OSU recheck with split logs, never rewrite the failed receipt."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

import run_rndv as am

window = am.window
candidate = am.candidate
osu = am.osu
CASE = candidate.B_ROOT / 'r1_shared_c7ef02b51_vkSZ69'
COMMIT = 'c7ef02b51d274e4ff3151457b82cc34f493419d5'
MODULE = 'cc12a6ed9299f87f36f927b9d4f8f8043810ec3273a96a55734a4cc89b2848f2'
CHECKPOINT = CASE / 'pending_order_accept_b7mudi0j'


def read_json(path):
    return json.loads(Path(path).read_text())


def normalized(value):
    return json.loads(json.dumps(value))


def check_checkpoint(root=CHECKPOINT, case=CASE):
    root, case = Path(root), Path(case)
    if root.parent != case:
        raise ValueError('checkpoint is not a child of this fixed CASE')
    report = read_json(root / 'results.json')
    if (report['source_commit'] != COMMIT or report['module_sha256'] != MODULE
            or report['status'] != 'STOPPED_INCOMPLETE'):
        raise ValueError('checkpoint source/module/state differs')
    normal = report['normal_regression']
    if normal['cases'] != 28 or not normal['passed']:
        raise ValueError('prior normal regression did not pass')
    expected_paths = (Path(normal['results_json']), Path(report['boundary_matrix']))
    for path in expected_paths:
        if not path.is_relative_to(case):
            raise ValueError('prior result path is outside this CASE')
    normal_rows = read_json(expected_paths[0])['cases']
    boundary_rows = read_json(expected_paths[1])['cases']
    if (len(normal_rows) != 28 or not all(row['passed'] for row in normal_rows)
            or len(boundary_rows) != 18 or not all(row['passed'] for row in boundary_rows)):
        raise ValueError('prior normal/boundary raw result coverage differs')
    timing = read_json(root / 'comparison/comparison_trials.json')
    if (len(timing) != 15 or not all(row['passed'] for row in timing)
            or any(sum(row['comparison_label'] == label for row in timing) != 5
                   for label in ('old_rm', 'new_rm', 'rc'))):
        raise ValueError('prior five-round timing coverage differs')
    matrix = read_json(root / 'window_and_rndv/results.json')
    if matrix['source_commit'] != COMMIT or matrix['module_sha256'] != MODULE:
        raise ValueError('matrix identity differs')
    rows = matrix['cases']
    tasks = [('WINDOW', item) for item in window.cases()]
    tasks += [('AM_RNDV', item) for item in am.cases()]
    if len(rows) != 20:
        raise ValueError('prior matrix is not the expected 20-task checkpoint')
    for index, (row, (family, item)) in enumerate(zip(rows, tasks)):
        expected = am.trial_directory(root / 'window_and_rndv', item, index).name
        if row['name'] != expected or row['family'] != family or row['exit'] != 0:
            raise ValueError('prior task name/family/exit differs')
        if normalized(row['matrix_case']) != normalized(item):
            raise ValueError('prior pinned matrix workload differs')
        if index < 19 and not row['passed']:
            raise ValueError('an earlier matrix task failed')
    last = rows[-1]
    if (last['passed'] or 'missing/extra/duplicate/out-of-order OSU size row' not in last['error']
            or last.get('cleanup_error')
            or any(value.get('terminated_own_pids') for value in last['cleanup'].values())):
        raise ValueError('last task does not match the isolated output-parser failure')
    path = root / 'results.json'
    return {'source_result': str(path), 'source_result_sha256': osu.digest(path),
            'matrix_result_sha256': osu.digest(root / 'window_and_rndv/results.json'),
            'normal_cases': 28, 'boundary_cases': 18, 'prior_matrix_passed': 19,
            'timing_trials': 15, 'last_trial_was_exit0_parser_failure': True}


def plan():
    return {'product_commit': COMMIT, 'module_sha256': MODULE, 'case': str(CASE),
            'checkpoint': str(CHECKPOINT), 'new_mpi_tasks': 1,
            'benchmark': 'original OSU7.5.2 osu_multi_lat', 'ppn': 4,
            'message_bytes': 65536, 'iterations': 20, 'warmup': 2,
            'timeout': 240, 'product_changed': False, 'rebuild_required': False,
            'full_B_acceptance': False, 'performance_accepted': False}


def main():
    parser = argparse.ArgumentParser(description='只补跑PPN4/64KB一项，UCX按实际进程分文件日志；默认仅计划')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may run this recheck')
    build = candidate.validate_build(CASE)
    if build['source_commit'] != COMMIT or build['module_sha256'] != MODULE:
        raise ValueError('fixed c7ef product identity differs')
    checkpoint = check_checkpoint()
    out = Path(tempfile.mkdtemp(prefix='am_log_recheck_', dir=CASE))
    report = {'plan': plan(), 'checkpoint': checkpoint, 'status': 'RUNNING',
              'performance_accepted': False}
    print('AM_LOG_RECHECK_DIR=' + str(out), flush=True)
    try:
        print('检查点=PASS 28正常/18边界/19矩阵项/15计时保留；本轮只新增1个MPI任务', flush=True)
        with candidate.bind_candidate(osu, CASE, build), window.benchmark_registry():
            osu.validate_package()
            commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                            (mode + '_run.json'), CASE / 'install')
                        for mode in ('rm', 'rc')}
            evidence = osu.preflight(out, commands)
            if min(item['available_cores'] for item in evidence['inventory']) < 4:
                raise ValueError('fewer than four allowed cores on a node')
            report['preflight'] = evidence
            window.check_help(out)
            item = am.cases()[-1]
            env = osu.environment(commands['rm'][1], 'iface', CASE / 'install', osu.budget(4))
            env.update(am.settings(item))
            report['defaults'] = candidate.default_capabilities(CASE / 'install', env, out)
            report['am_only_capabilities'] = {
                host: am.check_am_only_capability(
                    (out / (host + '_ucx_device.txt')).read_text(), am.parse_capability)
                for host in ('node89', 'node97')}
            library = window.prepare_helpers(out, report)
            trial_args = argparse.Namespace(iterations=20, warmup=2, timeout=240)
            with am.trial_profile(osu, item, am.window_matrix.parse_output,
                                  log_directory=am.trial_directory(out, item, 0)):
                with window.traced_environment(library):
                    row = osu.one_trial(out, commands, evidence, 'osu_multi_lat', 'rm',
                                        4, 'validation', 0, trial_args, message_bytes=65536)
            window.finish_trial(out, row, item, library)
            am.finish_protocol(out, row, item, evidence)
            report['trial'] = row
            if not row['passed']:
                raise ValueError(row['name'] + ': ' + row.get('error', 'recheck failed'))
            report['status'] = 'PRIOR_GATES_AND_FOCUSED_AM_RECHECK_PASS_PERFORMANCE_REVIEW_REQUIRED'
            print('OSU_MULTI_LAT RM PPN=4 SIZE=65536 DATA/MAPS/EXIT=PASS ACTUAL_RANKS=8/8', flush=True)
            print('UCX_LOGS=8/8 分进程保存；AM_PATH=PASS SEND_MAX=%dB' %
                  max(value['max_send_bytes'] for value in row['send_observations']), flush=True)
            print('既有窗口10项/AM前9项＋本轮最后1项=PASS；原始失败记录保留', flush=True)
            print('诊断/校验开启，不采用本轮时延作性能结论；完整B/C/原生RMA未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('AM_LOG_RECHECK_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        osu.base.write_json(out / 'results.json', report)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
