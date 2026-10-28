#!/usr/bin/env python3
"""Per-rank environment, timeout and narrowly scoped cleanup; no machine reset."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import time


def start_ticks(pid, proc_root=Path('/proc')):
    # comm can contain spaces/parentheses; fields after its last ')' are stable.
    return (Path(proc_root) / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]


def cleanup(nonce, root, proc_root=Path('/proc')):
    if len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        raise ValueError('invalid run nonce')
    root = str(Path(root).resolve())
    own_pid = os.getpid()
    killed = []
    for process in Path(proc_root).iterdir():
        if not process.name.isdigit() or int(process.name) == own_pid:
            continue
        pid = int(process.name)
        try:
            before = start_ticks(pid, proc_root)
            environment = (process / 'environ').read_bytes().split(b'\0')
            command = (process / 'cmdline').read_bytes()
            if (('A89_RUN_NONCE=' + nonce).encode() not in environment or
                    root.encode() not in command):
                continue
            if start_ticks(pid, proc_root) != before:
                continue
            os.kill(pid, signal.SIGTERM)
            killed.append((pid, before))
        except (OSError, ProcessLookupError):
            continue
    if killed:
        time.sleep(0.3)
    for pid, before in killed:
        try:
            if start_ticks(pid, proc_root) == before:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    return [pid for pid, _ in killed]


def execution(config, environment):
    """Inject the observer only when env execs the actual test application.

    Preloading it into timeout creates a second ranked constructor log with
    zero Jettys. Keep timeout and its cleanup token, but leave it unobserved.
    """
    timeout_environment = dict(environment)
    timeout_environment.pop('RM_RESOURCE_OBSERVER_DIR', None)
    application_environment = dict(timeout_environment)
    timeout = str(config['timeout']) + 's'
    command = ['/usr/bin/timeout', '--signal=TERM', '--kill-after=8s', timeout]
    if config['resource_probe']:
        observer = config['observers'][socket.gethostname()]
        preload = ':'.join(value for value in
                           (environment.get('LD_PRELOAD', ''), observer) if value)
        application_environment['LD_PRELOAD'] = preload
        application_environment['RM_RESOURCE_OBSERVER_DIR'] = config['run']
        command += ['/usr/bin/env', 'LD_PRELOAD=' + preload,
                    'RM_RESOURCE_OBSERVER_DIR=' + config['run']]
    command += [config['binary']] + config['arguments']
    return command, timeout_environment, application_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.cleanup:
        print(json.dumps({'terminated_own_pids': cleanup(config['nonce'], config['run'])}))
        return
    rank = int(os.environ['OMPI_COMM_WORLD_RANK'])
    directory = Path(config['run'])
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('UCX_') and key not in ('LD_PRELOAD', 'LD_DEBUG')}
    environment.update(config['environment'])
    environment['A89_RUN_NONCE'] = config['nonce']
    command, timeout_environment, application_environment = execution(config, environment)
    (directory / ('pid_rank%d.json' % rank)).write_text(json.dumps({
        'rank': rank, 'pid': os.getpid(), 'start_ticks': start_ticks(os.getpid()),
        'nonce': config['nonce'], 'hostname': socket.gethostname(),
        'cpu_affinity': sorted(os.sched_getaffinity(0)),
        'environment': {key: value for key, value in application_environment.items()
                        if key.startswith('UCX_') or key.startswith('LD_')}
    }, indent=2) + '\n')
    os.execve(command[0], command, timeout_environment)


if __name__ == '__main__':
    main()
