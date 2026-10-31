#!/usr/bin/env python3
"""One rebuilt product: READ, WRITE, then the existing normal matrix."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def both_enabled(legacy):
    original = legacy.values
    def values(mode, debug=False):
        result = original(mode, debug)
        result['UCX_RM_URMA_NATIVE_WRITE'] = 'y'
        result['UCX_RM_URMA_NATIVE_READ'] = 'y'
        return result
    legacy.values = values
    try:
        yield
    finally:
        legacy.values = original


def new_result(case, prefix, before, status, build):
    created = set(case.glob(prefix + '*')) - before
    if len(created) != 1:
        raise ValueError('ambiguous new result: ' + prefix)
    path = created.pop() / 'results.json'
    result = json.loads(path.read_text())
    if (result.get('status') != status or
            result.get('source_commit') != build['source_commit'] or
            result.get('module_sha256') != build['module_sha256']):
        raise ValueError('new result product/status differs: ' + prefix)
    return path


def stages(case, read, write, normal, build):
    previous_argv = sys.argv
    original_environment = write.trial_environment
    original_enabled = normal.enabled_normal
    try:
        before = set(case.glob('native_read_*'))
        sys.argv = [str(ROOT / 'run_candidate.py'), str(case), '--run']
        read.main()
        read_result = new_result(case, 'native_read_', before,
                                'DIRECT_NATIVE_READ_DATA_MAPS_REGISTRATION_RELEASE_PASS', build)
        # The previously accepted WRITE probe requires READ disabled. Test that
        # independent mode, then test both flags together in the normal matrix.
        def write_environment(candidate):
            result = original_environment(candidate)
            result['UCX_RM_URMA_NATIVE_READ'] = 'n'
            return result
        write.trial_environment = write_environment
        before = set(case.glob('native_write_*'))
        sys.argv = [str(ROOT.parent / 'native_write/run_candidate.py'), str(case), '--run']
        write.main()
        write_result = new_result(case, 'native_write_', before,
                                 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS', build)
        normal.CASE = case
        normal.COMMIT = build['source_commit']
        normal.MODULE = build['module_sha256']
        normal.PROOF = write_result.parent.name
        normal.enabled_normal = both_enabled
        before = set(case.glob('write_normal_*'))
        sys.argv = [str(ROOT.parent / 'native_write/normal_validation.py'), '--run']
        normal.main()
        normal_result = new_result(case, 'write_normal_', before,
                                  'WRITE_ENABLED_NORMAL_28_PASS_RMA_INVENTORY_COMPLETE', build)
        # Recheck the actual command receipts; a controller flag alone is not
        # evidence that every rank launcher received READ=y.
        summary = json.loads(normal_result.read_text())
        matrix_path = Path(summary['normal']['results_json'])
        matrix = json.loads(matrix_path.read_text())
        for row in matrix['cases']:
            command = json.loads(Path(row['log']).with_suffix('.json').read_text())
            if command['environment'].get('UCX_RM_URMA_NATIVE_READ') != 'y':
                raise ValueError('READ flag missing in normal receipt: ' + row['name'])
        return {'read': str(read_result), 'write': str(write_result), 'normal': str(normal_result)}
    finally:
        sys.argv = previous_argv
        write.trial_environment = original_environment
        normal.enabled_normal = original_enabled


def main():
    parser = argparse.ArgumentParser(description='新产品READ六轮、WRITE六轮及双开关28正常回归')
    parser.add_argument('case', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('PLAN=isolated rebuild; READ 6 + WRITE 6 + normal 28 with both enabled; max PPN4; no MPI RMA performance')
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89')
    case = args.case.resolve()
    if ROOT != case / 'source/contrib/rm_ctp/native_read':
        raise ValueError('run from the new isolated CASE source')
    read = load('read_candidate', ROOT / 'run_candidate.py')
    write = load('read_regression_write', ROOT.parent / 'native_write/run_candidate.py')
    normal = load('read_regression_normal', ROOT.parent / 'native_write/normal_validation.py')
    build = normal.base.validate_build(case)
    out = Path(tempfile.mkdtemp(prefix='native_read_accept_', dir=case))
    report = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
              'status': 'RUNNING', 'MPI_RMA_accepted': False, 'performance_accepted': False,
              'full_B4_accepted': False, 'default_read': False, 'default_write': False}
    print('NATIVE_READ_ACCEPT_DIR=' + str(out), flush=True)
    try:
        report['stages'] = stages(case, read, write, normal, build)
        report['status'] = 'NATIVE_READ_WRITE_AND_ENABLED_NORMAL_28_PASS'
        print('READ_6_WRITE_6_NORMAL_28=PASS；MPI单边/正式OSU/完整B4待验', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('NATIVE_READ_ACCEPT_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('NATIVE_READ_RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
