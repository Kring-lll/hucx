#!/usr/bin/env python3
"""Continue only the pinned controller_dir interruption without changing CASE source."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile

COMMIT = 'ac4146054b0e1364d0ea691b37b9f14238a43bb0'
MODULE = 'c211385e1fa4a5c4096f49cdc74b69af8ec82104e80edb9b460e0b7667789308'
CASE = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_ac4146054_sbFyVf')
CHECKPOINT = CASE / 'rkey_accept__txtvu2a/results.json'
ENTRY_SHA256 = 'df4b61f2c1d53c1f9d46550f2d2a60df61c0beffc3cb45337b33dd38e731a1a6'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(report):
    if report.get('source_commit') != COMMIT or report.get('module_sha256') != MODULE:
        raise ValueError('checkpoint product identity differs')


def checkpoint(case, path):
    case = case.resolve()
    path = path.resolve()
    if path.name != 'results.json' or path.parent.parent != case:
        raise ValueError('checkpoint must belong to this isolated CASE')
    old = json.loads(path.read_text())
    identity(old)
    if old.get('status') != 'STOPPED_INCOMPLETE' or old.get('error') != "KeyError: 'controller_dir'":
        raise ValueError('only the controller_dir interruption can be resumed')
    if any(key in old for key in ('boundary_matrix', 'timing', 'window_and_am', 'collective')):
        raise ValueError('checkpoint contains unexpected later-stage results')
    normal = old.get('normal_regression', {})
    if normal.get('passed') is not True or normal.get('cases') != 28:
        raise ValueError('normal gate is incomplete')
    source = Path(normal['results_json']).resolve()
    if (source.name != 'results.json' or source.parent.parent != case
            or not source.parent.name.startswith('iface_accept_')):
        raise ValueError('normal result points outside this CASE')
    report = json.loads(source.read_text())
    identity(report)
    expected = {'新模式加载与8B校验'}
    expected.update('%s_8B_%d' % (mode, repeat)
                    for mode in ('legacy', 'iface', 'rc') for repeat in range(3))
    expected.update('%s_%s_peers%d' % (mode, pattern, peers)
                    for mode in ('legacy', 'iface', 'rc')
                    for pattern in ('many', 'burst') for peers in (1, 2, 4))
    rows = report.get('cases', [])
    names = [row.get('name') for row in rows]
    if (len(rows) != 28 or set(names) != expected or len(set(names)) != 28
            or report.get('functional_acceptance') is not True
            or not all(row.get('passed') is True for row in rows)):
        raise ValueError('normal 28-case coverage or outcome differs')
    return normal, {'source_result': str(path), 'source_sha256': digest(path),
                    'normal_result': str(source), 'normal_sha256': digest(source),
                    'reused_normal_tasks': 28, 'reason': 'CONTROLLER_DIR_ONLY'}


def load_controller(case):
    entry = case / 'source/contrib/rm_ctp/rkey_lifecycle/run_candidate.py'
    if digest(entry) != ENTRY_SHA256:
        raise ValueError('original CASE controller differs; do not overwrite source')
    spec = importlib.util.spec_from_file_location('hucx_original_rkey_controller', entry)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def run(runner, case, build, out, normal, receipt):
    report = {'plan': runner.plan(), 'source_commit': build['source_commit'],
              'module_sha256': build['module_sha256'], 'controller_dir': str(out),
              'checkpoint': receipt, 'normal_regression': normal, 'status': 'RUNNING'}
    print('RKEY_RESUME_DIR=' + str(out), flush=True)
    try:
        # Existing peer CASE is checked, never overwritten on a hash mismatch.
        report['deployment'] = runner.candidate.deploy(case, build, out)
        print('检查点及双端候选库=PASS；复用28项正常回归，从边界矩阵继续', flush=True)
        with runner.candidate.bind_candidate(runner.osu, case, build):
            runner.candidate.boundary_matrix(case, report)
            print('既有边界矩阵=18/18 PASS', flush=True)
            runner.pending.TESTED_CASE = runner.REFERENCE_CASE
            runner.pending.TESTED_COMMIT = runner.REFERENCE_COMMIT
            runner.pending.TESTED_MODULE = runner.REFERENCE_MODULE
            report['timing'] = runner.pending.timing(out / 'comparison', case, build)
        print('8B OSU五轮中位数us=' + json.dumps({
            key: report['timing'][key]['median_us']
            for key in ('old_rm', 'new_rm', 'rc')}, ensure_ascii=False), flush=True)
        report['window_and_am'] = runner.pending.matrix(out / 'window_and_am', case, build)
        report['collective'] = runner.collective_matrix(out / 'collective', case, build)
        report['status'] = 'RKEY_FIX_AM_REGRESSION_PASS_PERFORMANCE_REVIEW_REQUIRED'
        print('既有AM/RC功能回归=PASS；rkey设备导入/READ/WRITE仍未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        runner.osu.base.write_json(out / 'results.json', report)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description='仅续跑已确认的rkey脚本字段中断，默认只列计划')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({'case': str(CASE), 'checkpoint': str(CHECKPOINT),
                          'product_commit': COMMIT, 'module_sha256': MODULE,
                          'reuse_normal_tasks': 28, 'max_ppn': 4,
                          'product_rebuild': False, 'native_RMA_enabled': False},
                         ensure_ascii=False, indent=2))
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise ValueError('only root on node89 may resume')
    normal, receipt = checkpoint(CASE, CHECKPOINT)
    runner = load_controller(CASE)
    build = runner.candidate.validate_build(CASE)
    identity(build)
    out = Path(tempfile.mkdtemp(prefix='rkey_resume_', dir=CASE))
    run(runner, CASE, build, out, normal, receipt)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('RKEY_RESUME_STOPPED=' + type(exc).__name__ + ': ' + str(exc)[:700], flush=True)
        raise SystemExit(1)
