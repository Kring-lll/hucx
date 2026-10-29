#!/usr/bin/env python3
"""Loader-only smoke test: fake local provider, no MPI/device operations."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(sys.argv[1])
    work = root / 'symbol_smoke'
    work.mkdir()
    for source, target, extra in (
            ('fake_provider.c', 'liburma.so', ['-shared', '-fPIC']),
            ('local_client.c', 'local_client', ['-ldl'])):
        result = subprocess.run(['gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                                 '-I/usr/include/ub/umdk/urma', str(root / source),
                                 '-o', str(work / target)] + extra,
                                text=True, capture_output=True, timeout=40)
        (work / (source + '.compile.log')).write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('loader smoke compile failed: ' + source)
    env = dict(os.environ)
    env.pop('LD_LIBRARY_PATH', None)
    env.pop('FAKE_ERROR', None)
    env.update(LD_PRELOAD=str(root / 'libsend_probe.so'), HUCX_OSU_PROBE_DIR=str(work),
               OMPI_COMM_WORLD_RANK='0', A89_RUN_NONCE='0' * 32)
    result = subprocess.run([str(work / 'local_client'), str(work / 'liburma.so')],
                            env=env, text=True, capture_output=True, timeout=10)
    (work / 'client.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('loader smoke failed: EXIT=' + str(result.returncode))
    paths = list(work.glob('wr_rank0_pid*.json'))
    if len(paths) != 1:
        raise RuntimeError('loader smoke trace absent')
    row = json.loads(paths[0].read_text())
    if (row.get('loaded_handle_used') != 1 or row.get('provider_is_liburma') is not True
            or row.get('error_calls') != 0 or row.get('incomplete') != 0
            or len(row['buckets']) != 1 or row['buckets'][0]['bytes'] != 4096
            or row['buckets'][0]['success'] != 1):
        raise RuntimeError('loader smoke trace incorrect')
    print(json.dumps({'status': 'PASS', 'loaded_handle_used': 1,
                      'mock_only': True, 'device_urma_contract': 'NOT_TESTED'}))

if __name__ == '__main__':
    main()
