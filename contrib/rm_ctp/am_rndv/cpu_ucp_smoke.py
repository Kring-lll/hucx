#!/usr/bin/env python3
"""Real UCP/TCP path smoke; explicitly not OSU, URMA or ARM acceptance."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from protocol import check_config, inspect_trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--sdk-lib', type=Path, required=True)
    parser.add_argument('--no-devices', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('UCX_', 'LD_'))}
    env.update(LD_LIBRARY_PATH=str(args.prefix / 'lib') + ':' + str(args.sdk_lib),
               LD_PRELOAD=str(args.no_devices), UCX_MODULE_DIR=str(args.prefix / 'lib/ucx'),
               UCX_TLS='tcp', UCX_NET_DEVICES='lo', UCX_PROTO_ENABLE='n',
               UCX_RNDV_SCHEME='am', UCX_RNDV_THRESH='16384',
               UCX_RNDV_SEND_NBR_THRESH='16384', UCX_ZCOPY_THRESH='inf',
               UCX_LOG_LEVEL='req', UCX_TCP_PUT_ENABLE='n')
    result = subprocess.run([str(args.prefix / 'bin/ucx_info'), '-c'], env=env,
                            text=True, capture_output=True, timeout=20)
    (args.out / 'config.txt').write_text(result.stdout + result.stderr)
    from sys import path
    path.insert(0, str(Path(__file__).resolve().parents[1] / 'boundary'))
    from boundary_config import bytes_value
    case = {'benchmark': 'osu_bibw', 'path': 'am_rndv', 'sizes': (65536,), 'threshold': 16384}
    if result.returncode:
        raise ValueError('CPU configuration query failed')
    check_config(result.stdout, case, bytes_value)
    receipt = {'scope': 'REAL_UCP_TCP_CPU_ONLY_NOT_OSU_URMA_OR_ARM', 'trials': [],
               'tcp_put_disabled_to_match_absent_rm_native_rma': True}
    for size in (16383, 16384, 65536, 1048576):
        case.update(sizes=(size,), path='eager' if size < 16384 else 'am_rndv')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        command = [str(args.prefix / 'bin/ucx_perftest'), '-t', 'tag_lat', '-s', str(size),
                   '-n', '4', '-w', '1', '-p', str(port)]
        processes = []
        server_log = args.out / ('server_%d.log' % size)
        client_log = args.out / ('client_%d.log' % size)
        try:
            with server_log.open('w') as left, client_log.open('w') as right:
                server = subprocess.Popen(command, env=env, stdout=left, stderr=subprocess.STDOUT)
                processes.append(server)
                time.sleep(.3)
                client = subprocess.Popen([command[0], '127.0.0.1'] + command[1:],
                                          env=env, stdout=right, stderr=subprocess.STDOUT)
                processes.append(client)
                client_exit = client.wait(timeout=30)
                server_exit = server.wait(timeout=30)
            if server_exit or client_exit:
                raise ValueError('CPU UCP/TCP process failed')
            identities = {0: {'host': socket.gethostname(), 'pid': server.pid},
                          1: {'host': socket.gethostname(), 'pid': client.pid}}
            proof = inspect_trace(server_log.read_text() + client_log.read_text(), case, identities)
            receipt['trials'].append({'size': size, 'server_exit': server_exit,
                                      'client_exit': client_exit, 'path_evidence': proof})
            print('CPU_UCP_TCP SIZE=%d PATH=%s PROOF=PASS NOT_NODE_ACCEPTANCE=YES' %
                  (size, case['path']), flush=True)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
    receipt['status'] = 'PASS_CPU_TCP_PATH_ONLY'
    (args.out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
