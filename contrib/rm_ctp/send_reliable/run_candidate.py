#!/usr/bin/env python3
"""Opt-in reliable SEND: historical regression, direct UCT and software injection.

No READ/WRITE, hardware fault recovery or performance acceptance is performed.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
EXPERIMENT = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-c-send-reliability-dev')
sys.path.insert(0, str(ROOT.parent / 'segment_default'))
spec = importlib.util.spec_from_file_location('send_candidate_base', ROOT.parent / 'segment_default/run_candidate.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
spec = importlib.util.spec_from_file_location('send_pair_base', ROOT.parent / 'native_write/run_candidate.py')
pair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pair)
spec = importlib.util.spec_from_file_location('send_maps', ROOT.parent / 'r1/a7_iface_four_ep_20261026.py')
maps_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maps_helper)


def environment(case):
    return {'LD_LIBRARY_PATH': str(case / 'install/lib') + ':' + str(case / 'install/lib/ucx'),
            'UCX_RM_URMA_LINK_MODE': 'reliable_v3', 'UCX_RM_URMA_MAX_PEERS': '5',
            'UCX_RM_URMA_FC_ENABLE': 'y', 'UCX_RM_URMA_FC_WND_SIZE': '8',
            'UCX_RM_URMA_TX_DEPTH': '8', 'UCX_RM_URMA_RX_DEPTH': '64',
            'UCX_RM_URMA_FC_SOFT_THRESH': '0.5', 'UCX_RM_URMA_FC_HARD_THRESH': '0.25',
            'UCX_RM_URMA_NATIVE_WRITE': 'n', 'UCX_RM_URMA_NATIVE_READ': 'n',
            'UCX_RM_URMA_NATIVE_RMA': 'n', 'UCX_RM_URMA_SEND_RTO': '100ms',
            'UCX_RM_URMA_SEND_TIMEOUT': '30s', 'UCX_RM_URMA_SEND_RETRIES': '256',
            'UCX_LOG_LEVEL': 'debug'}


def run_pair(case, out, exe, injector, inject):
    out.mkdir()
    base.osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', out.parent, out)
    row = {'passed': False, 'scope': 'SOFTWARE_RECEIVE_INJECTION' if inject else 'NORMAL_DIRECT_UCT',
           'environment': environment(case)}
    env = row['environment']
    if inject:
        env['LD_PRELOAD'] = str(injector)
        env['HUCX_SEND_TEST_MODULE'] = str(case / 'install/lib/ucx/libuct_ub.so.0.0.0')
    with socket.socket() as sock:
        sock.bind(('', 0))
        port = sock.getsockname()[1]
    clean = ['env', '-i', 'PATH=/usr/bin:/bin'] + [k + '=' + v for k, v in env.items()]
    server = ['timeout', '--signal=TERM', '--kill-after=5', '150'] + clean
    server += [str(exe), 'listen', '0.0.0.0', str(port), str(out / 'receiver.maps')]
    client = ['timeout', '--signal=TERM', '--kill-after=5', '140'] + clean
    client += [str(exe), 'connect', '9.88.43.97', str(port), str(out / 'sender.maps')]
    row.update(sender_command=client, receiver_command=server)
    proc = None
    try:
        remote = shlex.join(server) + ' > ' + shlex.quote(str(out / 'receiver.log')) + ' 2>&1'
        with (out / 'ssh.log').open('w') as ssh_log:
            proc = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                                     'root@9.88.43.97', remote], stdout=ssh_log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            with (out / 'sender.log').open('w') as log:
                row['sender_exit'] = subprocess.run(client, stdout=log, stderr=subprocess.STDOUT,
                                                     timeout=148, check=False).returncode
            row['receiver_exit'] = proc.wait(timeout=160)
        def fetch(name):
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                            'root@9.88.43.97:' + str(out / name), str(out / name)],
                           check=True, timeout=45)
        # Retrieve primary diagnostics before optional maps, including failures.
        fetch('receiver.log')
        if row['sender_exit'] or row['receiver_exit']:
            pair.print_probe_errors(out)
            raise RuntimeError('paired SEND failed; inspect sender.log and receiver.log')
        text = {}
        for role in ('sender', 'receiver'):
            text[role] = (out / (role + '.log')).read_text(errors='replace')
            if text[role].count('RELIABLE_SEND_PASS ROLE=') != 1:
                raise ValueError(role + ' missing final lifecycle/data evidence')
            if 'DIRECT_IFACE_FAILED:' in text[role] or ' UCX  ERROR ' in text[role] or ' UCX  FATAL ' in text[role]:
                raise ValueError(role + ' reports failure')
            for cycle in (0, 1):
                name = role + '.maps.' + str(cycle)
                if role == 'receiver':
                    fetch(name)
                mapped = (out / name).read_text()
                maps_helper.check_maps(mapped, case / 'install')
                for line in mapped.splitlines():
                    if any('/' + library in line for library in ('libuct', 'libucs', 'libucm')):
                        if str(case / 'install/lib') + '/' not in line:
                            raise ValueError(role + ' mixed candidate/core libraries')
        if inject:
            if 'RM reliable retry peer=' not in text['sender']:
                raise ValueError('injection produced no observed production retransmission')
            # Fail if PLT interposition did not actually execute on this build.
            expected = {'sender': ('ACK_DROPS=2',),
                        'receiver': ('DATA_DROPS=1', 'DUPLICATES=1')}
            for role, values in expected.items():
                if any(value not in text[role] for value in values):
                    raise ValueError(role + ' software injection not exercised')
        row['passed'] = True
        row['data'] = '512x64B short/bcopy, per-stream order and no duplicates'
        row['lifecycle'] = '160 creates/destroys, 32 early destroys, pending purge/requeue'
        print(('软件丢DATA/丢ACK/重复注入' if inject else '新模式正常SEND') +
              '=PASS 数据/顺序/去重/跨窗口/双端候选maps/生命周期/退出', flush=True)
    except BaseException as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        # The two timeout processes own only this round. No node-wide pkill.
        pair.stop(proc)
        (out / 'trial.json').write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n')
    return row


def main():
    parser = argparse.ArgumentParser(description='SEND可靠性首轮候选；默认仅计划')
    parser.add_argument('case', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('PLAN=既有旧模式28项回归＋reliable_v3正常/软件接收注入两轮；每端1进程，原生RMA关闭')
        print('C/D完整验收、MPI大消息、OSU性能、高规模及真实设备故障均不在本轮结论内')
        return
    case = args.case.resolve()
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89')
    if ROOT != case / 'source/contrib/rm_ctp/send_reliable':
        raise ValueError('execute the isolated candidate source')
    build = base.validate_build(case, root=EXPERIMENT)
    out = Path(tempfile.mkdtemp(prefix='send_reliable_', dir=case))
    report = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
              'status': 'RUNNING', 'default_link_mode_changed': False,
              'native_rma': False, 'performance_accepted': False, 'trials': []}
    print('SEND_RELIABLE_DIR=' + str(out), flush=True)
    try:
        base.B_ROOT = EXPERIMENT
        report['deployment'] = base.deploy(case, build, out)
        with base.bind_candidate(base.osu, case, build):
            report['prior_modes_normal_28'] = base.normal_regression(case, out)
        print('既有legacy_ep/iface_v2/RC正常矩阵=28/28 PASS（旧模式回归）', flush=True)
        exe = out / 'send_probe'
        injector = out / 'libsend_drop.so'
        commands = [
            ['gcc', '-std=gnu99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
             '-I' + str(case / 'install/include'),
             '-I' + str(case / 'source/src/uct/urma/rm_urma'),
             str(ROOT / 'node_probe.c'), str(case / 'source/src/uct/urma/rm_urma/rm_urma_addr.c'),
             '-L' + str(case / 'install/lib'), '-Wl,-rpath,' + str(case / 'install/lib'),
             '-luct', '-lucs', '-o', str(exe)],
            ['gcc', '-std=gnu99', '-O2', '-Wall', '-Wextra', '-Werror', '-fPIC', '-shared',
             str(ROOT / 'drop_receive.c'), '-ldl', '-o', str(injector)]
        ]
        with (out / 'compile.log').open('w') as log:
            for command in commands:
                subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=90)
        base.osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', case, out)
        base.osu.base.copy_remote([exe, injector], str(out) + '/')
        report['binaries'] = {}
        for binary in (exe, injector):
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            if base.osu.base.ssh('sha256sum "$1"\n', binary).split()[0] != digest:
                raise ValueError('peer test binary differs: ' + binary.name)
            report['binaries'][binary.name] = digest
        for inject in (False, True):
            report['trials'].append(run_pair(case, out / ('injection' if inject else 'normal'),
                                              exe, injector, inject))
        report['status'] = 'FOCUSED_SEND_NORMAL_AND_SOFTWARE_INJECTION_PASS'
        print('本轮定向SEND=PASS；默认旧模式保留；软件注入不是硬件丢包/故障恢复验收，无性能结论', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('SEND_RELIABLE_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
