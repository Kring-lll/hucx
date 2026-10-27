#!/usr/bin/env python3
"""Reuse tested rank execution; scope OSU cleanup by nonce + exact env path.

Original OSU argv has no output-directory positional argument. Do not depend on
an A89-specific argv substring to find our timeout/application processes.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import time

ROOT = Path(__file__).resolve().parent
helper = ROOT / 'a89_rank_exec.py'
if not helper.is_file():
    helper = ROOT.parent / 'a89/rank_exec.py'
spec = importlib.util.spec_from_file_location('osu_reused_a89_rank', helper)
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)


def cleanup(nonce, run, proc_root=Path('/proc')):
    if len(nonce) != 32 or any(value not in '0123456789abcdef' for value in nonce):
        raise ValueError('invalid OSU run nonce')
    marker = ('A89_RUN_NONCE=' + nonce).encode()
    directory = ('HUCX_OSU_PROBE_DIR=' + str(Path(run).resolve())).encode()
    killed = []
    for process in Path(proc_root).iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        pid = int(process.name)
        try:
            before = shared.start_ticks(pid, proc_root)
            environment = (process / 'environ').read_bytes().split(b'\0')
            if marker not in environment or directory not in environment:
                continue
            if shared.start_ticks(pid, proc_root) != before:
                continue
            os.kill(pid, signal.SIGTERM)
            killed.append((pid, before))
        except (OSError, ProcessLookupError):
            continue
    if killed:
        time.sleep(.3)
    for pid, before in killed:
        try:
            if shared.start_ticks(pid, proc_root) == before:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    return [pid for pid, _ in killed]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    if args.cleanup:
        config = json.loads(args.config.read_text())
        print(json.dumps({'terminated_own_pids': cleanup(config['nonce'], config['run'])}))
    else:
        shared.main()


if __name__ == '__main__':
    main()
