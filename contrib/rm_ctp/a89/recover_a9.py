"""Recover only the observed PPN96 post-MPI scp timeout, then continue A9.

Old reports and rank files remain unchanged. Historical measurements stay in
their original cohort; no new measurement is mixed into their median.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

from artifacts import fetch
from resume import manifest_digest
from support import (A8_LEVELS, DEFAULT_CASE, LAB, MINIMAL, MINIMAL_MODULE,
                     PRE_IFACE, PRE_IFACE_MODULE, PRODUCT_COMMIT, PRODUCT_MODULE,
                     budget, digest, levels, saved_spec, statistics_for)
import run_scale as runner

SOURCE_NAME = 'a89_n8hhrpqz'
SOURCE_TOOLKIT = '2710dfd026d0ceda0051ea04016f1491b55af6f12591744870fb85e26377993a'
FAILED_NAME = 'iface_p96_r0_resources'


def require_clean(row):
    if row.get('cleanup_error') or row.get('exit') != 0:
        raise ValueError('original launcher exit/cleanup not clean: ' + row['name'])
    clean = row['cleanup']
    if set(clean) != {'local', 'peer'} or any(
            value['terminated_own_pids'] for value in clean.values()):
        raise ValueError('original trial left processes: ' + row['name'])


def source_identity(path, case, current, toolkit):
    """No age-based performance claim: source data are explicitly historical."""
    if path.resolve() != case / SOURCE_NAME / 'results.json':
        raise ValueError('only the observed a89_n8hhrpqz collection failure is supported')
    previous = json.loads(path.read_text())
    if (previous['product_commit'] != PRODUCT_COMMIT or
            previous['module_sha256'] != PRODUCT_MODULE or
            manifest_digest(previous['toolkit']) != SOURCE_TOOLKIT):
        raise ValueError('source product/toolkit identity changed')
    for name in ('mpi_scale.c', 'rank_exec.py'):
        if previous['toolkit']['files'][name] != toolkit['files'][name]:
            raise ValueError('benchmark/rank wrapper changed: ' + name)
    if previous['toolkit']['regression_files'] != toolkit['regression_files']:
        raise ValueError('regression tools changed')
    if (previous.get('a8_functional') is not True or
            previous.get('a9_functional') is not False or
            not previous.get('error', '').endswith(
                'A9资源/回收失败，停止扩大：' + FAILED_NAME)):
        raise ValueError('source is not the observed stopped A9 run')
    if len(previous['inventory']) != 2 or len(current['inventory']) != 2:
        raise ValueError('inventory coverage changed')
    for old, new in zip(previous['inventory'], current['inventory']):
        if any(old[key] != new[key] for key in ('hostname', 'boot_id', 'allowed_cpus')):
            raise ValueError('node boot/CPU identity changed; historical continuation rejected')
    for mode in ('candidate', 'old_rm', 'old_minimal_control'):
        if any(previous['installations'][mode][key] != current['installations'][mode][key]
               for key in ('install', 'module_sha256', 'base_libraries_sha256')):
            raise ValueError('library identity changed')
    if previous['saved_launcher_records_sha256'] != current['saved_launcher_records_sha256']:
        raise ValueError('saved launcher changed')
    if previous['observer_identity'] != current['observer_identity']:
        raise ValueError('resource observer changed')
    if digest(path.parent / 'mpi_scale') != previous['binary_sha256']:
        raise ValueError('original binary changed')
    return previous


def validate_history(previous, source, case):
    """Recompute old metrics/resources; failed transfer is never accepted here."""
    markers = ('功能矩阵=通过', 'DIRECT_UCT_FOUR_EP_PASS=YES',
               'A7_NODE_LIFECYCLE_PASS=YES', 'MPI_ADDRESS_PHASE_PASS=YES')
    if len(previous['regressions']) != 4:
        raise ValueError('regression coverage incomplete')
    evidence = {}
    for row, marker in zip(previous['regressions'], markers):
        path = Path(row['log']).resolve()
        if (case not in path.parents or row['passed'] is not True or row['exit'] != 0
                or marker not in path.read_text(errors='replace')):
            raise ValueError('regression evidence missing')
        evidence[str(path)] = digest(path)
    expected = {'%s_p%d_r%d_timing' % (mode, ppn, repeat)
                for ppn in A8_LEVELS for mode in ('iface', 'old_rm', 'rc')
                for repeat in range(3)}
    expected.update('%s_p%d_r0_resources' % (mode, ppn)
                    for ppn in A8_LEVELS for mode in ('iface', 'old_rm', 'rc'))
    expected.update('iface_p%d_r%d_timing' % (ppn, repeat)
                    for ppn in (32, 48, 64, 96) for repeat in range(3))
    expected.update('iface_p%d_r0_resources' % ppn for ppn in (32, 48, 64, 96))
    rows = {row['name']: row for row in previous['trials']}
    if set(rows) != expected or len(rows) != len(previous['trials']):
        raise ValueError('source trial coverage/duplicates')
    for name, row in rows.items():
        require_clean(row)
        directory = Path(row['configuration']['run']).resolve()
        if directory.name != name or case not in directory.parents:
            raise ValueError('source trial path identity mismatch')
        raw_path = directory / 'trial.json'
        raw = json.loads(raw_path.read_text())
        expected_row = {key: value for key, value in row.items()
                        if key not in ('reused_from_report', 'source_report_sha256')}
        if raw != expected_row:
            raise ValueError('trial JSON differs from source report: ' + name)
        evidence[str(raw_path)] = digest(raw_path)
        if row['configuration'] != json.loads((directory / 'rank_config.json').read_text()):
            raise ValueError('rank configuration changed: ' + name)
        if row['configuration']['budget'] != budget(row['ppn']):
            raise ValueError('rank resource budget changed: ' + name)
        if row.get('reused_from_report'):
            if digest(Path(row['reused_from_report'])) != row['source_report_sha256']:
                raise ValueError('earlier timing provenance changed')
        if name == FAILED_NAME:
            error = row.get('error', '')
            if (row['passed'] is not False or not error.startswith('TimeoutExpired:')
                    or "'scp'" not in error or 'timed out after' not in error):
                raise ValueError('not a post-launch scp timeout')
            continue
        if row['passed'] is not True:
            raise ValueError('non-transfer trial failed: ' + name)
        install = PRE_IFACE if row['mode'] == 'old_rm' else case / 'install'
        checked = runner.validate_trial_artifacts(directory, row['configuration'], row['ppn'],
                    previous['inventory'], install, row['mode'], row['resource_probe'])
        if any(row[key] != value for key, value in checked.items()):
            raise ValueError('recomputed metrics/resources differ: ' + name)
    for ppn, recorded in zip(A8_LEVELS, previous['comparisons']):
        compared = runner.comparison([row for row in rows.values() if row['ppn'] == ppn], ppn)
        if compared != recorded:
            raise ValueError('A8 summary differs from raw trials')
    if len(previous['comparisons']) != len(A8_LEVELS):
        raise ValueError('A8 summary coverage incomplete')
    if [row['ppn'] for row in previous['a9_levels']] != [32, 48, 64]:
        raise ValueError('source A9 completed levels differ')
    for level in previous['a9_levels']:
        group = [row for row in rows.values() if row['ppn'] == level['ppn']
                 and not row['resource_probe']]
        summary = {key: statistics_for([row['metrics'][key] for row in group])
                   for key in group[0]['metrics']}
        if level['timings'] != summary or level['resource_trial'] not in rows:
            raise ValueError('A9 summary differs from raw trials')
    return rows, evidence


def recover_resource(source, out, failed, previous, case):
    run = out / FAILED_NAME
    run.mkdir()
    old_run = source.parent / FAILED_NAME
    # Copy existing local files unchanged. Fetch only the exact peer-rank set.
    for path in old_run.iterdir():
        if path.is_file() and (path.name.startswith(('rank', 'pid_rank')) or
                               path.name in ('launcher.log', 'trial.json', 'live_inventory.json')):
            shutil.copy2(path, run / path.name)
    fetch(old_run, range(96, 192), True, runner.PEER, destination=run)
    checked = runner.validate_trial_artifacts(run, failed['configuration'], 96,
                  previous['inventory'], case / 'install', 'iface', True)
    recovered = copy.deepcopy(failed)
    recovered.pop('error', None)
    recovered.update(checked, passed=True)
    recovered['artifact_directory'] = str(run)
    recovered['recovered_from_report'] = str(source)
    recovered['original_failed_trial_sha256'] = digest(old_run / 'trial.json')
    recovered['scope'] = '同一已退出MPI任务补全取证；未重新运行MPI；原失败记录保持不变'
    runner.write_json(run / 'trial.json', recovered)
    return recovered


def main():
    parser = argparse.ArgumentParser(description='修复已完成PPN96取证，并仅续跑后续RM规模')
    parser.add_argument('--source', type=Path, default=DEFAULT_CASE / SOURCE_NAME / 'results.json')
    parser.add_argument('--recover-only', action='store_true', help='只补取文件及校验，不启动MPI')
    parser.add_argument('--target-ppn', type=int, default=192)
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if args.target_ppn not in (96, 128, 160, 192) or args.timeout < 60:
        parser.error('target-ppn必须为96/128/160/192，timeout至少60')
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise RuntimeError('本工具应由root在node89执行')
    toolkit = runner.validate_toolkit()
    case = DEFAULT_CASE
    out = Path(tempfile.mkdtemp(prefix='a89_recover_', dir=case))
    print('A89_RECOVERY_DIR=' + str(out), flush=True)
    report = {'toolkit': toolkit, 'product_commit': PRODUCT_COMMIT,
              'module_sha256': PRODUCT_MODULE, 'comparisons': [], 'trials': [],
              'a8_functional': False, 'a9_functional': False, 'full_acceptance': False,
              'performance_acceptance': '历史样本与续跑样本分开留档，待完整审阅',
              'reliability_C': 'DEFERRED', 'node_run_started': time.strftime('%F %T %Z')}

    def save():
        runner.write_json(out / 'results.json', report)
        runner.render_summary(out, report)

    save()
    try:
        report['installations'] = {
            'candidate': runner.module_check(case / 'install', PRODUCT_MODULE),
            'old_rm': runner.module_check(PRE_IFACE, PRE_IFACE_MODULE),
            'old_minimal_control': runner.module_check(MINIMAL, MINIMAL_MODULE)}
        report['inventory'] = runner.inventory()
        baseline = LAB / 'osu_minimal8_ff_14ns8'
        report['saved_launcher_records_sha256'] = {
            kind: digest(baseline / (kind + '_run.json')) for kind in ('rm', 'rc')}
        report['observer_identity'] = {
            'local': digest(LAB / 'observer_build_d886fc3/liburma_resource_observer.so'),
            'peer': runner.ssh('sha256sum "$1"\n',
                    LAB / 'observer_runtime_d886fc3/liburma_resource_observer.so').split()[0]}
        source = args.source.resolve()
        previous = source_identity(source, case, report, toolkit)
        rows, evidence = validate_history(previous, source, case)
        report['history'] = {'source_report': str(source), 'source_sha256': digest(source),
                             'source_node_run_started': previous['node_run_started'],
                             'raw_trial_and_regression_sha256': evidence,
                             'historical_samples_revalidated_not_rerun': True,
                             'original_failed_trial_preserved': copy.deepcopy(rows[FAILED_NAME])}
        report['comparisons'] = copy.deepcopy(previous['comparisons'])
        report['regressions'] = copy.deepcopy(previous['regressions'])
        report['trials'] = copy.deepcopy(previous['trials'])
        report['a9_levels'] = copy.deepcopy(previous['a9_levels'])
        report['a8_functional'] = True
        report['highest_rm_passed_ppn'] = 64
        save()
        print('历史证据复核=通过 A8七档/三模式、A9至PPN64；历史计时未重测', flush=True)
        recovered = recover_resource(source, out, rows[FAILED_NAME], previous, case)
        report['trials'].append(recovered)
        timings = [row for row in rows.values() if row['ppn'] == 96 and not row['resource_probe']]
        summary = {key: statistics_for([row['metrics'][key] for row in timings])
                   for key in timings[0]['metrics']}
        report['a9_levels'].append({'ppn': 96, 'timings': summary,
            'resource_trial': str(out / FAILED_NAME), 'cohort': str(source.parent)})
        report['highest_rm_passed_ppn'] = 96
        save()
        print('PPN96原任务补证=通过 DATA/MAPS/PID/TX64/JETTY1/LIVE0；未重跑MPI', flush=True)
        if args.recover_only or args.target_ppn == 96:
            print('RECOVER_ONLY=YES；本次不启动MPI，A9至目标PPN192仍未完成', flush=True)
        else:
            report['old8b_control'] = runner.old8b_control(out, baseline)
            commands = {kind: saved_spec(baseline / (kind + '_run.json'), case / 'install')
                        for kind in ('rm', 'rc')}
            binary = out / 'mpi_scale'
            shutil.copy2(source.parent / 'mpi_scale', binary)
            shutil.copy2(runner.ROOT / 'rank_exec.py', out / 'rank_exec.py')
            runner.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
            runner.copy_remote([binary, out / 'rank_exec.py'], str(out) + '/')
            for path in (binary, out / 'rank_exec.py'):
                if runner.ssh('sha256sum "$1"\n', path).split()[0] != digest(path):
                    raise ValueError('continuation binary/wrapper peer mismatch')
            report['binary_sha256'] = digest(binary)
            capacity = min(row['available_cores'] for row in report['inventory'])
            target = min(capacity, args.target_ppn)
            requested = [ppn for ppn in levels(capacity, args.target_ppn) if ppn > 96]
            report['a9_continuation_plan'] = requested
            if target < args.target_ppn or not requested:
                raise ValueError('当前CPU许可不足所请求目标；不自动降低目标后宣称完成')
            for ppn in requested:
                group = []
                for repeat in range(3):
                    row = runner.trial(case, out, commands, report['inventory'],
                                       'iface', ppn, repeat, False, args.timeout)
                    report['trials'].append(row)
                    group.append(row)
                    save()
                    if not row['passed']:
                        raise RuntimeError('A9续跑停止：' + row['name'] + '；' + row.get('error', '')[:500])
                resource = runner.trial(case, out, commands, report['inventory'],
                                        'iface', ppn, 0, True, args.timeout)
                report['trials'].append(resource)
                save()
                if not resource['passed']:
                    raise RuntimeError('A9资源续跑停止：' + resource['name'] + '；' + resource.get('error', '')[:500])
                summary = {key: statistics_for([row['metrics'][key] for row in group])
                           for key in group[0]['metrics']}
                report['a9_levels'].append({'ppn': ppn, 'timings': summary,
                                            'resource_trial': str(out / resource['name']),
                                            'cohort': str(out)})
                report['highest_rm_passed_ppn'] = ppn
                save()
                print('A9 RM PPN=%d 数据/资源/退出=通过 8B交换=%.3fus 1024B有效载荷=%.2fMB/s；无同规模RC基线' %
                      (ppn, summary['exchange8_max_rank_us']['median'],
                       summary['burst1024_payload_MBps']['median']), flush=True)
            report['a9_functional'] = report['highest_rm_passed_ppn'] == target
            report['a9_accepted_target_ppn'] = target
            save()
        print('HIGHEST_RM_PASSED_PPN=' + str(report['highest_rm_passed_ppn']) +
              ' A9_FUNCTIONAL=' + str(report['a9_functional']) + ' FULL_ACCEPTANCE=False', flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        print('A89_RECOVERY_STOPPED=' + report['error'][:700], file=sys.stderr, flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
