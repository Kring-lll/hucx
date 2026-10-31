#!/usr/bin/env python3
"""Bounded basic UCT and public UCP RMA; no unverified MPI OSC/atomic claim."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


read = load('basic_previous_read', ROOT.parent / 'native_read/run_candidate.py')
base = read.base
osu = base.osu


def environment(case):
    result = read.trial_environment(case)
    result.update({'UCX_RM_URMA_NATIVE_RMA': 'y', 'UCX_RM_URMA_NATIVE_READ': 'n',
                   'UCX_RM_URMA_NATIVE_WRITE': 'n', 'UCX_TLS': 'rm_urma',
                   'UCX_NET_DEVICES': 'udmac0d1e2:1', 'UCX_PROTO_ENABLE': 'n',
                   'UCX_LOG_LEVEL': 'trace', 'UCX_WARN_UNUSED_ENV_VARS': 'n'})
    return result


def inspect_pair(out, case, kind, expected_kinds):
    checker = load('basic_maps_check', ROOT.parent / 'r1/a7_iface_four_ep_20261026.py')
    rows = []
    for role in ('sender', 'receiver'):
        text = (out / (role + '.log')).read_text(errors='replace')
        marker = 'NATIVE_BASIC_PASS ROUNDS=8 ' if kind == 'basic' else 'UCP_RMA_PASS ROUNDS=16 '
        prefix = 'NATIVE_BASIC_ROUND=' if kind == 'basic' else 'UCP_RMA_ROUND='
        count = 8 if kind == 'basic' else 16
        if kind == 'basic':
            sizes = (8, 64, 8, 4096, 8, 64, 4096, 8)
            offsets = (0, 8, 64, 128, 0, 8, 64, 128)
            name = 'reader' if role == 'sender' else 'provider'
            actual = re.findall(r'^NATIVE_BASIC_ROUND=(\d+) BYTES=(\d+) OFFSET=(\d+) ROLE=' + name + r' DATA=PASS$', text, re.M)
            expected = [(str(i), str(sizes[i]), str(offsets[i])) for i in range(count)]
        else:
            sizes = (8, 65536, 64, 16385, 4096, 8193, 8, 65536)
            name = 'initiator' if role == 'sender' else 'provider'
            actual = re.findall(r'^UCP_RMA_ROUND=(\d+) OP=(PUT|GET) BYTES=(\d+) ROLE=' + name + r' DATA=PASS$', text, re.M)
            expected = [(str(i), 'PUT' if i < 8 else 'GET', str(sizes[i % 8])) for i in range(count)]
        if (actual != expected or text.count(prefix) != count or marker not in text):
            raise ValueError('missing/duplicate/order/data/cleanup receipt: ' + role)
        checker.check_maps((out / (role + '.maps')).read_text(), case / 'install')
        log = out / (role + '.ucx.log')
        if log.is_symlink() or not log.is_file():
            raise ValueError('missing/nonregular UCX log: ' + role)
        operations = {}
        # Scan whole file incrementally: no 128MiB rejection or truncated proof.
        with log.open(errors='replace') as stream:
            for line in stream:
                if re.search(r'UCX\s+(ERROR|FATAL)|Assertion.*failed', line):
                    raise ValueError('UCX error: ' + line[:350])
                match = re.search(r'RM native RMA post kind=(\w+) length=(\d+)', line)
                if match:
                    name, length = match[1], int(match[2])
                    if length < 1 or length > 4096:
                        raise ValueError('native work request out of bound')
                    operations[name] = operations.get(name, 0) + 1
        rows.append({'role': role, 'rounds': count, 'operations': operations,
                     'maps_sha256': osu.digest(out / (role + '.maps')),
                     'ucx_log_sha256': osu.digest(log), 'ucx_log_bytes': log.stat().st_size})
    if not set(expected_kinds) <= rows[0]['operations'].keys():
        raise ValueError('requested native path not observed: ' + str(rows[0]['operations']))
    return rows


def pair(case, parent, exe, name, kind, settings, expected_kinds):
    out = parent / name
    out.mkdir()
    osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', parent, out)
    report = {'name': name, 'passed': False, 'environment': settings,
              'expected_native_kinds': expected_kinds}
    server = None
    try:
        with socket.socket() as sock:
            sock.bind(('', 0))
            port = sock.getsockname()[1]
        def command(role):
            env = dict(settings, UCX_LOG_FILE=str(out / (role + '.ucx.log')))
            return (['timeout', '--signal=TERM', '--kill-after=5', '150',
                     'env', '-i', 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'] +
                    [k + '=' + v for k, v in sorted(env.items())] +
                    [str(exe), 'listen' if role == 'receiver' else 'connect',
                     '0.0.0.0' if role == 'receiver' else '9.88.43.97',
                     str(port), str(out / (role + '.maps'))])
        remote = command('receiver')
        local = command('sender')
        report['sender_command'] = local
        report['receiver_command'] = remote
        shell = shlex.join(remote) + ' > ' + shlex.quote(str(out / 'receiver.log')) + ' 2>&1'
        with (out / 'ssh.log').open('w') as log:
            server = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                                       'root@9.88.43.97', shell], stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=True)
            with (out / 'sender.log').open('w') as output:
                completed = subprocess.run(local, stdout=output, stderr=subprocess.STDOUT, timeout=158)
            report['sender_exit'] = completed.returncode
            report['receiver_exit'] = server.wait(timeout=160)
        def fetch(filename):
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                            'root@9.88.43.97:' + str(out / filename), str(out / filename)],
                           check=True, timeout=60)
        # Preserve original runtime failure, rather than hiding it behind maps.
        fetch('receiver.log')
        if report['sender_exit'] != 0 or report['receiver_exit'] != 0:
            read.print_probe_errors(out)
            raise RuntimeError('paired probe exited: sender=%s receiver=%s; logs=%s' %
                               (report['sender_exit'], report['receiver_exit'], out))
        for filename in ('receiver.maps', 'receiver.ucx.log'):
            fetch(filename)
        report['evidence'] = inspect_pair(out, case, kind, expected_kinds)
        report['passed'] = True
        print(name + '=DATA/GUARDS/MAPS/NATIVE_PATH/EXIT PASS', flush=True)
        return report
    except BaseException as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        # Only this SSH process group is signaled. The remote child has its own
        # bounded timeout; never pkill an application name or a node-wide user.
        read.stop(server)
        (out / 'trial.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


@contextmanager
def enabled_normal(legacy):
    original = legacy.values
    def values(mode, debug=False):
        result = original(mode, debug)
        result.update({'UCX_RM_URMA_NATIVE_RMA': 'y', 'UCX_RM_URMA_NATIVE_READ': 'n',
                       'UCX_RM_URMA_NATIVE_WRITE': 'n'})
        return result
    legacy.values = values
    try:
        yield
    finally:
        legacy.values = original


def main():
    parser = argparse.ArgumentParser(description='基础RMA与普通UCP读写；默认仅计划')
    parser.add_argument('case', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('PLAN=prior READ6+WRITE6; basic UCT8; UCP16x2 with strict size alternation; enabled normal28; max PPN4; MPI OSC/OSU deferred pending atomics; no performance claim')
        return
    case = args.case.resolve()
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89')
    if ROOT != case / 'source/contrib/rm_ctp/native_rma':
        raise ValueError('run from new isolated candidate source')
    build = base.validate_build(case)
    out = Path(tempfile.mkdtemp(prefix='native_rma_accept_', dir=case))
    report = {'source_commit': build['source_commit'], 'module_sha256': build['module_sha256'],
              'status': 'RUNNING', 'default_native_rma': False, 'MPI_RMA_accepted': False,
              'performance_accepted': False, 'full_B4_accepted': False,
              'MPI_OSU': 'DEFERRED_OSC_UCX_REQUESTS_AMO32_AMO64_NOT_ADVERTISED', 'trials': []}
    print('NATIVE_RMA_ACCEPT_DIR=' + str(out), flush=True)
    saved_argv = sys.argv
    saved_ucx = {key: val for key, val in os.environ.items() if key.startswith('UCX_')}
    try:
        report['deployment'] = base.deploy(case, build, out)
        for key in saved_ucx:
            os.environ.pop(key, None)
        os.environ['UCX_RM_URMA_NATIVE_RMA'] = 'n'
        accept = load('basic_prior_accept', ROOT.parent / 'native_read/accept_candidate.py')
        write = load('basic_prior_write', ROOT.parent / 'native_write/run_candidate.py')
        report['prior'] = {}
        for label, module, prefix, status in (
                ('read', read, 'native_read_', 'DIRECT_NATIVE_READ_DATA_MAPS_REGISTRATION_RELEASE_PASS'),
                ('write', write, 'native_write_', 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS')):
            before = set(case.glob(prefix + '*'))
            sys.argv = [str(module.ROOT / 'run_candidate.py'), str(case), '--run']
            module.main()
            result = accept.new_result(case, prefix, before, status, build)
            report['prior'][label] = {'results_json': str(result), 'sha256': osu.digest(result)}
        for kind in ('basic', 'ucp'):
            exe = out / (kind + '_probe')
            cmd = ['gcc', '-std=gnu99', '-O2', '-g', '-I' + str(case / 'install/include'),
                   '-I' + str(case / 'source/src/uct/urma/rm_urma'), str(ROOT / (kind + '_probe.c')),
                   str(case / 'source/src/uct/urma/rm_urma/rm_urma_addr.c'),
                   '-L' + str(case / 'install/lib'), '-Wl,-rpath,' + str(case / 'install/lib')]
            cmd += ['-lucp'] if kind == 'ucp' else []
            cmd += ['-luct', '-lucs', '-o', str(exe)]
            with (out / (kind + '_compile.log')).open('w') as log:
                subprocess.run(cmd, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=90)
            osu.base.ssh('set -eu\nmkdir -p "$1"\n', out)
            osu.base.copy_remote([exe], str(out) + '/')
            if osu.base.ssh('sha256sum "$1"\n', exe).split()[0] != osu.digest(exe):
                raise ValueError('peer executable differs: ' + kind)
            if kind == 'basic':
                report['trials'].append(pair(case, out, exe, 'basic_uct', kind, environment(case),
                                            ['put_short', 'put_bcopy', 'get_bcopy']))
            else:
                for name, threshold, expected in (
                        ('ucp_bcopy', 'inf', ['put_short', 'put_bcopy', 'get_bcopy']),
                        ('ucp_zcopy', '1024', ['put_short', 'put_zcopy', 'get_zcopy'])):
                    env = environment(case)
                    env.update({'UCX_BCOPY_THRESH': '0', 'UCX_ZCOPY_THRESH': threshold})
                    report['trials'].append(pair(case, out, exe, name, kind, env, expected))
        sys.path.insert(0, str(ROOT.parent / 'r1'))
        import accept_iface as legacy
        with enabled_normal(legacy):
            report['normal'] = base.normal_regression(case, out)
        normal = json.loads(Path(report['normal']['results_json']).read_text())
        for row in normal['cases']:
            receipt = json.loads(Path(row['log']).with_suffix('.json').read_text())
            if receipt['environment'].get('UCX_RM_URMA_NATIVE_RMA') != 'y':
                raise ValueError('actual normal command missing NATIVE_RMA=y')
        report['status'] = 'BASIC_UCT_AND_PUBLIC_UCP_RMA_NORMAL_PASS_MPI_OSU_PENDING'
        print('READ6/WRITE6/基础UCT8/UCP16x2/开启后28正常=PASS；UCP严格大小交替已覆盖', flush=True)
        print('MPI单边OSU=待原子操作依赖处理；本轮不作性能或完整B4结论', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('NATIVE_RMA_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        sys.argv = saved_argv
        for key in list(os.environ):
            if key.startswith('UCX_'):
                os.environ.pop(key, None)
        os.environ.update(saved_ucx)
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
