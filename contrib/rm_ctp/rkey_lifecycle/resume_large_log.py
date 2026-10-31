#!/usr/bin/env python3
"""Recheck existing 1MiB log evidence, then run only two AM and ten collective tasks."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile

ROOT = Path(__file__).resolve().parent

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

r = load('rkey_large_log_controller', ROOT / 'run_candidate.py')
prior = load('rkey_prior_checkpoint', ROOT / 'resume.py')
am, window, candidate, osu = r.pending.am, r.pending.window, r.candidate, r.osu
CASE = prior.CASE
CHECKPOINT = CASE / 'rkey_resume_j6c51b_5'


def read(path):
    return json.loads(Path(path).read_text())


def normalized(value):
    return json.loads(json.dumps(value))


def owned(path, case=CASE):
    path = Path(path).resolve()
    if not path.is_relative_to(case.resolve()):
        raise ValueError('checkpoint reference outside this CASE')
    return path


def checkpoint(root=CHECKPOINT, case=CASE):
    root, case = Path(root), Path(case)
    if root.parent != case:
        raise ValueError('checkpoint not directly below CASE')
    old = read(root / 'results.json')
    prior.identity(old)
    if (old.get('status') != 'STOPPED_INCOMPLETE' or
            'missing/nonregular/oversized actual application UCX log' not in old.get('error', '')):
        raise ValueError('not the selected 1MiB log-limit interruption')
    _, previous = prior.checkpoint(case, Path(old['checkpoint']['source_result']))
    for key in ('source_sha256', 'normal_sha256'):
        if previous[key] != old['checkpoint'][key]:
            raise ValueError('earlier checkpoint identity changed: ' + key)
    boundary = owned(old['boundary_matrix'], case)
    boundary_report = read(boundary)
    if (boundary_report.get('product_commit') != prior.COMMIT or
            boundary_report.get('module_sha256') != prior.MODULE or
            len(boundary_report['cases']) != 18 or
            not all(row['passed'] for row in boundary_report['cases'])):
        raise ValueError('prior 18-case boundary result differs')
    timing_path = root / 'comparison/comparison_trials.json'
    timing = read(timing_path)
    if len(timing) != 15 or not all(row['passed'] for row in timing):
        raise ValueError('prior timing is incomplete')
    for label in ('old_rm', 'new_rm', 'rc'):
        rows = [row for row in timing if row['comparison_label'] == label]
        if (len(rows) != 5 or [row['output']['latency_us'] for row in rows]
                != old['timing'][label]['samples_us']):
            raise ValueError('prior timing samples differ: ' + label)
    matrix_root = root / 'window_and_am'
    matrix = read(matrix_root / 'results.json')
    prior.identity(matrix)
    tasks = [('WINDOW', item) for item in window.cases()]
    tasks += [('AM_RNDV', item) for item in am.cases()]
    rows = matrix['cases']
    if len(rows) != 18:
        raise ValueError('expected seventeen passes and one interrupted log check')
    for index, row in enumerate(rows):
        family, item = tasks[index]
        if (row['name'] != am.trial_directory(matrix_root, item, index).name or
                row['family'] != family or normalized(row['matrix_case']) != normalized(item)
                or row.get('exit') != 0 or (index < 17 and row.get('passed') is not True)):
            raise ValueError('prior matrix task differs at index ' + str(index))
    last = rows[-1]
    if (last['passed'] or last.get('error') !=
            'ValueError: missing/nonregular/oversized actual application UCX log'
            or last.get('cleanup_error') or set(last.get('cleanup', {})) != {'local', 'peer'}
            or any(side.get('terminated_own_pids') for side in last['cleanup'].values())):
        raise ValueError('last trial contains another failure or cleanup problem')
    receipt = {'source_result': str(root / 'results.json'),
               'source_sha256': osu.digest(root / 'results.json'),
               'matrix_sha256': osu.digest(matrix_root / 'results.json'),
               'boundary_sha256': osu.digest(boundary), 'timing_sha256': osu.digest(timing_path),
               'normal_cases': 28, 'boundary_cases': 18, 'timing_trials': 15,
               'passed_window_tasks': 10, 'passed_am_tasks': 7}
    return old, matrix, receipt


def review_large(matrix, destination, root=CHECKPOINT, case=CASE):
    """No MPI, no old receipt writes; independently recheck data/maps/WR/protocol."""
    out = Path(root) / 'window_and_am'
    row = copy.deepcopy(matrix['cases'][-1])
    item = am.cases()[7]
    run = out / row['name']
    if row.get('exit') != 0 or item['sizes'] != (1048576,):
        raise ValueError('not the completed original 1MiB OSU task')
    raw = read(run / 'trial.json')
    if any(raw.get(key) != row.get(key) for key in
           ('name', 'exit', 'passed', 'error', 'configuration', 'rank_evidence')):
        raise ValueError('matrix and original trial receipts differ')
    evidence = matrix['preflight']
    row['output'] = am.window_matrix.parse_output(
        (run / 'launcher.log').read_text(), item, 20)
    mpi = evidence['benchmarks'][item['benchmark']]['mpi_library']
    if osu.digest(Path(mpi)) != evidence['benchmarks'][item['benchmark']]['mpi_library_sha256']:
        raise ValueError('pinned MPI dependency changed')
    ranks = osu.inspect_ranks(run, 1, evidence['inventory'], Path(case) / 'install',
                              row['configuration']['nonce'], mpi)
    if normalized(ranks) != normalized(row['rank_evidence']):
        raise ValueError('rank/maps evidence differs')
    row['rank_evidence'] = ranks
    row['passed'] = True
    row.pop('error', None)
    if candidate.SEGMENT_KEYS & row['configuration']['environment'].keys():
        raise ValueError('old trial unexpectedly forced SEG_SIZE')
    row['effective_defaults'] = {
        host: candidate.check_defaults((run / (host + '_ucx_config.txt')).read_text(),
                                        candidate.bytes_value)
        for host in ('node89', 'node97')}
    row['segment_environment_override'] = False
    library = out / 'libsend_probe.so'
    if osu.digest(library) != matrix['probe_sha256']['node89']:
        raise ValueError('WR observer binary changed')
    row['send_observations'] = window.wr.inspect_trial(run, row, library)
    row['wr_probe_passed'] = True
    row['protocol_configuration'] = {
        host: am.check_config((run / (host + '_ucx_config.txt')).read_text(), item, candidate.bytes_value)
        for host in ('node89', 'node97')}
    identities = {rank: {'host': evidence['inventory'][rank]['hostname'],
                         'pid': ranks['pids'][rank]} for rank in range(2)}
    row['protocol_path'], row['protocol_log_files'] = am.collect_split_logs(
        out, row, evidence, osu.base, reuse_existing=True,
        parse_lines=lambda lines: am.inspect_trace(lines, item, identities))
    row['recheck_scope'] = 'EXISTING_DATA_MAPS_WR_FULL_STREAMED_LOGS_NO_NEW_MPI'
    osu.base.write_json(destination, row)
    return row


def remaining_am(out, case, build):
    out.mkdir()
    report = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
              'cases': [], 'status': 'RUNNING'}
    try:
        with candidate.bind_candidate(osu, case, build), window.benchmark_registry():
            osu.validate_package()
            commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                            (mode + '_run.json'), case / 'install')
                        for mode in ('rm', 'rc')}
            evidence = osu.preflight(out, commands)
            report['preflight'] = evidence
            window.check_help(out)
            if min(node['available_cores'] for node in evidence['inventory']) < 4:
                raise ValueError('fewer than four allowed cores')
            library = window.prepare_helpers(out, report)
            args = argparse.Namespace(iterations=20, warmup=2, timeout=240)
            for index, item in enumerate(am.cases()[8:], 18):
                with am.trial_profile(osu, item, am.window_matrix.parse_output,
                                      log_directory=am.trial_directory(out, item, index)):
                    with window.traced_environment(library):
                        row = osu.one_trial(out, commands, evidence, item['benchmark'], 'rm',
                                            item['ppn'], 'validation', index, args,
                                            message_bytes=item['sizes'][0])
                window.finish_trial(out, row, item, library)
                am.finish_protocol(out, row, item, evidence)
                report['cases'].append(row)
                osu.base.write_json(out / 'results.json', report)
                if not row['passed']:
                    raise ValueError(row['name'] + ': ' + row.get('error', 'AM check failed'))
                print('AM_REMAINING OSU=%s PPN=%d SIZE=65536 DATA/MAPS/WR/PATH/EXIT=PASS' %
                      (item['benchmark'], item['ppn']), flush=True)
        report['status'] = 'REMAINING_TWO_AM_TASKS_PASS'
        return str(out / 'results.json')
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        osu.base.write_json(out / 'results.json', report)


def main():
    parser = argparse.ArgumentParser(description='补验1MiB已有日志，只新增2项AM及10项集合，默认仅计划')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({'case': str(CASE), 'checkpoint': str(CHECKPOINT),
                          'new_mpi_tasks': 12, 'max_ppn': 4, 'new_1MiB_tasks': 0,
                          'log_limit_mib': 256, 'product_rebuild': False,
                          'native_RMA_enabled': False}, ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may run')
    build = candidate.validate_build(CASE)
    prior.identity(build)
    old, matrix, receipt = checkpoint()
    out = Path(tempfile.mkdtemp(prefix='rkey_log_review_', dir=CASE))
    report = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
              'checkpoint': receipt, 'timing': old['timing'], 'status': 'RUNNING',
              'performance_accepted': False, 'native_RMA_accepted': False}
    print('RKEY_LOG_REVIEW_DIR=' + str(out), flush=True)
    try:
        report['deployment'] = candidate.deploy(CASE, build, out)
        report['large_log_review'] = review_large(matrix, out / 'large_log_trial.json')
        print('1MiB既有任务补验=DATA/MAPS/WR/AM_PATH/FULL_LOGS/EXIT PASS；未重跑MPI', flush=True)
        report['remaining_am'] = remaining_am(out / 'remaining_am', CASE, build)
        report['collective'] = r.collective_matrix(out / 'collective', CASE, build)
        report['status'] = 'RKEY_FIX_PRIOR_GATES_LOG_REVIEW_AND_REMAINING_TASKS_PASS_PERFORMANCE_REVIEW_REQUIRED'
        print('28正常/18边界/10窗口/10AM/10集合=PASS；15轮计时保留，性能待审阅', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        osu.base.write_json(out / 'results.json', report)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('RKEY_LOG_REVIEW_STOPPED=' + type(exc).__name__ + ': ' + str(exc)[:700], flush=True)
        raise SystemExit(1)
