"""Real C/shared-loader tests with an explicit CPU PMPI fixture, not real MPI."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import analysis
import run as runner

FAKE_MPI = r'''
#include <stdlib.h>
#include "mpi.h"
int PMPI_Init(int *a, char ***b) { (void)a; (void)b; return 0; }
int PMPI_Init_thread(int *a, char ***b, int r, int *p) { *p = r; return PMPI_Init(a,b); }
int PMPI_Finalize(void) { return 0; }
int PMPI_Comm_rank(MPI_Comm c, int *r) { (void)c; *r = atoi(getenv("OMPI_COMM_WORLD_RANK")); return 0; }
int PMPI_Comm_size(MPI_Comm c, int *r) { (void)c; *r = 2; return 0; }
int MPI_Init(int *a, char ***b) { return PMPI_Init(a,b); }
int MPI_Init_thread(int *a, char ***b, int r, int *p) { return PMPI_Init_thread(a,b,r,p); }
int MPI_Finalize(void) { return PMPI_Finalize(); }
'''
APPLICATION = r'''
#define _GNU_SOURCE
#include <sched.h>
#include <stdlib.h>
#include <stdio.h>
#include <unistd.h>
#include <string.h>
#include "mpi.h"
int main(int argc, char **argv) {
    cpu_set_t mask; CPU_ZERO(&mask); CPU_SET(atoi(getenv("CPU_FIXTURE_CORE")), &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask) != 0) { return 2; }
    int provided = 0;
    int code = argc > 1 ? MPI_Init_thread(&argc,&argv,0,&provided) : MPI_Init(&argc,&argv);
    if (code != 0) { return 3; }
    const char *benchmark = getenv("CPU_OSU_FIXTURE");
    if (benchmark == NULL) {
        puts("CPU_FIXTURE_DATA_BODY_NO_REAL_MPI");
    } else if (atoi(getenv("OMPI_COMM_WORLD_RANK")) == 0) {
        int validation = 0, iterations = 0;
        for (int i = 1; i < argc; ++i) {
            if (strcmp(argv[i], "--validation") == 0) { validation = 1; }
            if (strcmp(argv[i], "--iterations") == 0 && i + 1 < argc) { iterations = atoi(argv[i+1]); }
        }
        if (strcmp(benchmark, "osu_multi_lat") == 0) {
            puts("# OSU MPI Multi Latency Test v7.5.2");
            printf("8 2.30%s\n", validation ? " Pass" : "");
        } else {
            puts("# OSU MPI All-to-All Personalized Exchange Latency Test v7.5.2");
            printf("8 4.00 3.90 4.10 %d%s\n", iterations, validation ? " Pass" : "");
        }
    }
    return MPI_Finalize();
}
'''


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='osu_pmpi_cpu_')
        cls.root = Path(cls.temporary.name)
        cls.install = cls.root / 'candidate'
        (cls.install / 'lib/ucx').mkdir(parents=True)
        (cls.root / 'fake_mpi.c').write_text(FAKE_MPI)
        (cls.root / 'application.c').write_text(APPLICATION)
        (cls.root / 'empty.c').write_text('int cpu_fixture_only;\n')
        cls.mpi = cls.root / 'libmpi.so.40'
        include = str(ROOT / 'tests')
        common = ['gcc', '-std=c99', '-O2', '-Wall', '-Wextra', '-Werror', '-I' + include]
        subprocess.run(common + ['-shared', '-fPIC', str(cls.root / 'fake_mpi.c'),
                         '-Wl,-soname,libmpi.so.40', '-o', str(cls.mpi)], check=True)
        cls.probe = cls.root / 'identity.so'
        link = ['-L' + str(cls.root), '-l:libmpi.so.40', '-Wl,-rpath,' + str(cls.root)]
        subprocess.run(common + ['-shared', '-fPIC', str(ROOT / 'identity.c')] + link +
                       ['-o', str(cls.probe)], check=True)
        cls.binary = cls.root / 'application'
        subprocess.run(common + [str(cls.root / 'application.c')] + link +
                       ['-o', str(cls.binary)], check=True)
        cls.libraries = []
        for name in ('libucp.so.0', 'libuct.so.0', 'libucs.so.0', 'libucm.so.0', 'libuct_ub.so.0'):
            path = cls.install / ('lib/ucx' if name.startswith('libuct_ub') else 'lib') / name
            subprocess.run(['gcc', '-shared', '-fPIC', str(cls.root / 'empty.c'), '-o', str(path)], check=True)
            cls.libraries.append(path)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_both_mpi_startup_apis_produce_actual_pid_maps_and_finalize_rss(self):
        core = min(os.sched_getaffinity(0))
        for threaded in (False, True):
            with self.subTest(threaded=threaded), tempfile.TemporaryDirectory(dir=self.root) as folder:
                run = Path(folder)
                for rank in (0, 1):
                    env = dict(os.environ, OMPI_COMM_WORLD_RANK=str(rank), HUCX_OSU_PROBE_DIR=folder,
                               CPU_FIXTURE_CORE=str(core), LD_PRELOAD=':'.join(map(str, self.libraries + [self.probe])))
                    result = subprocess.run([str(self.binary)] + (['threaded'] if threaded else []),
                                             env=env, text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'CPU_FIXTURE_DATA_BODY_NO_REAL_MPI')
                    row = json.loads((run / ('rank%d.json' % rank)).read_text())
                    self.assertTrue(row['finalized'])
                    self.assertEqual(row['proof_error'], 0)
                    self.assertGreater(row['pid'], 0)
                    (run / ('pid_rank%d.json' % rank)).write_text(json.dumps({
                        'rank': rank, 'nonce': 'a' * 32, 'hostname': socket.gethostname(), 'cpu_affinity': [core]}))
                inventory = [{'hostname': socket.gethostname(), 'allowed_cpus': [core]}] * 2
                result = analysis.inspect_ranks(run, 1, inventory, self.install, 'a' * 32, str(self.mpi))
                self.assertGreater(result['before_finalize_rss_sum_mib'], 0)
                path = run / 'rank1_before_finalize.maps'
                path.write_text(path.read_text().replace(str(self.install), '/wrong'))
                with self.assertRaisesRegex(ValueError, 'mapping'):
                    analysis.inspect_ranks(run, 1, inventory, self.install, 'a' * 32, str(self.mpi))

    def test_probe_has_no_timed_data_or_wtime_wrappers(self):
        text = subprocess.check_output(['nm', '-D', '--defined-only', str(self.probe)], text=True)
        exported = {line.split()[-1] for line in text.splitlines()}
        self.assertEqual({name for name in exported if name.startswith('MPI_')},
                         {'MPI_Init', 'MPI_Init_thread', 'MPI_Finalize'})

    def test_runner_all_benchmarks_modes_and_phases_with_real_probe_file_contract(self):
        # The launcher/provider are explicit synthetic fixtures. Real C PID,
        # maps/RSS, output parsing, report generation and cleanup checks execute.
        core = min(os.sched_getaffinity(0))
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            root = Path(folder)
            case = root / 'case'
            case.mkdir()
            (case / 'install').symlink_to(self.install, target_is_directory=True)
            out = case / 'run'
            out.mkdir()
            (out / 'libosu_identity.so').write_bytes(self.probe.read_bytes())
            (out / 'osu_rank.py').write_bytes((runner.ROOT / 'osu_rank.py').read_bytes())
            (out / 'a89_rank_exec.py').write_bytes((runner.A89 / 'rank_exec.py').read_bytes())
            osu = root / 'osu'
            for benchmark, (relative, _, _) in analysis.BENCHMARKS.items():
                path = osu / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.binary.read_bytes())
                path.chmod(0o755)
            host = socket.gethostname()
            inventory = [{'hostname': host, 'boot_id': 'fixture', 'allowed_cpus': [core],
                          'available_cores': 1, 'mem_available_kib': 1024*1024}] * 2
            evidence = {'inventory': inventory, 'benchmarks': {
                name: {'mpi_library': str(self.mpi)} for name in analysis.BENCHMARKS}}
            args = types.SimpleNamespace(iterations=5000, warmup=1000, timeout=60)
            commands = {mode: (['/fixture/mpirun', '/fixture/osu_latency'],
                               {'CPU_FIXTURE_CORE': str(core)}) for mode in ('rm','rc')}

            def execute_fixture(command, env, log, timeout):
                config = json.loads(Path(command[-1]).read_text())
                texts = []
                run = Path(config['run'])
                for rank in (0, 1):
                    child_env = dict(os.environ, **config['environment'])
                    child_env.update(OMPI_COMM_WORLD_RANK=str(rank), CPU_OSU_FIXTURE=Path(config['binary']).name)
                    child_env['LD_PRELOAD'] += ':' + str(self.libraries[-1])
                    child = subprocess.run([config['binary']] + config['arguments'], env=child_env,
                                           text=True, capture_output=True, timeout=10)
                    if child.returncode:
                        raise RuntimeError(child.stderr)
                    texts.append(child.stdout + child.stderr)
                    row = json.loads((run / ('rank%d.json' % rank)).read_text())
                    (run / ('pid_rank%d.json' % rank)).write_text(json.dumps({
                        'rank': rank, 'nonce': config['nonce'], 'hostname': host, 'cpu_affinity': [core]}))
                    if config['resource_probe']:
                        mode = 1 if config['environment']['UCX_TLS'].startswith('rm_') else 2
                        events = [{'event': 'start', 'rank': rank},
                            {'event': 'acquire', 'kind': 'jetty', 'rc': 0, 'mode': mode,
                             'ptr': 'p', 'ctx': 'context', 'depth': 64},
                            {'event': 'release', 'kind': 'jetty', 'rc': 0, 'ptr': 'p'}]
                        (run / ('rank%d_pid%d.jsonl' % (rank,row['pid']))).write_text('\n'.join(map(json.dumps,events)))
                log.write_text(''.join(texts))
                return {'exit': 0, 'launcher_wall_s': .1}

            def ssh_fixture(script, *args, **kwargs):
                return '{"terminated_own_pids":[]}' if '--cleanup' in script else ''

            with patch.object(runner, 'DEFAULT_CASE', case), patch.object(runner, 'OSU', osu), \
                 patch.object(runner.base, 'inventory', return_value=inventory), \
                 patch.object(runner.base, 'ssh', side_effect=ssh_fixture), \
                 patch.object(runner.base, 'copy_remote'), patch.object(runner.base, 'parsed_config'), \
                 patch.object(runner.base, 'run_logged', side_effect=execute_fixture), patch.object(runner, 'fetch'):
                for benchmark in analysis.BENCHMARKS:
                    for mode in ('rc', 'rm'):
                        for phase in ('validation','timing','resources'):
                            row = runner.one_trial(out, commands, evidence, benchmark, mode, 1, phase, 0, args)
                            self.assertTrue(row['passed'], row.get('error', row.get('cleanup_error')))
                            self.assertEqual(row['output']['validation_enabled'], phase == 'validation')
