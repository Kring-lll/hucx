#!/usr/bin/env python3
"""New B candidate: verify defaults, reuse regressions and original OSU tools."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import json
import os
from pathlib import Path
import shutil
import socket
import statistics
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'wr_probe'))
import run_probe as wr
from boundary_config import bytes_value
from policy import B_ROOT, SEGMENT_KEYS, bind_candidate, check_defaults, validate_build, without_segment_overrides

osu = wr.osu
BASELINE_LAB = osu.LAB
OLD_CASE = osu.DEFAULT_CASE
OLD_COMMIT = osu.PRODUCT_COMMIT
OLD_MODULE = osu.PRODUCT_MODULE


def deploy(case, build, out):
    """Only create a new peer CASE, or reuse its already matching install."""
    module = case / 'install/lib/ucx/libuct_ub.so.0.0.0'
    state = osu.base.ssh('if test -e "$1"; then printf EXISTS; else printf ABSENT; fi\n', case).strip()
    if state == 'ABSENT':
        archive = case / 'install.tar.gz'
        archive_sha = osu.digest(archive)
        osu.base.ssh('set -eu\nmkdir -p "$1"\nmkdir "$2"\n', B_ROOT, case)
        osu.base.copy_remote([archive], str(case) + '/')
        if osu.base.ssh('sha256sum "$1"\n', archive).split()[0] != archive_sha:
            raise ValueError('peer install archive differs')
        osu.base.ssh('set -eu\ntar -xzf "$1" -C "$2"\n', archive, case, timeout=100)
    elif state != 'EXISTS':
        raise ValueError('unexpected peer CASE existence result')
    if osu.base.ssh('sha256sum "$1"\n', module).split()[0] != build['module_sha256']:
        raise ValueError('existing peer install is not this candidate; do not overwrite')
    return osu.base.module_check(case / 'install', build['module_sha256'])


def default_capabilities(install, environment, out):
    effective = without_segment_overrides(environment)
    result = wr.boundary.check_remote_caps(install, effective, out)
    defaults = {}
    for host in ('node89', 'node97'):
        defaults[host] = check_defaults((out / (host + '_ucx_config.txt')).read_text(),
                                        bytes_value)
    return {'effective_defaults': defaults, 'capability': result,
            'segment_environment_override': False}


def check_trial_default(row, out):
    if not row.get('passed'):
        return row
    try:
        environment = row['configuration']['environment']
        if SEGMENT_KEYS & environment.keys():
            raise ValueError('trial unexpectedly forced SEG_SIZE through environment')
        run = out / row['name']
        parsed = {}
        for host in ('node89', 'node97'):
            parsed[host] = check_defaults((run / (host + '_ucx_config.txt')).read_text(),
                                           bytes_value)
        row['effective_defaults'] = parsed
        row['segment_environment_override'] = False
    except Exception as exc:
        row['passed'] = False
        row['error'] = 'default configuration gate: ' + str(exc)
    osu.base.write_json(out / row['name'] / 'trial.json', row)
    return row


def normal_regression(case, out):
    """Run the existing A regression, without adding a new communication demo."""
    sys.path.insert(0, str(PROJECT / 'r1'))
    import accept_iface as legacy
    previous_remote = legacy.remote
    previous_argv = sys.argv
    before = set(case.glob('iface_accept_*'))
    log = out / 'normal_28_regression.log'
    try:
        legacy.remote = osu.base.ssh
        sys.argv = [str(PROJECT / 'r1/accept_iface.py'), str(case), '--no-deploy',
                    '--experiment-root', str(B_ROOT),
                    '--baseline', str(BASELINE_LAB / 'osu_minimal8_ff_14ns8'), '--repeats', '3']
        with log.open('w') as stream, redirect_stdout(stream), redirect_stderr(stream):
            legacy.main()
    finally:
        legacy.remote = previous_remote
        sys.argv = previous_argv
    created = set(case.glob('iface_accept_*')) - before
    if len(created) != 1:
        raise ValueError('normal regression output identity is ambiguous')
    path = created.pop() / 'results.json'
    report = json.loads(path.read_text())
    if len(report['cases']) != 28 or not all(row['passed'] for row in report['cases']):
        raise ValueError('normal 28-case regression failed; log=' + str(log))
    return {'passed': True, 'cases': 28, 'results_json': str(path), 'log': str(log),
            'scope': 'EXISTING_HISTORICAL_REGRESSION_NOT_NEW_STANDARD_OSU_CASES'}


def boundary_matrix(case, receipt):
    previous_trial = osu.one_trial
    previous_caps = wr.boundary.check_remote_caps
    previous_argv = sys.argv
    before = set(case.glob('b_wr_probe_*'))
    log = Path(receipt['controller_dir']) / 'default_boundary_matrix.log'
    # Keep a reference to avoid recursively calling this replacement.
    def capabilities(install, environment, out):
        effective = without_segment_overrides(environment)
        result = previous_caps(install, effective, out)
        result['defaults'] = {host: check_defaults(
            (out / (host + '_ucx_config.txt')).read_text(), bytes_value)
            for host in ('node89', 'node97')}
        return result

    def trial(out, *args, **kwargs):
        kwargs['segment_size'] = None
        return check_trial_default(previous_trial(out, *args, **kwargs), Path(out))

    try:
        osu.one_trial = trial
        wr.boundary.check_remote_caps = capabilities
        sys.argv = [str(PROJECT / 'wr_probe/run_probe.py'), '--boundary-matrix', '--run']
        with log.open('w') as stream, redirect_stdout(stream), redirect_stderr(stream):
            wr.main()
    finally:
        osu.one_trial = previous_trial
        wr.boundary.check_remote_caps = previous_caps
        sys.argv = previous_argv
    created = set(case.glob('b_wr_probe_*')) - before
    if len(created) != 1:
        raise ValueError('WR boundary output identity is ambiguous')
    result = created.pop()
    report = json.loads((result / 'results.json').read_text())
    if len(report['cases']) != 18 or not all(row['passed'] for row in report['cases']):
        raise ValueError('18-case default boundary matrix incomplete')
    if not all(row.get('wr_probe_passed') for row in report['cases'] if row['mode'] == 'rm'):
        raise ValueError('RM per-SEND evidence incomplete')
    receipt['boundary_matrix'] = str(result / 'results.json')
    receipt['boundary_log'] = str(log)
    maxima = [item['max_send_bytes'] for row in report['cases']
              for item in row.get('send_observations', [])]
    receipt['max_observed_send_bytes'] = max(maxima)
    return result


def timing_comparison(out, case, build):
    """Five unobserved 8B OSU rounds, alternating old RM/new RM/current RC."""
    out.mkdir()
    osu.base.ssh('set -eu\nmkdir -p "$1"\nmkdir "$2"\n', out.parent, out)
    commands_new = {mode: osu.saved_spec(BASELINE_LAB / 'osu_minimal8_ff_14ns8' /
                                        (mode + '_run.json'), case / 'install') for mode in ('rm', 'rc')}
    evidence = osu.preflight(out, commands_new)
    for source, name in ((osu.ROOT / 'osu_rank.py', 'osu_rank.py'),
                          (osu.A89 / 'rank_exec.py', 'a89_rank_exec.py')):
        shutil.copy2(source, out / name)
    for name in ('osu_rank.py', 'a89_rank_exec.py', 'libosu_identity.so'):
        osu.base.copy_remote([out / name], str(out) + '/')
        if osu.base.ssh('sha256sum "$1"\n', out / name).split()[0] != osu.digest(out / name):
            raise ValueError('comparison helper differs on peer: ' + name)
    osu.base.module_check(OLD_CASE / 'install', OLD_MODULE)
    args = argparse.Namespace(iterations=5000, warmup=1000, timeout=120)
    rows = []
    commands_old = {mode: osu.saved_spec(BASELINE_LAB / 'osu_minimal8_ff_14ns8' /
                                        (mode + '_run.json'), OLD_CASE / 'install') for mode in ('rm', 'rc')}
    with bind_candidate(osu, OLD_CASE, {'source_commit': OLD_COMMIT, 'module_sha256': OLD_MODULE}):
        control = osu.one_trial(out, commands_old, evidence, 'osu_multi_lat', 'rm',
                                1, 'validation', 0, args)
    if not control['passed']:
        raise ValueError('previous RM validation failed: ' + str(control.get('error')))
    for repeat in range(5):
        order = ('old_rm', 'new_rm', 'rc') if repeat % 2 == 0 else ('rc', 'new_rm', 'old_rm')
        for index, label in enumerate(order):
            target = OLD_CASE if label == 'old_rm' else case
            identity = {'source_commit': OLD_COMMIT, 'module_sha256': OLD_MODULE} if label == 'old_rm' else build
            with bind_candidate(osu, target, identity):
                commands = commands_old if label == 'old_rm' else commands_new
                row = osu.one_trial(out, commands, evidence, 'osu_multi_lat',
                                    'rc' if label == 'rc' else 'rm', 1, 'timing',
                                    repeat * 3 + index, args)
            row['comparison_label'] = label
            rows.append(row)
            osu.base.write_json(out / 'comparison_trials.json', rows)
            if not row['passed']:
                raise ValueError('8B comparison failed: ' + str(row.get('error')))
            if 'libsend_probe' in row['configuration']['environment']['LD_PRELOAD']:
                raise ValueError('WR observer leaked into performance round')
    result = {}
    for label in ('old_rm', 'new_rm', 'rc'):
        values = [row['output']['latency_us'] for row in rows if row['comparison_label'] == label]
        result[label] = {'samples_us': values, 'median_us': statistics.median(values),
                         'minimum_us': min(values), 'maximum_us': max(values),
                         'rss_sum_mib_median': statistics.median([
                             row['rank_evidence']['before_finalize_rss_sum_mib']
                             for row in rows if row['comparison_label'] == label])}
    result['new_vs_old_latency_reduction_percent'] = (
        1 - result['new_rm']['median_us'] / result['old_rm']['median_us']) * 100
    result['new_vs_rc_latency_reduction_percent'] = (
        1 - result['new_rm']['median_us'] / result['rc']['median_us']) * 100
    result['scope'] = 'THIS_BATCH_8B_PPN1_ONLY_PERFORMANCE_REVIEW_REQUIRED'
    return result


def main():
    parser = argparse.ArgumentParser(description='RM默认4K候选：默认仅计划，--run才部署和运行')
    parser.add_argument('case', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('PLAN 新候选默认RM=4K RC/UM=8K；既有28项回归＋标准OSU 18项逐SEND矩阵＋五轮8B对照')
        print('运行时不注入SEG_SIZE；产品需隔离编译；最大PPN=4；C延期，192暂停')
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may run this candidate')
    case = args.case.resolve()
    build = validate_build(case)
    if Path(__file__).resolve() != case / 'source/contrib/rm_ctp/segment_default/run_candidate.py':
        raise ValueError('execute this candidate source, not another checkout')
    out = Path(tempfile.mkdtemp(prefix='segment_default_accept_', dir=case))
    receipt = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
               'controller_dir': str(out),
               'status': 'PREFLIGHT', 'performance_accepted': False, 'C_retry': 'DEFERRED'}
    print('DEFAULT_SEGMENT_ACCEPT_DIR=' + str(out), flush=True)
    try:
        receipt['deployment'] = deploy(case, build, out)
        with bind_candidate(osu, case, build):
            command, saved = osu.saved_spec(BASELINE_LAB / 'osu_minimal8_ff_14ns8/rm_run.json', case / 'install')
            env = osu.environment(saved, 'iface', case / 'install', osu.budget(4))
            receipt['defaults'] = default_capabilities(case / 'install', env, out)
            print('双端默认配置=PASS RM=4096 RC/UM=8192 AM_BCOPY=4093 SEG_ENV_OVERRIDE=NO', flush=True)
            receipt['normal_regression'] = normal_regression(case, out)
            print('既有正常矩阵=28/28 PASS；日志单独保留', flush=True)
            boundary_matrix(case, receipt)
            print('默认片长逐SEND矩阵=18/18 PASS PPN=4 RANKS=8/8 SEND_MAX=%dB SEG_ENV_OVERRIDE=NO' %
                  receipt['max_observed_send_bytes'], flush=True)
            # The existing WR runner restored environment; timing uses only identity probes.
            receipt['timing'] = timing_comparison(out / 'comparison', case, build)
        receipt['status'] = 'FUNCTIONAL_PASS_PERFORMANCE_REVIEW_REQUIRED'
        timing = receipt['timing']
        print('8B OSU中位数us=' + json.dumps({key: timing[key]['median_us']
              for key in ('old_rm', 'new_rm', 'rc')}, ensure_ascii=False), flush=True)
        print('新RM相对旧RM/RC时延降低=%.2f%%/%.2f%%；负值为变慢，仅本轮样本' % (
            timing['new_vs_old_latency_reduction_percent'], timing['new_vs_rc_latency_reduction_percent']), flush=True)
        print('Finalize前RSS两节点合计MiB=' + json.dumps({key: timing[key]['rss_sum_mib_median']
              for key in ('old_rm', 'new_rm', 'rc')}, ensure_ascii=False), flush=True)
        print('默认片长功能=PASS；性能待审阅，硬件最大报长与故障恢复未验收', flush=True)
    except BaseException as exc:
        receipt['status'] = 'STOPPED_INCOMPLETE'
        receipt['error'] = type(exc).__name__ + ': ' + str(exc)
        print('DEFAULT_SEGMENT_STOPPED=' + receipt['error'][:500], flush=True)
        raise SystemExit(1)
    finally:
        osu.base.write_json(out / 'results.json', receipt)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
