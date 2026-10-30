#!/usr/bin/env python3
"""Rebuild-required RM ordering fix: reuse existing original-OSU acceptance."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'am_rndv'))
import run_rndv as am

window = am.window
candidate = am.candidate
osu = am.osu
TESTED_CASE = candidate.B_ROOT / 'r1_shared_80b28a25b_dvKqlu'
TESTED_COMMIT = '80b28a25b5ea0e245bca88db44be79d15d06127f'
TESTED_MODULE = '8d9ed4133c71c4ca77ad900e60433e9781cfef2adf33dc396f3f642b2cc9821c'


def plan():
    return {'source_base': '9e67e26ba061b613f9cef8b19f027266c454e965',
            'product_changed': True, 'isolated_build_required': True,
            'normal_regression': 28, 'boundary_matrix': 18,
            'window_matrix': 10, 'am_rendezvous_matrix': 10,
            'timing_rounds': 5, 'timing_reference_product': TESTED_COMMIT,
            'max_ppn': 4, 'max_message_bytes': 1048576,
            'C_retry': 'DEFERRED', 'native_RMA_accepted': False,
            'size_alternation': 'NOT_COVERED', 'full_B_acceptance': False}


def timing(out, case, build):
    """Keep the latest tested 4K product as the rollback/performance reference."""
    names = ('OLD_CASE', 'OLD_COMMIT', 'OLD_MODULE')
    previous = {name: getattr(candidate, name) for name in names}
    try:
        candidate.OLD_CASE = TESTED_CASE
        candidate.OLD_COMMIT = TESTED_COMMIT
        candidate.OLD_MODULE = TESTED_MODULE
        return candidate.timing_comparison(out, case, build)
    finally:
        for name, value in previous.items():
            setattr(candidate, name, value)


def matrix(out, case, build):
    out.mkdir()
    report = {'source_commit': build['source_commit'],
              'module_sha256': build['module_sha256'], 'cases': [],
              'status': 'RUNNING', 'performance_accepted': False}

    def save():
        osu.base.write_json(out / 'results.json', report)

    save()
    try:
        with candidate.bind_candidate(osu, case, build), window.benchmark_registry():
            osu.validate_package()
            commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                            (mode + '_run.json'), case / 'install')
                        for mode in ('rm', 'rc')}
            evidence = osu.preflight(out, commands)
            report['preflight'] = evidence
            window.check_help(out)
            env = osu.environment(commands['rm'][1], 'iface', case / 'install', osu.budget(4))
            env.update(am.settings(am.cases()[3]))
            report['defaults'] = candidate.default_capabilities(case / 'install', env, out)
            report['am_only_capabilities'] = {
                host: am.check_am_only_capability(
                    (out / (host + '_ucx_device.txt')).read_text(), am.parse_capability)
                for host in ('node89', 'node97')}
            if min(item['available_cores'] for item in evidence['inventory']) < 4:
                raise ValueError('fewer than four allowed physical cores on one node')
            library = window.prepare_helpers(out, report)
            tasks = [('WINDOW', item) for item in window.cases()]
            tasks += [('AM_RNDV', item) for item in am.cases()]
            for index, (family, item) in enumerate(tasks):
                profile = (am.trial_profile(osu, item, am.window_matrix.parse_output,
                                            log_directory=am.trial_directory(out, item, index))
                           if family == 'AM_RNDV' else window.adapt_trial(osu, item))
                args = argparse.Namespace(iterations=20 if family == 'AM_RNDV' else 100,
                                          warmup=2 if family == 'AM_RNDV' else 10,
                                          timeout=240)
                with profile:
                    if item['mode'] == 'rm':
                        with window.traced_environment(library):
                            row = osu.one_trial(out, commands, evidence, item['benchmark'],
                                                'rm', item['ppn'], 'validation', index,
                                                args, message_bytes=item['sizes'][0])
                    else:
                        row = osu.one_trial(out, commands, evidence, item['benchmark'],
                                            'rc', item['ppn'], 'validation', index,
                                            args, message_bytes=8)
                window.finish_trial(out, row, item, library)
                if family == 'AM_RNDV':
                    am.finish_protocol(out, row, item, evidence)
                row['family'] = family
                report['cases'].append(row)
                save()
                if not row['passed']:
                    raise ValueError(row['name'] + ': ' + row.get('error', 'trial failed'))
                maximum = max((r['max_send_bytes'] for r in row.get('send_observations', [])), default=0)
                print('%s OSU=%s MODE=%s PPN=%d SIZE=%s DATA/MAPS/EXIT=PASS SEND_MAX=%s PATH=%s' %
                      (family, item['benchmark'], item['mode'].upper(), item['ppn'],
                       ','.join(map(str, item['sizes'])), maximum or '未观察',
                       item.get('path', 'eager')), flush=True)
            report['status'] = 'SELECTED_WINDOW_AND_AM_RNDV_MATRIX_PASS'
            return {'results_json': str(out / 'results.json'), 'window_tasks': 10, 'am_tasks': 10}
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        save()


def main():
    parser = argparse.ArgumentParser(description='RM pending顺序修复候选；需独立构建，默认仅计划')
    parser.add_argument('case', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may run this candidate')
    case = args.case.resolve()
    build = candidate.validate_build(case)
    if Path(__file__).resolve() != case / 'source/contrib/rm_ctp/pending_order/run_candidate.py':
        raise ValueError('execute the runner from this candidate source')
    if build['source_commit'] == TESTED_COMMIT:
        raise ValueError('the failing old product is not the fixed candidate')
    out = Path(tempfile.mkdtemp(prefix='pending_order_accept_', dir=case))
    report = {'plan': plan(), 'source_commit': build['source_commit'],
              'module_sha256': build['module_sha256'], 'controller_dir': str(out),
              'status': 'RUNNING', 'performance_accepted': False}
    print('PENDING_ORDER_DIR=' + str(out), flush=True)
    try:
        report['deployment'] = candidate.deploy(case, build, out)
        with candidate.bind_candidate(osu, case, build):
            report['normal_regression'] = candidate.normal_regression(case, out)
            print('既有正常矩阵=28/28 PASS', flush=True)
            candidate.boundary_matrix(case, report)
            print('既有边界矩阵=18/18 PASS', flush=True)
            report['timing'] = timing(out / 'comparison', case, build)
        print('8B OSU五轮中位数us=' + json.dumps(
            {key: report['timing'][key]['median_us'] for key in ('old_rm', 'new_rm', 'rc')},
            ensure_ascii=False), flush=True)
        report['matrix'] = matrix(out / 'window_and_rndv', case, build)
        report['status'] = 'NORMAL_ORDERING_FIX_FUNCTIONAL_PASS_PERFORMANCE_REVIEW_REQUIRED'
        print('pending修复正常功能=PASS；窗口10/10及AM rendezvous10/10通过', flush=True)
        print('性能须审阅全部五轮样本；原生RMA/C/完整B仍未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('PENDING_ORDER_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        osu.base.write_json(out / 'results.json', report)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
