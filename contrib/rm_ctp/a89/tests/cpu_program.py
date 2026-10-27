#!/usr/bin/env python3
"""Compile/run the real C test body through a deliberately tiny file API harness.

This checks test-program phase/socket/data logic ONLY, not actual MPI/UCX/URMA.
"""
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from support import summarize, resource_counts
from run_scale import comparison, render_summary


def run(binary, ppn, install, corrupt=False):
    with tempfile.TemporaryDirectory(prefix='a89_c_body_') as folder:
        processes = []
        streams = []
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        try:
            for rank in range(2 * ppn):
                env = dict(os.environ, CPU_MPI_DIR=folder, CPU_MPI_RANK=str(rank),
                           CPU_MPI_SIZE=str(2 * ppn), OMPI_COMM_WORLD_LOCAL_RANK=str(rank % ppn),
                           LD_PRELOAD=':'.join(str(p) for p in sorted(install.rglob('*.so*'))))
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
                metrics = summarize(rows, ppn)
                # Synthetic Jetty events below test file/schema/report contracts
                # only. The C producer supplies real PID and three kernel maps
                # snapshots; these fixture DSOs are NOT UCX/URMA libraries.
                trials = []
                for mode in ('iface', 'old_rm', 'rc'):
                    for row in rows:
                        events = [{'event': 'start', 'rank': row['rank']}]
                        pointers = ['p%d' % i for i in range(ppn + 1 if mode == 'rc' else 1)]
                        for pointer in pointers:
                            events.append({'event': 'acquire', 'kind': 'jetty', 'rc': 0,
                                           'mode': 2 if mode == 'rc' else 1, 'ctx': 'context',
                                           'ptr': pointer, 'depth': 64})
                        for pointer in pointers:
                            events.append({'event': 'release', 'kind': 'jetty', 'rc': 0, 'ptr': pointer})
                        path = Path(folder) / ('rank%d_pid%d.jsonl' % (row['rank'], row['pid']))
                        path.write_text('\n'.join(map(json.dumps, events)))
                        assert not path.with_suffix('.maps').exists()
                    resources = resource_counts(folder, range(2 * ppn), 2 if mode == 'rc' else 1,
                                                install, 64, {r['rank']: r['pid'] for r in rows})
                    for repeat in range(3):
                        trials.append({'name': mode + str(repeat), 'mode': mode,
                                       'resource_probe': False, 'metrics': metrics})
                    trials.append({'name': mode + '_resources', 'mode': mode, 'resource_probe': True,
                                   'metrics': metrics, 'resources': resources})
                compared = comparison(trials, ppn)
                report = {'comparisons': [compared], 'a8_functional': False, 'a9_functional': False,
                          'performance_acceptance': 'CPU SYNTHETIC CONTRACT ONLY; NOT MPI/UCX/URMA'}
                render_summary(Path(folder), report)
                assert 'CPU SYNTHETIC CONTRACT ONLY' in (Path(folder) / 'summary.md').read_text()
                json.dumps(compared)
                print('CPU_FILE_CONTRACT PPN=%d C_MAPS->RESOURCES->COMPARISON->REPORT=PASS (NO_UCX_URMA)' % ppn)
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
        install = Path(directory) / 'fake_install'
        (install / 'lib/ucx').mkdir(parents=True)
        source = Path(directory) / 'fixture_dso.c'
        source.write_text('void a89_cpu_fixture_only(void) {}\n')
        library = Path(directory) / 'fixture.so'
        subprocess.run(['gcc', '-shared', '-fPIC', str(source), '-o', str(library)], check=True)
        for name in ('ucp', 'uct', 'ucs', 'ucm'):
            shutil.copy2(library, install / 'lib' / ('lib' + name + '.so.0'))
        shutil.copy2(library, install / 'lib/ucx/libuct_ub.so.0.0.0')
        run(binary, 1, install)
        run(binary, 4, install)
        run(binary, 1, install, corrupt=True)


if __name__ == '__main__':
    main()
