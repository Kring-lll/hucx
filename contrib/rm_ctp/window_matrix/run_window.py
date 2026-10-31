#!/usr/bin/env python3
"""Original OSU normal-window matrix on the pinned ARM product; no rebuild."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'segment_default'))
import run_candidate as candidate
from matrix import BENCHMARKS, PRODUCT_COMMIT, PRODUCT_MODULE, SOURCE_BASE, adapt_trial, cases

osu = candidate.osu
wr = candidate.wr
CASE = candidate.B_ROOT / 'r1_shared_80b28a25b_dvKqlu'


def plan():
    return {'source_base': SOURCE_BASE, 'product_commit': PRODUCT_COMMIT,
            'module_sha256': PRODUCT_MODULE, 'case': str(CASE),
            'standard_osu': '7.5.2', 'cases': cases(), 'trial_count': len(cases()),
            'iterations': 100, 'warmup': 10, 'max_ppn': 4,
            'performance_claim': False, 'size_alternation': 'NOT_COVERED',
            'transport_changed': False, 'C_retry': 'DEFERRED'}


@contextmanager
def benchmark_registry():
    previous = osu.BENCHMARKS
    osu.BENCHMARKS = BENCHMARKS
    try:
        yield
    finally:
        osu.BENCHMARKS = previous


def check_help(out):
    for name in ('osu_bw', 'osu_bibw'):
        text = (out / (name + '_help.txt')).read_text()
        if any(value not in text for value in ('--window-size', '--buffer-num')):
            raise ValueError('required original OSU window/multiple-buffer options missing: ' + name)


def prepare_helpers(out, report):
    """Use the already validated local-load forwarding probe and symbol smoke."""
    osu.base.ssh('set -eu\nmkdir -p "$2"\ntest ! -e "$1"\nmkdir "$1"\n', out, out.parent)
    files = ((osu.ROOT / 'osu_rank.py', 'osu_rank.py'),
             (osu.A89 / 'rank_exec.py', 'a89_rank_exec.py'),
             (wr.ROOT / 'send_probe.c', 'send_probe.c'),
             (wr.ROOT / 'symbol_smoke.py', 'symbol_smoke.py'),
             (wr.ROOT / 'tests/fake_provider.c', 'fake_provider.c'),
             (wr.ROOT / 'tests/local_client.c', 'local_client.c'))
    for source, name in files:
        shutil.copy2(source, out / name)
    for name in [name for _, name in files] + ['libosu_identity.so']:
        osu.base.copy_remote([out / name], str(out) + '/')
        if osu.base.ssh('sha256sum "$1"\n', out / name).split()[0] != osu.digest(out / name):
            raise ValueError('peer helper hash mismatch: ' + name)
    library = out / 'libsend_probe.so'
    command = ['gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
               '-I/usr/include/ub/umdk/urma', str(out / 'send_probe.c'),
               '-o', str(library), '-ldl', '-pthread']
    osu.base.run_logged(command, dict(os.environ), out / 'compile_probe.log', 60)
    peer_compile = osu.base.ssh('set -eu\ngcc -std=gnu11 -O2 -Wall -Wextra -Werror -shared -fPIC '
                              '-I/usr/include/ub/umdk/urma "$1" -o "$2" -ldl -pthread\n',
                              out / 'send_probe.c', library, timeout=60)
    (out / 'compile_probe_peer.log').write_text(peer_compile)
    report['probe_sha256'] = {'node89': osu.digest(library),
                             'node97': osu.base.ssh('sha256sum "$1"\n', library).split()[0]}
    smoke = subprocess.run([sys.executable, str(out / 'symbol_smoke.py'), str(out)],
                           text=True, capture_output=True, timeout=100)
    (out / 'symbol_smoke.log').write_text(smoke.stdout + smoke.stderr)
    if smoke.returncode:
        raise ValueError('local forwarding smoke failed; see symbol_smoke.log')
    report['symbol_smoke'] = {'node89': json.loads(smoke.stdout),
        'node97': json.loads(osu.base.ssh('python3 "$1" "$2"\n',
                                        out / 'symbol_smoke.py', out, timeout=100))}
    return library


@contextmanager
def traced_environment(library):
    previous = osu.environment

    def environment(*args, **kwargs):
        value = previous(*args, **kwargs)
        value['LD_PRELOAD'] = str(library) + ':' + value['LD_PRELOAD']
        return value

    osu.environment = environment
    try:
        yield
    finally:
        osu.environment = previous


def finish_trial(out, row, case, library):
    """A launcher success is insufficient: check defaults and all actual PIDs."""
    row['matrix_case'] = case
    run = out / row['name']
    try:
        candidate.check_trial_default(row, out)
        if not row['passed']:
            raise ValueError(row.get('error', 'launcher/rank/default gate failed'))
        if case['mode'] == 'rm':
            row['wr_fetch_sha256'] = wr.fetch_peer_summaries(run, row)
            row['send_observations'] = wr.inspect_trial(run, row, library)
            row['wr_probe_passed'] = True
    except Exception as exc:
        row['passed'] = False
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    osu.base.write_json(run / 'trial.json', row)
    return row


def main():
    parser = argparse.ArgumentParser(description='原版OSU窗口及同进程递增尺寸校验，默认只输出计划')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 600:
        parser.error('每个任务超时须60..600秒')
    if not (args.run or args.preflight_only):
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise ValueError('只允许root在node89显式执行；本工具不自动登录node89')
    build = candidate.validate_build(CASE)
    if build['source_commit'] != PRODUCT_COMMIT or build['module_sha256'] != PRODUCT_MODULE:
        raise ValueError('fixed node-tested product identity differs')
    out = Path(tempfile.mkdtemp(prefix='b_window_', dir=CASE))
    print('B_WINDOW_DIR=' + str(out), flush=True)
    report = {'plan': plan(), 'cases': [], 'status': 'PREFLIGHT',
              'full_B_acceptance': False, 'performance_accepted': False}
    toolkit = PROJECT / 'window_manifest.json'
    if toolkit.is_file():
        report['toolkit_manifest_sha256'] = osu.digest(toolkit)

    def save():
        osu.base.write_json(out / 'results.json', report)

    save()
    try:
        with candidate.bind_candidate(osu, CASE, build), benchmark_registry():
            osu.validate_package()
            commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                            (mode + '_run.json'), CASE / 'install')
                        for mode in ('rm', 'rc')}
            evidence = osu.preflight(out, commands)
            check_help(out)
            env = osu.environment(commands['rm'][1], 'iface', CASE / 'install', osu.budget(4))
            report['defaults'] = candidate.default_capabilities(CASE / 'install', env, out)
            report['preflight'] = evidence
            if min(row['available_cores'] for row in evidence['inventory']) < 4:
                raise ValueError('fewer than four allowed physical cores on a node')
            print('预检=PASS 固定产品/OSU/HMPI/默认片长；最高PPN4', flush=True)
            if not args.run:
                report['status'] = 'PREFLIGHT_PASS_NO_MPI'
                save()
                return
            library = prepare_helpers(out, report)
            report['status'] = 'RUNNING'
            save()
            trial_args = argparse.Namespace(iterations=100, warmup=10, timeout=args.timeout)
            for index, case in enumerate(cases()):
                with adapt_trial(osu, case):
                    if case['mode'] == 'rm':
                        with traced_environment(library):
                            row = osu.one_trial(out, commands, evidence, case['benchmark'],
                                                'rm', case['ppn'], 'validation', index,
                                                trial_args, message_bytes=case['sizes'][0])
                    else:
                        row = osu.one_trial(out, commands, evidence, case['benchmark'],
                                            'rc', case['ppn'], 'validation', index,
                                            trial_args, message_bytes=8)
                finish_trial(out, row, case, library)
                report['cases'].append(row)
                save()
                if not row['passed']:
                    raise ValueError(row['name'] + ': ' + row.get('error', 'trial failed'))
                sizes = case['sizes']
                maximum = max((item['max_send_bytes'] for item in row.get('send_observations', [])), default=0)
                print('OSU=%s MODE=%s PPN=%d SIZES=%d..%d ROWS=%d WINDOW=%s '
                      'DATA=PASS EXIT=0 MAPS=PASS SEND_MAX=%s' %
                      (case['benchmark'], case['mode'].upper(), case['ppn'], sizes[0], sizes[-1],
                       len(sizes), case['window'] or 'N/A', maximum or '未观察'), flush=True)
            report['status'] = 'SELECTED_NORMAL_WINDOW_MATRIX_PASS'
            report['validated_size_rows'] = sum(len(row['output']['rows']) for row in report['cases'])
            save()
            print('正常窗口矩阵=10/10 PASS；同进程递增扫描已测，大小反复交替未覆盖', flush=True)
            print('观察器/校验开启；本轮不作带宽/时延优势结论，C及RMA未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        print('B_WINDOW_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        save()
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
