#!/usr/bin/env python3
"""New product: native WRITE first, then the existing WRITE-enabled matrix."""
import argparse
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


def stages(case, write, normal, build):
    previous_argv = sys.argv
    before = set(case.glob('native_write_*'))
    try:
        sys.argv = [str(ROOT / 'run_candidate.py'), str(case), '--run']
        write.main()
        created = set(case.glob('native_write_*')) - before
        if len(created) != 1:
            raise ValueError('new native WRITE result identity is ambiguous')
        proof = created.pop()
        report = json.loads((proof / 'results.json').read_text())
        if report.get('status') != 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS':
            raise ValueError('new native WRITE did not pass')
        # The generic normal validator must bind to THIS newly built product;
        # the old 343d proof remains historical and cannot validate new code.
        normal.CASE = case
        normal.COMMIT = build['source_commit']
        normal.MODULE = build['module_sha256']
        normal.PROOF = proof.name
        before_normal = set(case.glob('write_normal_*'))
        sys.argv = [str(ROOT / 'normal_validation.py'), '--run']
        normal.main()
        created_normal = set(case.glob('write_normal_*')) - before_normal
        if len(created_normal) != 1:
            raise ValueError('new normal result identity is ambiguous')
        normal_result = created_normal.pop() / 'results.json'
        summary = json.loads(normal_result.read_text())
        if summary.get('status') != 'WRITE_ENABLED_NORMAL_28_PASS_RMA_INVENTORY_COMPLETE':
            raise ValueError('new normal matrix incomplete')
        return {'native_write': str(proof / 'results.json'), 'normal': str(normal_result)}
    finally:
        sys.argv = previous_argv


def main():
    parser = argparse.ArgumentParser(description='pending修复候选：新产品6轮WRITE后接28正常回归')
    parser.add_argument('case', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('PLAN=new product rebuild required; 6 native WRITE rounds then existing WRITE-enabled normal 28; max 1+4 ranks; no RMA OSU performance')
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89')
    case = args.case.resolve()
    if ROOT != case / 'source/contrib/rm_ctp/native_write':
        raise ValueError('run from the new isolated CASE source')
    write = load('pending_fix_write', ROOT / 'run_candidate.py')
    normal = load('pending_fix_normal', ROOT / 'normal_validation.py')
    build = normal.base.validate_build(case)
    if build['source_commit'] == '343d934fea013fb98c4bb5465b6eeb8aef39ac44':
        raise ValueError('old product has not received this pending fix')
    out = Path(tempfile.mkdtemp(prefix='pending_progress_accept_', dir=case))
    result = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
              'status': 'RUNNING', 'performance_accepted': False, 'full_B4_accepted': False}
    print('PENDING_PROGRESS_ACCEPT_DIR=' + str(out), flush=True)
    try:
        result['stages'] = stages(case, write, normal, build)
        result['status'] = 'NEW_PRODUCT_WRITE_AND_ENABLED_NORMAL_MATRIX_PASS'
    except BaseException as exc:
        result['status'] = 'STOPPED_INCOMPLETE'
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        print('PENDING_PROGRESS_STOPPED=' + result['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        (out / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print('PENDING_PROGRESS_RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
