"""Bounded paired public-UCT timeout checks; provider CQEs are never fabricated."""
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess


def compile_probe(integration, case, out, commands):
    project = Path(__file__).resolve().parent.parent
    exe = out / 'failure_probe'
    library = out / 'libsend_failure.so'
    env = integration.runtime_environment(commands['rm'][1])
    for name, command in (
        ('failure_probe', ['gcc', '-std=gnu99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                           '-Wno-unused-function', '-I' + str(case / 'install/include'),
                           '-I' + str(case / 'source/src/uct/urma/rm_urma'),
                           str(project / 'send_validation/failure_probe.c'),
                           str(case / 'source/src/uct/urma/rm_urma/rm_urma_addr.c'),
                           '-L' + str(case / 'install/lib'), '-Wl,-rpath,' + str(case / 'install/lib'),
                           '-luct', '-lucs', '-lucm', '-lpthread', '-ldl', '-lm', '-o', str(exe)]),
        ('failure_injector', ['gcc', '-std=gnu99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                              '-shared', '-fPIC', str(project / 'send_validation/drop_all.c'),
                              '-ldl', '-o', str(library)])):
        integration.osu.base.run_logged(command, env, out / ('compile_' + name + '.log'), 90)
    integration.copy_checked([exe, library], out)
    return exe, library


def inspect(logs, fault, limit):
    if fault not in ('none','drop_data','drop_ack') or limit not in ('retry_limit','deadline'):
        raise ValueError('unknown failure scenario')
    parsed = {}
    for role, text in logs.items():
        if re.search(r'DIRECT_IFACE_FAILED:|DIRECT_IFACE_STATUS:|FAILURE_.*FAILED|'
                     r'UCX\s+(ERROR|FATAL)|Assertion.*failed|Caught signal', text):
            raise ValueError(role + ': unexpected probe/runtime error')
        matches = re.findall(r'SEND_FAILURE_PROBE_PASS ROLE=(\w+) PROFILE=(\w+) '
                             r'CALLBACKS=(\d+) RECEIVED=(\d+) ELAPSED=([\d.]+) CLOSED=YES', text)
        faults = re.findall(r'FAILURE_INJECTION CALLS=(\d+) DATA=(\d+) ACK=(\d+)', text)
        if len(matches) != 1 or len(faults) != 1:
            raise ValueError(role + ': missing/duplicate close/fault receipt')
        r, p, callbacks, received, elapsed = matches[0]
        if r != role or p != fault or not 0 < float(elapsed) < 20:
            raise ValueError('probe identity or duration mismatch')
        parsed[role] = dict(callbacks=int(callbacks), received=int(received), elapsed=float(elapsed),
                            calls=int(faults[0][0]), data=int(faults[0][1]), ack=int(faults[0][2]))
    expected = fault != 'none'
    if expected and limit == 'deadline' and parsed['sender']['elapsed'] < 0.7:
        raise ValueError('deadline did not elapse; unrelated early failure cannot pass')
    if (parsed['sender']['callbacks'] != (2 if expected else 0) or parsed['receiver']['callbacks'] != 0
            or parsed['receiver']['received'] != (0 if fault == 'drop_data' else 1)):
        raise ValueError('terminal callback or deduplicated delivery evidence differs')
    retries = len(re.findall(r'RM reliable retry peer=', logs['sender']))
    expected_retries = 3 if limit == 'retry_limit' and expected else 0
    if retries != expected_retries:
        raise ValueError('production resend count differs: ' + str(retries))
    if fault == 'drop_data' and parsed['receiver']['data'] < 1:
        raise ValueError('DATA fault did not trigger')
    if fault == 'drop_ack' and parsed['sender']['ack'] < 1:
        raise ValueError('ACK fault did not trigger')
    if fault == 'none' and any(r['data'] or r['ack'] for r in parsed.values()):
        raise ValueError('control unexpectedly injected a fault')
    return dict(roles=parsed, production_retry_posts=retries,
                expected_error='UCS_ERR_ENDPOINT_TIMEOUT' if expected else 'UCS_OK')


