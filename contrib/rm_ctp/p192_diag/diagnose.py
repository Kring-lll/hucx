#!/usr/bin/env python3
"""One PPN192 validation replay; original tested runner, no transport edits."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'osu_compare'))
import run as runner
from supervisor import Supervisor
from support import DEFAULT_CASE, LAB, PRODUCT_COMMIT, PRODUCT_MODULE, budget, digest, saved_spec

SOURCE = DEFAULT_CASE / 'osu_rm_scale_6ic354wy'


def validate():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if digest(ROOT / name) != expected:
            raise ValueError('diagnostic source changed: ' + name)
    runner.validate_package()
    return manifest


def main():
    parser = argparse.ArgumentParser(description='仅一轮PPN192定位，默认不运行MPI')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    manifest = validate()
    if not args.run:
        print('MODE=PLAN_ONLY MPI_JOBS=0; --run才执行一轮RM PPN192校验定位')
        print('CAPTURE=node89 ranks0/96/189 at90/150s; then stop own launcher')
        print('PRODUCT_UNCHANGED=YES FORMAL_PERFORMANCE_ACCEPTANCE=NO')
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise ValueError('由root在node89空闲窗口执行')
    if not Path('/usr/bin/gdb').is_file():
        raise ValueError('node89 gdb missing')
    old = json.loads((SOURCE / 'results.json').read_text())
    failed = json.loads((SOURCE / 'osu_multi_lat_rm_p192_validation_0/trial.json').read_text())
    if (old['highest_common_passed_ppn'] != 160 or old['status'] != 'STOPPED_INCOMPLETE' or
            failed['budget'] != budget(192) or failed['phase'] != 'validation' or
            failed['configuration']['arguments'] != runner.arguments('osu_multi_lat', 1000, 100, True) or
            failed['configuration']['resource_probe']):
        raise ValueError('source is not the recorded PPN192 validation failure')
    evidence = old['preflight']
    checked = runner.base.module_check(DEFAULT_CASE / 'install', PRODUCT_MODULE)
    for key in ('install','module_sha256','base_libraries_sha256'):
        if checked[key] != evidence['candidate'][key]:
            raise ValueError('candidate changed: ' + key)
    spec_path = LAB / 'osu_minimal8_ff_14ns8/rm_run.json'
    if digest(spec_path) != evidence['saved_launcher_sha256']['rm']:
        raise ValueError('saved launcher changed')
    binary = Path(evidence['benchmarks']['osu_multi_lat']['path'])
    expected = evidence['benchmarks']['osu_multi_lat']['sha256']
    if (digest(binary) != expected or
            runner.base.ssh('sha256sum "$1"\n', binary).split()[0] != expected):
        raise ValueError('OSU binary changed')
    if digest(SOURCE / 'libosu_identity.so') != evidence['identity_sha256']:
        raise ValueError('original identity probe changed')
    out = Path(tempfile.mkdtemp(prefix='p192_stack_', dir=DEFAULT_CASE))
    print('DIAGNOSTIC_DIR=' + str(out), flush=True)
    report = {'scope':'ONE_PPN192_DIAGNOSTIC_NOT_PERFORMANCE', 'manifest':manifest,
              'source_report':str(SOURCE/'results.json'), 'source_report_sha256':digest(SOURCE/'results.json'),
              'product_commit':PRODUCT_COMMIT, 'module_sha256':PRODUCT_MODULE,
              'stacks':[], 'intentional_early_stop':False, 'formal_acceptance':False}
    supervisor = Supervisor(runner, out, report)
    supervisor.save()
    runner.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
    for origin in (SOURCE/'osu_rank.py',SOURCE/'a89_rank_exec.py',SOURCE/'libosu_identity.so',ROOT/'proc_guard.py'):
        expected_source = {'osu_rank.py':runner.ROOT/'osu_rank.py',
                           'a89_rank_exec.py':runner.A89/'rank_exec.py'}.get(origin.name)
        if expected_source is not None and digest(origin) != digest(expected_source):
            raise ValueError('original rank helper changed: ' + origin.name)
        destination = out / origin.name
        shutil.copy2(origin, destination)
        runner.base.copy_remote([destination], str(out)+'/')
        if runner.base.ssh('sha256sum "$1"\n',destination).split()[0] != digest(destination):
            raise ValueError('peer helper hash differs')
    runner.base.run_logged = supervisor
    try:
        row = runner.one_trial(out, {'rm':saved_spec(spec_path,DEFAULT_CASE/'install')}, evidence,
                'osu_multi_lat','rm',192,'validation',0,
                types.SimpleNamespace(iterations=5000,warmup=1000,timeout=600))
        report['trial'] = row
        if row['passed']:
            report['status'] = 'SINGLE_VALIDATION_PASSED_ORIGINAL_FAILURE_NOT_REPRODUCED'
        elif report['intentional_early_stop']:
            report['status'] = ('EARLY_STOP_DIAGNOSTIC_ONLY' if any(r['passed'] for r in report['stacks'])
                                else 'EARLY_STOP_NO_USABLE_STACKS')
        else:
            report['status'] = 'REPLAY_FAILED_READ_ORIGINAL_LOG'
        if row.get('cleanup_error'):
            report['cleanup_attention'] = row['cleanup_error']
        supervisor.save()
        print('STATUS=' + report['status'],flush=True)
        print('STACK_CAPTURES=%d/6' % sum(r['passed'] for r in report['stacks']),flush=True)
        print('CLEANUP_COUNTS=' + str({k:len(v['terminated_own_pids'])
              for k,v in row.get('cleanup',{}).items()}),flush=True)
        print('FORMAL_PPN192_ACCEPTANCE=NO HIGHEST_PREVIOUS_COMMON_PPN=160',flush=True)
        print('REPORT=' + str(out/'diagnostic.json'),flush=True)
        if report.get('cleanup_attention'):
            print('CLEANUP_ATTENTION=' + str(report['cleanup_attention'])[:500],flush=True)
    finally:
        runner.base.run_logged = supervisor.original


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('P192_DIAGNOSTIC_FAILED=' + type(exc).__name__ + ': ' + str(exc)[:800],file=sys.stderr)
        raise SystemExit(1)
