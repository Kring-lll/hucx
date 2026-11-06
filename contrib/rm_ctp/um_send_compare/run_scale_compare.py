#!/usr/bin/env python3
"""Reuse accepted launch/identity/cleanup paths; only Alltoall scales to PPN160."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import zipfile

import bootstrap
import policy
import run_compare
import scale_policy as scale

REPEAT_NAME = 'um_osu_repeat_otwq6070'
REPEAT_SHA = '9a41d387d2f6d0be33c7cf73b11669c474f9a10e3e55666910068e482f7d6c8e'
FAILED_SCALE = 'um_osu_scale_a9ogkfo_'


def failed_scale_checkpoint(case, commit, module):
    root = case/FAILED_SCALE
    path = root/'results.json'
    report = json.loads(path.read_text())
    rows = report['gates'] + report['trials']
    if (report['source_commit'] != commit or report['module_sha256'] != module
            or report['tool']['tool_commit'] != '4c0ef1a6ee7d7df97ef8d08e4666836a1ff026f0'
            or report['status'] != 'STOPPED_INCOMPLETE' or len(rows) != 230
            or len(report['comparisons']) != 12 or sum(bool(r['passed']) for r in rows) != 229):
        raise ValueError('reported 230-task scale checkpoint differs')
    for row in rows:
        trial = (root/row['name']/'trial.json').resolve()
        if trial.parent.parent != root.resolve() or json.loads(trial.read_text()) != row:
            raise ValueError('scale checkpoint trial changed: '+row['name'])
        if row['passed']:
            policy.inspect_trial(row, row['profile'], row['phase'])
    if [scale.summarize(report['trials'], i) for i in range(12)] != report['comparisons']:
        raise ValueError('scale checkpoint complete-group statistics differ')
    failed = rows[-1]
    if (failed['name'] != 'osu_alltoall_rm_p128_timing_229' or failed['profile'] != 'rm'
            or failed['ppn'] != 128 or failed['phase'] != 'timing' or failed['repeat'] != 0):
        raise ValueError('unexpected failed scale task')
    cleanup = failed.get('cleanup', {})
    if (failed.get('cleanup_error') or set(cleanup) != {'local', 'peer'} or
            any(v.get('terminated_own_pids') != [] for v in cleanup.values())):
        raise ValueError('historical failed-task cleanup differs from reported empty dual-node cleanup')
    log = root/failed['name']/'launcher.log'
    observation = scale.rnr_log_evidence(failed, log.read_text(errors='replace'))
    if not observation['rnr_observed']:
        raise ValueError('historical scale RNR not observed: '+json.dumps(observation, sort_keys=True))
    print('HISTORICAL_FAILURE_LOG='+json.dumps(observation, sort_keys=True), flush=True)
    return {'source_result': str(path), 'observed_sha256': bootstrap.digest(path),
            'failed_log': str(log), 'failed_log_sha256': bootstrap.digest(log),
            'successful_tasks_reviewed': 229, 'failed_tasks_retained': 1,
            'complete_groups_reviewed': 12, 'prior_failure_upgraded': False,
            'historical_failure_log': observation,
            'historical_log_exclusivity_required': False,
            'live_failure_continuation_rule': 'unchanged strict status10-only and empty cleanup',
            'merged_with_new_samples': False,
            'binding': 'reported identity/coverage/all trial equality/statistics; hashes captured now, not pre-pinned'}


def repeat_checkpoint(case, commit, module):
    path = case/REPEAT_NAME/'results.json'
    if bootstrap.digest(path) != REPEAT_SHA:
        raise ValueError('reviewed 183-task results changed')
    report = json.loads(path.read_text())
    if (report['source_commit'] != commit or report['module_sha256'] != module
            or report['status'] != 'FOCUSED_REPEAT_STUDY_COMPLETE_REVIEW_REQUIRED'
            or len(report['gates']) != 3 or len(report['trials']) != 180
            or [r['profile'] for r in report['gates']] != list(policy.PROFILES)):
        raise ValueError('reviewed repeat coverage/product differs')
    for row in report['gates'] + report['trials']:
        trial = (path.parent/row['name']/'trial.json').resolve()
        if trial.parent.parent != path.parent.resolve() or json.loads(trial.read_text()) != row:
            raise ValueError('reviewed repeat trial changed: ' + row['name'])
        policy.inspect_trial(row, row['profile'], row['phase'])
    if [policy.summarize(report['trials'], i, n, policy.STUDY_GROUPS)
            for i, n in enumerate((30, 12, 12))] != report['comparisons']:
        raise ValueError('reviewed repeat statistics changed')
    return {'source_result': str(path), 'sha256': REPEAT_SHA, 'reviewed_mpi': 183,
            'merged_with_new_samples': False}


def disk_inventory(base, case):
    code = ('import json,shutil,socket,sys\n'
            'print(json.dumps({"hostname":socket.gethostname(),'
            '"free_bytes":shutil.disk_usage(sys.argv[1]).free}))\n')
    env = {k: v for k, v in os.environ.items() if k not in ('LD_PRELOAD', 'LD_DEBUG')}
    local = subprocess.check_output([sys.executable, '-c', code, str(case)],
                                    env=env, text=True, timeout=15)
    peer = base.ssh('env -u LD_PRELOAD -u LD_DEBUG python3 -c "$1" "$2"\n',
                    code, case, timeout=20)
    return [json.loads(local), json.loads(peer)]


def main():
    parser = argparse.ArgumentParser(description='原版OSU latency/bw/bibw两进程；Alltoall PPN1..160；RC到24')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--timing', action='store_true', help='校验后五轮计时及一轮资源；默认只计划')
    parser.add_argument('--rx-depth', type=int, choices=(512, 4096), default=None,
                        help='整批三模式接收深度；4096同时覆盖RC辅助UM，旧入口默认512保持不变')
    parser.add_argument('--rnr-study', action='store_true', help='仅PPN128，RM/UM分别RX512/4096；精确RNR失败保留并跳过该条件后续')
    args = parser.parse_args()
    if args.rnr_study and args.rx_depth is not None:
        parser.error('--rnr-study already fixes two queue depths; do not combine --rx-depth')
    requested_rx_depth = args.rx_depth if args.rx_depth is not None else 512
    if args.rnr_study:
        args.timing = True
    plan = scale.rnr_plan if args.rnr_study else lambda timing: scale.plan(timing, requested_rx_depth)
    write_tables = scale.write_rnr_tables if args.rnr_study else scale.write_tables
    if not (args.run or args.preflight_only):
        print(json.dumps(plan(args.timing), ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('explicit execution is root on node89 only')
    guard, engine, prior, metadata = bootstrap.load()
    prior['reviewed_repeat'] = repeat_checkpoint(guard.CASE, guard.COMMIT, guard.MODULE_SHA)
    if args.rnr_study:
        prior['failed_scale_review'] = failed_scale_checkpoint(guard.CASE, guard.COMMIT, guard.MODULE_SHA)
    case = guard.CASE
    # Prevent accidental overlap of this scale package with itself. Other users'
    # jobs are only observed; their processes/settings are never changed.
    lease = (case.parent/'um_osu_scale.lock').open('a+')
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        raise RuntimeError('another scale batch is running; preserve both jobs')
    osu = engine['osu']
    base = osu.base
    prefix = ('um_rnr_study_' if args.rnr_study else
              'um_osu_rx4096_' if requested_rx_depth == 4096 else 'um_osu_scale_')
    out = Path(tempfile.mkdtemp(prefix=prefix, dir=case))
    print(('RNR_STUDY_DIR=' if args.rnr_study else 'SCALE_OSU_DIR=') + str(out), flush=True)
    report = {'source_commit': guard.COMMIT, 'module_sha256': guard.MODULE_SHA,
              'tool': metadata, 'checkpoint': prior, 'plan': plan(args.timing),
              'status': 'RUNNING', 'gates': [], 'trials': [], 'comparisons': [],
              'capacity_checks': [], 'task_errors': [], 'skipped_tasks': []}
    def save():
        write_tables(out, report)
        base.write_json(out/'results.json', report)
    save()
    names = ('DEFAULT_CASE', 'PRODUCT_COMMIT', 'PRODUCT_MODULE', 'BENCHMARKS',
             'arguments', 'parse_output', 'environment', 'budget', 'inspect_jettys', 'launcher')
    original = {name: getattr(osu, name) for name in names}
    original_config = base.parsed_config
    original_inventory = base.inventory
    active_ppn = 1
    active_rx_depth = requested_rx_depth
    def guarded_inventory():
        current = original_inventory()
        check = scale.capacity(current, active_ppn, disk_inventory(base, case))
        report['capacity_checks'].append(check)
        save()
        if not check['passed']:
            raise RuntimeError('staging CPU/memory/rlimit/disk insufficient at PPN%d; see capacity_checks' % active_ppn)
        return current
    try:
        engine['OBSERVE'] = engine['FAULT'] = False
        engine['deploy'](case, engine['validate_source'](case))
        osu.DEFAULT_CASE, osu.PRODUCT_COMMIT, osu.PRODUCT_MODULE = case, guard.COMMIT, guard.MODULE_SHA
        osu.BENCHMARKS, osu.arguments, osu.parse_output = scale.BENCHMARKS, scale.arguments, scale.parse_output
        def environment(saved, mode, install, configuration):
            env = engine['environment'](saved, mode, install, configuration)
            if args.rnr_study or requested_rx_depth == 4096:
                if not args.rnr_study:
                    # RC also opens an auxiliary UM iface. Verify all actual
                    # JFRs at the same depth rather than ignoring auxiliary RX.
                    for profile in scale.PROFILES:
                        env['UCX_'+profile.upper()+'_URMA_RX_DEPTH'] = str(active_rx_depth)
                env['UCX_'+engine['PROFILE'].upper()+'_URMA_RX_DEPTH'] = str(active_rx_depth)
                # Explicitly freeze the unchanged replenishment settings.
                prefix = 'UCX_'+engine['PROFILE'].upper()+'_URMA_'
                env[prefix+'RX_MAX_POLL'] = '16'
                env[prefix+'RX_MAX_BATCH'] = '16'
            return env
        def budget(ppn):
            config = scale.budget(ppn)
            config['rx_depth'] = active_rx_depth
            return config
        osu.environment, osu.budget = environment, budget
        base.parsed_config = engine['parsed_config']
        base.inventory = guarded_inventory
        def jettys(run, ppn, unused, pids, depth):
            values = original['inspect_jettys'](run, ppn, policy.MODES[engine['PROFILE']], pids, depth)
            if args.rnr_study or requested_rx_depth == 4096:
                depths = scale.jfr_depths(run, ppn, pids, active_rx_depth)
                for rank in values:
                    values[rank]['jfr_rx_depths'] = depths[rank]
            return values
        osu.inspect_jettys = jettys
        # The underlying launcher already binds distinct physical cores. Add a
        # second guard so future schedules cannot accidentally scale 2-rank OMBs.
        def launcher(command, ppn, hosts, wrapper, config):
            cfg = json.loads(Path(config).read_text())
            benchmark = Path(cfg['binary']).name
            if benchmark != 'osu_alltoall' and ppn != 1:
                raise ValueError('two-rank OSU must use PPN1')
            if engine['PROFILE'] == 'rc' and ppn > 24:
                raise ValueError('RC outside agreed PPN24 limit')
            return original['launcher'](command, ppn, hosts, wrapper, config)
        osu.launcher = launcher
        engine['PROFILE'] = 'rm'
        commands = {}
        for mode in ('rc', 'rm'):
            command, saved = osu.saved_spec(osu.LAB/'osu_minimal8_ff_14ns8'/(mode+'_run.json'), case/'install')
            commands[mode] = command, environment(saved, mode, case/'install', {})
        evidence = osu.preflight(out, commands)
        report['preflight'] = evidence
        report['available_physical_cores'] = [r['available_cores'] for r in evidence['inventory']]
        for name in ('osu_bw', 'osu_bibw'):
            if any(s not in (out/(name+'_help.txt')).read_text() for s in ('--window-size', '--buffer-num')):
                raise ValueError('original bandwidth options missing')
        base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        for source, name in ((osu.ROOT/'osu_rank.py', 'osu_rank.py'),
                             (osu.A89/'rank_exec.py', 'a89_rank_exec.py')):
            shutil.copy2(source, out/name)
        if args.rnr_study or requested_rx_depth == 4096:
            # Limit only our new rank children, including fatal core dumps.
            # This has no effect on other jobs or the OSU successful hot path.
            path = out/'osu_rank.py'
            source = path.read_text()
            old = '        shared.main()'
            if source.count(old) != 1:
                raise ValueError('pinned rank wrapper entry differs')
            path.write_text(source.replace(old, '        import resource\n        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n'+old))
        with zipfile.ZipFile(Path(__file__).parent) as bundle:
            (out/'load_snapshot.py').write_bytes(bundle.read('load_snapshot.py'))
        helpers = [out/name for name in ('osu_rank.py', 'a89_rank_exec.py', 'libosu_identity.so', 'load_snapshot.py')]
        base.copy_remote(helpers, str(out) + '/')
        for path in helpers:
            if base.ssh('sha256sum "$1"\n', path).split()[0] != bootstrap.digest(path):
                raise ValueError('peer helper checksum mismatch: ' + path.name)
        report['helper_sha256'] = {p.name: bootstrap.digest(p) for p in helpers}
        save()
        print(('预检=PASS 固定a4d双端库/原版OSU7.5.2；仅PPN128 RX512/4096定向实验，不继续160' if args.rnr_study else
               '预检=PASS 固定a4d双端库/原版OSU7.5.2；点对点PPN1，Alltoall目标PPN160，RC到24；逐任务检查资源'), flush=True)
        print('AVAILABLE_PHYSICAL_CORES=' + json.dumps(report['available_physical_cores']), flush=True)
        print('QUEUE_POLICY=' + report['plan']['queue_policy'] if not args.rnr_study else
              'QUEUE_POLICY=PPN128 RM/UM RX512/4096 controlled study', flush=True)
        if args.preflight_only:
            report['status'] = 'PREFLIGHT_ONLY_NO_BENCHMARK_MPI'
            save()
            return
        index = 0
        schedule = [((-1, 'osu_latency', 1, 8, 'validation', 0, p), requested_rx_depth) for p in scale.PROFILES]
        schedule += list(scale.rnr_tasks(args.timing)) if args.rnr_study else [(item, requested_rx_depth) for item in scale.tasks(args.timing)]
        blocked = set()
        for item, active_rx_depth in schedule:
            group, benchmark, active_ppn, size, phase, repeat, profile = item
            condition = profile, active_rx_depth
            if group >= 0 and condition in blocked:
                report['skipped_tasks'].append({'item': item, 'requested_rx_depth': active_rx_depth,
                                               'reason': 'earlier observed RNR in this condition'})
                save()
                continue
            report['active_task'] = {'index': index, 'benchmark': benchmark, 'ppn': active_ppn,
                                     'phase': phase, 'profile': profile, 'repeat': repeat}
            if args.rnr_study or requested_rx_depth == 4096:
                report['active_task']['requested_rx_depth'] = active_rx_depth
            save()
            print('START ' + json.dumps(report['active_task'], sort_keys=True), flush=True)
            iterations, warmup = ((100, 10) if phase == 'validation' else
                                  (10000, 1000) if benchmark == 'osu_latency' else (1000, 100))
            trial_args = SimpleNamespace(iterations=iterations, warmup=warmup,
                                         timeout=120 if active_ppn == 1 else 300)
            try:
                row = run_compare.run_trial(engine, out, commands, evidence, item, index,
                                             capture=True, trial_args=trial_args)
            except Exception as exc:
                report['task_errors'].append({**report['active_task'],
                                              'error': type(exc).__name__ + ': ' + str(exc)})
                raise
            if args.rnr_study or requested_rx_depth == 4096:
                row['requested_rx_depth'] = active_rx_depth
                base.write_json(out/row['name']/'trial.json', row)
            (report['gates'] if group == -1 else report['trials']).append(row)
            index += 1
            save()
            print('OSU=%s PROFILE=%s PPN=%d BYTES=%d PHASE=%s REPEAT=%d PASS=%s' % (
                benchmark, profile, active_ppn, size, phase, repeat, row['passed']), flush=True)
            if not row['passed']:
                log = out/row['name']/'launcher.log'
                if args.rnr_study and group >= 0:
                    row['failure_log_evidence'] = scale.rnr_log_evidence(row, log.read_text(errors='replace'))
                    base.write_json(out/row['name']/'trial.json', row)
                    save()
                    print('LIVE_FAILURE_LOG='+json.dumps(row['failure_log_evidence'], sort_keys=True), flush=True)
                if args.rnr_study and group >= 0 and row['failure_log_evidence']['live_continuation_allowed']:
                    row['diagnosis'] = 'RNR_RETRY_EXHAUSTED'
                    base.write_json(out/row['name']/'trial.json', row)
                    blocked.add(condition)
                    save()
                    print('OBSERVED_RNR PROFILE=%s RX_DEPTH=%d；失败保留，该条件后续跳过，继续其他受控条件' % condition, flush=True)
                    continue
                raise RuntimeError(row.get('error', 'OSU failed') + ' LOG=' + str(out/row['name']/'launcher.log'))
            if not args.rnr_study and group >= 0 and args.timing and phase == 'resources' and profile == scale.profiles(active_ppn)[-1]:
                compared = scale.summarize(report['trials'], group)
                report['comparisons'].append(compared)
                save()
                print('GROUP=' + json.dumps(compared, sort_keys=True), flush=True)
        report['load_capture_incomplete_trials'] = [r['name'] for r in report['gates'] + report['trials']
            if any('error' in v for v in r['load_evidence'].values())]
        report['status'] = ('SELECTED_P2P_AND_ALLTOALL_SCALE_COMPLETE_REVIEW_REQUIRED' if args.timing else
                            'SELECTED_P2P_AND_ALLTOALL_SCALE_VALIDATION_ONLY_PASS')
        if args.rnr_study:
            report['status'] = ('RNR_STUDY_COMPLETED_WITH_OBSERVED_FAILURES_REVIEW_REQUIRED' if blocked else
                                'RNR_STUDY_ALL_CONDITIONS_COMPLETE_REVIEW_REQUIRED')
            report['all_conditions_passed'] = not blocked
        report.pop('active_task', None)
        save()
        print(('PPN128 RNR定向实验结束；失败及成功均保留，是否缓解待审阅，不继续PPN160，不作根因/完整规模验收结论' if args.rnr_study else
               '完成所选点对点/Alltoall规模测试；保留全部样本待审阅。无PSN精简/READ_WRITE/硬件故障/完整C_D验收结论'), flush=True)
    except Exception as exc:
        report.update(status='STOPPED_INCOMPLETE', error=type(exc).__name__ + ': ' + str(exc))
        save()
        print('SCALE_OSU_STOPPED=' + report['error'], flush=True)
        raise
    finally:
        for name, value in original.items():
            setattr(osu, name, value)
        base.parsed_config, base.inventory = original_config, original_inventory
        lease.close()
        print('RESULTS_JSON=' + str(out/'results.json'), flush=True)
        print('TABLE_MD=' + str(out/'comparison.md'), flush=True)
        print('TABLE_CSV=' + str(out/'comparison.csv'), flush=True)
        if (bootstrap.digest(case/'source'/guard.RELATIVE_ENTRY) != guard.ENTRY_SHA or
                bootstrap.digest(case/'install/lib/ucx/libuct_ub.so.0.0.0') != guard.MODULE_SHA):
            raise RuntimeError('original candidate source/module changed')


if __name__ == '__main__':
    main()