def run(integration, case, root, commands, binary, library, item):
    base = integration.osu.base
    name = 'exit_' + item['limit'] + '_' + item['fault']
    out = root / name
    out.mkdir()
    base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', out.parent, out)
    env = integration.effective_environment(case, 1, commands['rm'][1])
    env.update({'LD_LIBRARY_PATH': str(case / 'install/lib'),
           'UCX_MODULE_DIR': str(case / 'install/lib/ucx'),
           'UCX_RM_URMA_LINK_MODE':'reliable_v3', 'UCX_RM_URMA_MAX_PEERS':'5',
           'UCX_RM_URMA_FC_ENABLE':'y', 'UCX_RM_URMA_FC_WND_SIZE':'8',
           'UCX_RM_URMA_TX_DEPTH':'8', 'UCX_RM_URMA_RX_DEPTH':'64',
           'UCX_RM_URMA_SEG_SIZE':'4096', 'UCX_RM_URMA_NATIVE_RMA':'n',
           'UCX_RM_URMA_NATIVE_READ':'n', 'UCX_RM_URMA_NATIVE_WRITE':'n',
           'UCX_RM_URMA_SEND_RTO':'100ms' if item['limit'] == 'retry_limit' else '800ms',
           'UCX_RM_URMA_SEND_TIMEOUT':'5s' if item['limit'] == 'retry_limit' else '800ms',
           'UCX_RM_URMA_SEND_RETRIES':'3' if item['limit'] == 'retry_limit' else '256',
           'UCX_LOG_LEVEL':'debug', 'LD_PRELOAD':str(library),
           'HUCX_SEND_TEST_MODULE':str(case / 'install/lib/ucx/libuct_ub.so.0.0.0'),
           'HUCX_SEND_FAILURE_PROFILE':item['fault']})
    with socket.socket() as sock:
        sock.bind(('', 0))
        port = sock.getsockname()[1]
    prefix = ['timeout', '--signal=TERM', '--kill-after=3', '45', 'env', '-i', 'PATH=/usr/bin:/bin']
    prefix += [k + '=' + v for k, v in env.items()]
    client = prefix + [str(binary), 'connect', '9.88.43.97', str(port), str(out / 'sender.maps')]
    server = prefix + [str(binary), 'listen', '0.0.0.0', str(port), str(out / 'receiver.maps')]
    row = dict(item, name=name, run=str(out), passed=False, environment=env,
               sender_command=client, receiver_command=server)
    proc = None
    try:
        integration.capture_configuration(case / 'install', env, out, 'iface')
        with (out / 'ssh.log').open('w') as sshlog, (out / 'sender.log').open('w') as senderlog:
            remote = 'exec ' + shlex.join(server) + ' > ' + shlex.quote(str(out / 'receiver.log')) + ' 2>&1'
            proc = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                                     'root@9.88.43.97', remote], stdout=sshlog, stderr=subprocess.STDOUT)
            row['sender_exit'] = subprocess.run(client, stdout=senderlog, stderr=subprocess.STDOUT,
                                                 timeout=52).returncode
            row['receiver_exit'] = proc.wait(timeout=55)
        # Capture primary logs before checking maps; missing maps must not hide an earlier error.
        peerlog = base.ssh('cat "$1"\n', out / 'receiver.log', timeout=20)
        (out / 'receiver.log').write_text(peerlog)
        if row['sender_exit'] or row['receiver_exit']:
            raise RuntimeError('probe exited nonzero; launcher timeout is never an expected failure PASS')
        integration.fetch_peer_json(out, ['receiver.maps'])
        from_map = integration.PROJECT / 'send_reliable/run_candidate.py'
        import importlib.util
        spec = importlib.util.spec_from_file_location('send_failure_maps', from_map)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        for role in ('sender', 'receiver'):
            helper.maps_helper.check_maps((out / (role + '.maps')).read_text(), case / 'install')
        logs = {role:(out / (role + '.log')).read_text(errors='replace') for role in ('sender','receiver')}
        row['evidence'] = inspect(logs, item['fault'], item['limit'])
        row['passed'] = True
    except Exception as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        if proc is not None and proc.poll() is None:
            # Remote exec is bounded by its own GNU timeout. Do not kill any other SSH/MPI job.
            try:
                proc.wait(timeout=55)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
                row['passed'] = False
                row['cleanup_error'] = 'own SSH did not exit after bounded remote command'
        base.write_json(out / 'trial.json', row)
    return row
