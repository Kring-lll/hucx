#!/usr/bin/env python3
"""Bounded native UCT WRITE bring-up; not OSU performance or full B4 acceptance."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'segment_default'))
spec = importlib.util.spec_from_file_location('write_candidate_base', ROOT.parent / 'segment_default/run_candidate.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
osu = base.osu


def stop(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def trial_environment(case):
    # Match the already validated direct-UCT iface_v2 resource contract.
    return {'LD_LIBRARY_PATH': str(case / 'install/lib') + ':' + str(case / 'install/lib/ucx'),
            'UCX_RM_URMA_LINK_MODE': 'iface_v2', 'UCX_RM_URMA_MAX_PEERS': '5',
            'UCX_RM_URMA_FC_ENABLE': 'y', 'UCX_RM_URMA_FC_WND_SIZE': '8',
            'UCX_RM_URMA_TX_DEPTH': '8', 'UCX_RM_URMA_RX_DEPTH': '64',
            'UCX_RM_URMA_FC_SOFT_THRESH': '0.5', 'UCX_RM_URMA_FC_HARD_THRESH': '0.25',
            'UCX_RM_URMA_NATIVE_WRITE': 'y', 'UCX_LOG_LEVEL': 'debug'}


def collect_receiver_outputs(out, sender_exit, receiver_exit, fetch):
    # Error logs exist before final maps; preserve the primary process failure.
    fetch('receiver.log')
    if sender_exit != 0 or receiver_exit != 0:
        raise RuntimeError('native probe failed: sender_exit=%s receiver_exit=%s; logs=%s' %
                           (sender_exit, receiver_exit, out))
    fetch('receiver.maps')


def print_probe_errors(out):
    import re
    pattern = re.compile(r'DIRECT_IFACE_(?:FAILED|STATUS)|UCX\s+(?:ERROR|FATAL)|Assertion')
    for role in ('sender', 'receiver'):
        path = out / (role + '.log')
        if path.exists():
            lines = path.read_text(errors='replace').splitlines()
            hits = [line for line in lines if pattern.search(line)]
            for line in (hits[:4] or lines[-3:]):
                print(role.upper() + ': ' + line[:450], flush=True)


def main():
    parser = argparse.ArgumentParser(description='原生WRITE定向验证；默认仅计划')
    parser.add_argument('case', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    case = args.case.resolve()
    if not args.run:
        print('PLAN=6 native WRITE rounds; 8/64/4096 bytes twice; two processes; no MPI; default capabilities unchanged; no performance claim')
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89')
    if ROOT != case / 'source/contrib/rm_ctp/native_write':
        raise ValueError('run the entry from this isolated CASE source')
    build = base.validate_build(case)
    out = Path(tempfile.mkdtemp(prefix='native_write_', dir=case))
    report = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
              'status': 'RUNNING', 'scope': 'DIRECT_UCT_WRITE_ONLY_NOT_OSU_PERFORMANCE',
              'GET_enabled': False, 'default_native_write': False,
              'tool_overlay_sha256': globals().get('OVERLAY_SHA256')}
    print('NATIVE_WRITE_DIR=' + str(out), flush=True)
    server = None
    try:
        report['deployment'] = base.deploy(case, build, out)
        exe = out / 'write_probe'
        cmd = ['gcc', '-std=gnu99', '-O2', '-g', '-I' + str(case / 'install/include'),
               '-I' + str(case / 'source/src/uct/urma/rm_urma'), str(ROOT / 'write_probe.c'),
               str(case / 'source/src/uct/urma/rm_urma/rm_urma_addr.c'),
               '-L' + str(case / 'install/lib'), '-Wl,-rpath,' + str(case / 'install/lib'),
               '-luct', '-lucs', '-o', str(exe)]
        with (out / 'compile.log').open('w') as log:
            subprocess.run(cmd, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=90)
        report['probe_sha256'] = osu.digest(exe)
        osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', case, out)
        osu.base.copy_remote([exe], str(out) + '/')
        if osu.base.ssh('sha256sum "$1"\n', exe).split()[0] != report['probe_sha256']:
            raise ValueError('peer probe differs')
        with socket.socket() as sock:
            sock.bind(('', 0))
            port = sock.getsockname()[1]
        environment = trial_environment(case)
        env_args = ['env', '-u', 'LD_PRELOAD', '-u', 'LD_DEBUG']
        env_args += [k + '=' + v for k, v in environment.items()]
        server_cmd = ['timeout', '--signal=TERM', '--kill-after=5', '90'] + env_args
        server_cmd += [str(exe), 'listen', '0.0.0.0', str(port), str(out / 'receiver.maps')]
        shell = shlex.join(server_cmd) + ' > ' + shlex.quote(str(out / 'receiver.log')) + ' 2>&1'
        report['environment'] = environment
        report['receiver_command'] = server_cmd
        with (out / 'ssh.log').open('w') as log:
            server = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                                       'root@9.88.43.97', shell], stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=True)
            client_cmd = ['timeout', '--signal=TERM', '--kill-after=5', '80'] + env_args
            client_cmd += [str(exe), 'connect', '9.88.43.97', str(port), str(out / 'sender.maps')]
            report['sender_command'] = client_cmd
            with (out / 'sender.log').open('w') as client_log:
                result = subprocess.run(client_cmd, stdout=client_log, stderr=subprocess.STDOUT,
                                        timeout=88, check=False)
            report['sender_exit'] = result.returncode
            report['receiver_exit'] = server.wait(timeout=105)
        def fetch(name):
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                            'root@9.88.43.97:' + str(out / name), str(out / name)],
                           check=True, timeout=45)
        collect_receiver_outputs(out, report['sender_exit'], report['receiver_exit'], fetch)
        for role in ('sender', 'receiver'):
            text = (out / (role + '.log')).read_text(errors='replace')
            if text.count('NATIVE_WRITE_ROUND=') != 6 or 'NATIVE_WRITE_PASS ' not in text:
                raise ValueError(role + ' incomplete native data/registration result')
            if ' UCX  ERROR ' in text or ' UCX  FATAL ' in text or 'DIRECT_IFACE_FAILED:' in text:
                raise ValueError(role + ' error log')
            # The established A7 maps checker verifies candidate core and transport libraries.
            path = ROOT.parent / 'r1/a7_iface_four_ep_20261026.py'
            helper_spec = importlib.util.spec_from_file_location('write_maps_checker', path)
            helper = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(helper)
            helper.check_maps((out / (role + '.maps')).read_text(), case / 'install')
        report['status'] = 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS'
        print('原生WRITE=PASS 6轮 8/64/4096B 接收端内存校验/双端候选maps/注册释放=PASS', flush=True)
        print('仅定向UCT功能；默认开关关闭，READ/正式OSU/完整B4尚未验收', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print_probe_errors(out)
        print('NATIVE_WRITE_STOPPED=' + report['error'][:600], flush=True)
        raise SystemExit(1)
    finally:
        stop(server)
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
