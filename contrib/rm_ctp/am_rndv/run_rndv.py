#!/usr/bin/env python3
"""Original OSU normal AM rendezvous matrix on the already tested RM product."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

from split_logs import collect as collect_split_logs, trial_directory

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'window_matrix'))
import run_window as window
import matrix as window_matrix
from boundary_config import parse_capability
from protocol import (BASE, ITERATIONS, WARMUP, cases, check_am_only_capability,
                      check_config, inspect_trace, settings, trial_profile)

osu = window.osu
candidate = window.candidate
CASE = window.CASE


def plan():
    return {'tool_source_base': BASE, 'product_commit': window_matrix.PRODUCT_COMMIT,
            'module_sha256': window_matrix.PRODUCT_MODULE, 'case_dir': str(CASE),
            'cases': cases(), 'task_count': 10, 'max_ppn': 4, 'max_message_bytes': 1048576,
            'iterations': ITERATIONS, 'warmup': WARMUP,
            'product_unchanged': True, 'performance_accepted': False,
            'native_RMA_accepted': False, 'full_B4_acceptance': False,
            'size_alternation': 'NOT_COVERED', 'C_retry': 'DEFERRED'}


def inspect_defaults(out, environment):
    result = candidate.default_capabilities(CASE / 'install', environment, out)
    result['am_only_capabilities'] = {
        host: check_am_only_capability((out / (host + '_ucx_device.txt')).read_text(),
                                       parse_capability)
        for host in ('node89', 'node97')}
    return result


def finish_protocol(out, row, case, evidence):
    run = out / row['name']
    try:
        if not row['passed']:
            raise ValueError(row.get('error', 'normal validation gate failed'))
        row['protocol_configuration'] = {
            host: check_config((run / (host + '_ucx_config.txt')).read_text(),
                               case, candidate.bytes_value)
            for host in ('node89', 'node97')}
        identities = {rank: {'host': evidence['inventory'][rank // case['ppn']]['hostname'],
                             'pid': row['rank_evidence']['pids'][rank]}
                      for rank in range(2 * case['ppn'])}
        text = (run / 'launcher.log').read_text(errors='replace')
        if row.get('configuration', {}).get('environment', {}).get('UCX_LOG_FILE'):
            text, row['protocol_log_files'] = collect_split_logs(out, row, evidence, osu.base)
        row['protocol_path'] = inspect_trace(text, case, identities)
    except Exception as exc:
        row['passed'] = False
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    osu.base.write_json(run / 'trial.json', row)
    return row


def main():
    parser = argparse.ArgumentParser(description='原版OSU AM rendezvous正常大消息；默认仅计划')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 600:
        parser.error('单任务超时须60..600秒')
    if not (args.run or args.preflight_only):
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise ValueError('本工具只由root在node89显式运行')
    build = candidate.validate_build(CASE)
    if (build['source_commit'] != window_matrix.PRODUCT_COMMIT
            or build['module_sha256'] != window_matrix.PRODUCT_MODULE):
        raise ValueError('fixed tested product identity differs')
    out = Path(tempfile.mkdtemp(prefix='b_am_rndv_', dir=CASE))
    print('AM_RNDV_DIR=' + str(out), flush=True)
    report = {'plan': plan(), 'cases': [], 'status': 'PREFLIGHT',
              'performance_accepted': False, 'full_B_acceptance': False}
    manifest = PROJECT / 'am_rndv_manifest.json'
    if manifest.is_file():
        report['toolkit_manifest_sha256'] = osu.digest(manifest)

    def save():
        osu.base.write_json(out / 'results.json', report)

    save()
    try:
        with candidate.bind_candidate(osu, CASE, build), window.benchmark_registry():
            osu.validate_package()
            commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                            (mode + '_run.json'), CASE / 'install')
                        for mode in ('rm', 'rc')}
            evidence = osu.preflight(out, commands)
            window.check_help(out)
            env = osu.environment(commands['rm'][1], 'iface', CASE / 'install', osu.budget(4))
            env.update(settings(cases()[3]))
            report['defaults'] = inspect_defaults(out, env)
            report['preflight'] = evidence
            if min(item['available_cores'] for item in evidence['inventory']) < 4:
                raise ValueError('one node has fewer than four allowed physical cores')
            print('预检=PASS 产品/OSU/HMPI/4K片长；RM原生RMA与AM_ZCOPY能力关闭', flush=True)
            save()
            if not args.run:
                report['status'] = 'PREFLIGHT_PASS_NO_MPI'
                return
            library = window.prepare_helpers(out, report)
            report['status'] = 'RUNNING'
            save()
            task_args = argparse.Namespace(iterations=ITERATIONS, warmup=WARMUP, timeout=args.timeout)
            for index, case in enumerate(cases()):
                with trial_profile(osu, case, window_matrix.parse_output,
                                   log_directory=trial_directory(out, case, index)):
                    if case['mode'] == 'rm':
                        with window.traced_environment(library):
                            row = osu.one_trial(out, commands, evidence, case['benchmark'],
                                                'rm', case['ppn'], 'validation', index,
                                                task_args, message_bytes=case['sizes'][0])
                    else:
                        row = osu.one_trial(out, commands, evidence, case['benchmark'],
                                            'rc', case['ppn'], 'validation', index,
                                            task_args, message_bytes=case['sizes'][0])
                window.finish_trial(out, row, case, library)
                finish_protocol(out, row, case, evidence)
                report['cases'].append(row)
                save()
                if not row['passed']:
                    raise ValueError(row['name'] + ': ' + row.get('error', 'trial failed'))
                maximum = max((item['max_send_bytes'] for item in row.get('send_observations', [])), default=0)
                print('OSU=%s MODE=%s PPN=%d SIZE=%d PATH=%s DATA=PASS EXIT=0 '
                      'MAPS=PASS PATH_PROOF=%s SEND_MAX=%s' %
                      (case['benchmark'], case['mode'].upper(), case['ppn'], case['sizes'][0],
                       case['path'].upper(), '非本项' if case['path'] == 'gate' else 'PASS',
                       maximum or '未观察'), flush=True)
            report['status'] = 'SELECTED_AM_RNDV_NORMAL_MATRIX_PASS'
            save()
            print('AM大消息正常矩阵=10/10 PASS；已测范围至1MiB，原生READ/WRITE未验收', flush=True)
            print('协议日志和SEND观察开启，不作性能结论；大小反复交替/C仍未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        print('B_AM_RNDV_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        save()
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
