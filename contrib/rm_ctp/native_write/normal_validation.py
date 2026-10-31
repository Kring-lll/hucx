#!/usr/bin/env python3
"""Reuse native WRITE evidence; run one existing 28-case matrix with WRITE on."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'segment_default'))
spec = importlib.util.spec_from_file_location('write_normal_base', ROOT.parent / 'segment_default/run_candidate.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
CASE = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_343d934fe_vpkpQc')
COMMIT = '343d934fea013fb98c4bb5465b6eeb8aef39ac44'
MODULE = '5373b75bbfc7429ba34f9f84191886ba1eaaf0b0bde2308a76ab5ed7e8b0122c'
PROOF = 'native_write_0e8m3w04'


def read(path):
    return json.loads(Path(path).read_text())


def prior_write(case=None):
    case = CASE if case is None else Path(case)
    root = case / PROOF
    report = read(root / 'results.json')
    if (report.get('status') != 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS'
            or report.get('source_commit') != COMMIT or report.get('module_sha256') != MODULE
            or report.get('sender_exit') != 0 or report.get('receiver_exit') != 0
            or report.get('GET_enabled') is not False):
        raise ValueError('native WRITE proof identity/status differs')
    expected = [(str(i), str((8, 64, 4096)[i % 3])) for i in range(6)]
    for role in ('sender', 'receiver'):
        text = (root / (role + '.log')).read_text(errors='replace')
        actual = re.findall(r'^NATIVE_WRITE_ROUND=(\d+) BYTES=(\d+) ROLE=' + role + r' DATA=PASS$', text, re.M)
        if actual != expected or 'NATIVE_WRITE_PASS ROUNDS=6 ' not in text:
            raise ValueError('native WRITE data receipt differs: ' + role)
    return {'results_json': str(root / 'results.json'), 'sha256': base.osu.digest(root / 'results.json'),
            'scope': 'PRIOR_6_WRITE_ROUNDS_REUSED_NOT_RERUN'}


@contextmanager
def enabled_normal(legacy):
    original = legacy.values
    def values(mode, debug=False):
        result = original(mode, debug)
        result['UCX_RM_URMA_NATIVE_WRITE'] = 'y'
        return result
    legacy.values = values
    try:
        yield
    finally:
        legacy.values = original


def check_normal_receipts(result, case=None):
    case = CASE if case is None else Path(case)
    path = Path(result['results_json'])
    if path.resolve().parent.parent != case.resolve():
        raise ValueError('normal receipt outside candidate CASE')
    report = read(path)
    if report.get('source_commit') != COMMIT or report.get('module_sha256') != MODULE:
        raise ValueError('normal product identity differs')
    cases = report['cases']
    if (len(cases) != 28 or len({r['name'] for r in cases}) != 28
            or not all(r['passed'] for r in cases) or report.get('functional_acceptance') is not True):
        raise ValueError('normal 28-case matrix incomplete')
    for row in cases:
        command = read(Path(row['log']).with_suffix('.json'))
        if command['environment'].get('UCX_RM_URMA_NATIVE_WRITE') != 'y':
            raise ValueError('WRITE flag missing from actual command receipt: ' + row['name'])
    return {'cases': 28, 'native_write': 'y', 'median_8B_us': report['median_8B_us'],
            'source_result_sha256': base.osu.digest(path)}


def local_inventory(root, info):
    rows = {}
    for name in ('osu_put_latency', 'osu_put_bw', 'osu_put_bibw'):
        paths = sorted(p for p in root.rglob(name) if p.is_file() and os.access(p, os.X_OK))
        rows[name] = [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
    env = dict(os.environ, OMPI_MCA_pml='ob1', OMPI_MCA_coll='basic,self,libnbc')
    env.pop('LD_PRELOAD', None)
    env.pop('LD_DEBUG', None)
    result = subprocess.run([str(info), '--parsable', '--param', 'osc', 'all'],
                            env=env, text=True, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError('ompi_info OSC inventory failed: ' + result.stderr[-500:])
    return {'benchmarks': rows, 'ompi_info': str(info), 'ompi_info_sha256': hashlib.sha256(info.read_bytes()).hexdigest(),
            'osc_components': result.stdout, 'osc_ucx_reported': 'mca:osc:ucx:' in result.stdout,
            'scope': 'FILE_AND_COMPONENT_INVENTORY_ONLY_NO_OSU_OR_MPI_BENCHMARK_LAUNCHED'}


def inventory(out):
    lab = base.BASELINE_LAB
    saved = read(lab / 'osu_minimal8_ff_14ns8/rm_run.json')
    launcher = next(Path(v) for v in saved['command'] if Path(v).name in ('mpirun', 'mpiexec'))
    info = launcher.with_name('ompi_info')
    root = lab / 'osu_build_7.5.2_20261020_AOlmOB/install'
    local = local_inventory(root, info)
    # Send exactly this same inventory function; no remote directory assumptions.
    import ast
    source = globals().get('OVERLAY_CODE')
    if source is None:
        source = Path(__file__).read_text()
    function = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'local_inventory')
    program = 'import os,json,hashlib,subprocess\nfrom pathlib import Path\n' + ast.get_source_segment(source, function)
    program += '\nprint(json.dumps(local_inventory(Path(' + repr(str(root)) + '), Path(' + repr(str(info)) + '))))\n'
    peer = json.loads(base.osu.base.ssh('python3 -c "$1"\n', program, timeout=45))
    for name in local['benchmarks']:
        if local['benchmarks'][name] != peer['benchmarks'][name]:
            raise ValueError('two-node OSU files differ: ' + name)
    if local['ompi_info_sha256'] != peer['ompi_info_sha256']:
        raise ValueError('two-node ompi_info differs')
    result = {'node89': local, 'node97': peer, 'RMA_benchmark_run': False}
    base.osu.base.write_json(out / 'rma_inventory.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description='复用WRITE证据，开启WRITE做28正常回归；RMA仅文件/组件核查')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('PLAN=prior 6 WRITE rounds reused; one 28-case normal matrix with NATIVE_WRITE=y; max 1+4 ranks; RMA file/OSC inventory only; no rebuild')
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89')
    build = base.validate_build(CASE)
    if build['source_commit'] != COMMIT or build['module_sha256'] != MODULE:
        raise ValueError('wrong product')
    checkpoint = prior_write()
    out = Path(tempfile.mkdtemp(prefix='write_normal_', dir=CASE))
    report = {'source_commit': COMMIT, 'module_sha256': MODULE, 'status': 'RUNNING',
              'prior_write': checkpoint, 'native_write': 'y', 'RMA_OSU_accepted': False,
              'full_B4_accepted': False, 'overlay_sha256': globals().get('OVERLAY_SHA256')}
    print('WRITE_NORMAL_DIR=' + str(out), flush=True)
    try:
        report['deployment'] = base.deploy(CASE, build, out)
        sys.path.insert(0, str(ROOT.parent / 'r1'))
        import accept_iface as legacy
        with enabled_normal(legacy):
            report['normal'] = base.normal_regression(CASE, out)
        report['normal_checks'] = check_normal_receipts(report['normal'])
        print('WRITE开启后的既有正常矩阵=28/28 PASS；六轮原生WRITE证据复用，未重跑', flush=True)
        print('本轮8B中位数us=' + json.dumps(report['normal_checks']['median_8B_us']), flush=True)
        print('legacy为同一新库的旧建链模式，不是改造前产品；本批小样本不作全性能结论', flush=True)
        report['inventory'] = inventory(out)
        for name, paths in report['inventory']['node89']['benchmarks'].items():
            print('RMA_BINARY=' + name + ' COUNT=' + str(len(paths)), flush=True)
        print('OSC_UCX_REPORTED=' + str(report['inventory']['node89']['osc_ucx_reported']) + '; NO_RMA_BENCHMARK_RUN=YES', flush=True)
        report['status'] = 'WRITE_ENABLED_NORMAL_28_PASS_RMA_INVENTORY_COMPLETE'
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('WRITE_NORMAL_STOPPED=' + report['error'][:700], flush=True)
        print('NORMAL_LOG=' + str(out / 'normal_28_regression.log'), flush=True)
        raise SystemExit(1)
    finally:
        base.osu.base.write_json(out / 'results.json', report)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
