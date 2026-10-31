#!/usr/bin/env python3
"""Shared rkey prerequisite fix; original-OSU AM/RC regression, not RMA acceptance."""
import argparse
import json
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile

PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    'hucx_rkey_pending_reference', PROJECT / 'pending_order/run_candidate.py')
pending = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pending)
sys.path.insert(0, str(PROJECT / 'collective_matrix'))
import run_collective as collective

candidate = pending.candidate
osu = pending.osu
REFERENCE_COMMIT = 'c7ef02b51d274e4ff3151457b82cc34f493419d5'
REFERENCE_MODULE = 'cc12a6ed9299f87f36f927b9d4f8f8043810ec3273a96a55734a4cc89b2848f2'
REFERENCE_CASE = candidate.B_ROOT / 'r1_shared_c7ef02b51_vkSZ69'


def plan():
    return {'source_base': '82cff2375b58768fa02c28ce961cef5d86676158',
            'reference_product': REFERENCE_COMMIT,
            'product_files': ['src/uct/urma/base/urma_md.c',
                              'src/uct/urma/base/urma_md.h'],
            'isolated_rebuild_required': True, 'normal_tasks': 28,
            'boundary_tasks': 18, 'window_tasks': 10, 'am_tasks': 10,
            'collective_tasks': 10, 'timing_trials': 15,
            'max_ppn': 4, 'max_message_bytes': 1048576,
            'native_RMA_enabled': False, 'native_RMA_accepted': False,
            'C_retry': 'DEFERRED', 'performance_accepted': False,
            'full_B_acceptance': False}


def collective_matrix(out, case, build):
    out.mkdir()
    report = {'cases': [], 'status': 'RUNNING',
              'source_commit': build['source_commit'],
              'module_sha256': build['module_sha256']}
    try:
        with candidate.bind_candidate(osu, case, build), collective.registry():
            osu.validate_package()
            commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                            (mode + '_run.json'), case / 'install')
                        for mode in ('rm', 'rc')}
            evidence = osu.preflight(out, commands)
            report['preflight'] = evidence
            collective.check_help(out)
            if min(item['available_cores'] for item in evidence['inventory']) < 4:
                raise ValueError('fewer than four allowed physical cores on a node')
            library = collective.window.prepare_helpers(out, report)
            args = argparse.Namespace(iterations=20, warmup=2, timeout=240)
            for index, item in enumerate(collective.cases()):
                with collective.profile(osu, item, out, index, collective.log_pattern):
                    if item['mode'] == 'rm':
                        with collective.window.traced_environment(library):
                            row = osu.one_trial(out, commands, evidence, item['benchmark'],
                                                'rm', item['ppn'], 'validation', index, args,
                                                message_bytes=item['sizes'][0])
                    else:
                        row = osu.one_trial(out, commands, evidence, item['benchmark'],
                                            'rc', item['ppn'], 'validation', index, args,
                                            message_bytes=item['sizes'][0])
                collective.finish(out, row, item, evidence, library)
                report['cases'].append(row)
                osu.base.write_json(out / 'results.json', report)
                if not row['passed']:
                    raise ValueError(row['name'] + ': ' + row.get('error', 'failed'))
                print('COLLECTIVE OSU=%s MODE=%s PPN=%d SIZES=%s DATA/MAPS/EXIT=PASS' %
                      (item['benchmark'], item['mode'].upper(), item['ppn'], item['sizes']),
                      flush=True)
        report['status'] = 'SELECTED_COLLECTIVE_NORMAL_MATRIX_PASS'
        return str(out / 'results.json')
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        osu.base.write_json(out / 'results.json', report)


def main():
    parser = argparse.ArgumentParser(description='rkey修复候选的既有AM/RC回归，默认仅计划')
    parser.add_argument('case', type=Path, nargs='?')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if args.case is None:
        parser.error('--run requires the isolated candidate CASE')
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may start this candidate')
    case = args.case.resolve()
    if Path(__file__).resolve() != case / 'source/contrib/rm_ctp/rkey_lifecycle/run_candidate.py':
        raise ValueError('run this entry from the isolated candidate source')
    build = candidate.validate_build(case)
    if build['source_commit'] == REFERENCE_COMMIT:
        raise ValueError('reference product is not the new candidate')
    out = Path(tempfile.mkdtemp(prefix='rkey_accept_', dir=case))
    report = {'plan': plan(), 'source_commit': build['source_commit'],
              'module_sha256': build['module_sha256'], 'controller_dir': str(out),
              'status': 'RUNNING'}
    print('RKEY_ACCEPT_DIR=' + str(out), flush=True)
    try:
        report['deployment'] = candidate.deploy(case, build, out)
        with candidate.bind_candidate(osu, case, build):
            report['normal_regression'] = candidate.normal_regression(case, out)
            print('既有正常矩阵=28/28 PASS', flush=True)
            candidate.boundary_matrix(case, report)
            print('既有边界矩阵=18/18 PASS', flush=True)
            pending.TESTED_CASE = REFERENCE_CASE
            pending.TESTED_COMMIT = REFERENCE_COMMIT
            pending.TESTED_MODULE = REFERENCE_MODULE
            report['timing'] = pending.timing(out / 'comparison', case, build)
        print('8B OSU五轮中位数us=' + json.dumps({
            label: report['timing'][label]['median_us']
            for label in ('old_rm', 'new_rm', 'rc')}, ensure_ascii=False), flush=True)
        report['window_and_am'] = pending.matrix(out / 'window_and_am', case, build)
        report['collective'] = collective_matrix(out / 'collective', case, build)
        report['status'] = 'RKEY_FIX_AM_REGRESSION_PASS_PERFORMANCE_REVIEW_REQUIRED'
        print('既有AM/RC功能回归=PASS；rkey真实设备导入/READ/WRITE仍未验收', flush=True)
        print('性能须审阅全部样本；原生RMA能力关闭，完整B/C未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('RKEY_ACCEPT_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        osu.base.write_json(out / 'results.json', report)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
