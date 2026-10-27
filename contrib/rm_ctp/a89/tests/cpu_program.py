#!/usr/bin/env python3
"""Compile/run the real C test body through a deliberately tiny file API harness.

This checks test-program phase/socket/data logic ONLY, not actual MPI/UCX/URMA.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from support import summarize


def run(binary, ppn, corrupt=False):
    with tempfile.TemporaryDirectory(prefix='a89_c_body_') as folder:
        processes = []
        streams = []
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        try:
            for rank in range(2 * ppn):
                env = dict(os.environ, CPU_MPI_DIR=folder, CPU_MPI_RANK=str(rank),
                           CPU_MPI_SIZE=str(2 * ppn), OMPI_COMM_WORLD_LOCAL_RANK=str(rank % ppn))
                if corrupt:
                    env['CPU_MPI_CORRUPT'] = '1'
                stream = open(Path(folder) / ('p%d.log' % rank), 'w')
                streams.append(stream)
                processes.append(subprocess.Popen([str(binary), str(ppn), '127.0.0.1',
                    str(port), folder, '2', '5', socket.gethostname(), socket.gethostname()],
                    env=env, stdout=stream, stderr=subprocess.STDOUT))
            codes = [process.wait(timeout=15) for process in processes]
            if corrupt:
                if not any(code != 0 for code in codes):
                    raise RuntimeError('corruption was not detected')
            else:
                if any(codes):
                    raise RuntimeError('C-body harness failed: ' + str(codes) + '\n' +
                        '\n'.join(p.read_text() for p in Path(folder).glob('p*.log')))
                rows = [json.loads((Path(folder) / ('rank%d.json' % rank)).read_text())
                        for rank in range(2 * ppn)]
                summarize(rows, ppn)
            print('CPU_C_BODY PPN=%d CORRUPT=%s CHECK=PASS (NOT_REAL_MPI)' % (ppn, corrupt))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait()
            for stream in streams:
                stream.close()


def main():
    with tempfile.TemporaryDirectory(prefix='a89_c_build_') as directory:
        binary = Path(directory) / 'mpi_scale'
        subprocess.run(['gcc', '-std=c99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                        '-I' + str(ROOT / 'tests/cpu_mpi'), str(ROOT / 'mpi_scale.c'),
                        str(ROOT / 'tests/cpu_mpi/file_mpi.c'), '-o', str(binary)], check=True)
        run(binary, 1)
        run(binary, 4)
        run(binary, 1, corrupt=True)


if __name__ == '__main__':
    main()
