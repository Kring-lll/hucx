import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support
import rank_exec


class PlanningTests(unittest.TestCase):
    def test_budget_covers_all_ranks_and_control(self):
        for ppn in range(1, 193):
            value = support.budget(ppn)
            self.assertEqual(value['max_peers'], 2 * ppn)
            self.assertGreaterEqual(value['rx_depth'], 2 * ppn * 10 + 2)
        self.assertEqual(support.budget(20)['rx_depth'], 512)
        self.assertEqual(support.budget(192)['rx_depth'], 4096)

    def test_pair_grid_is_symmetric_and_full(self):
        for ppn in (1, 2, 4, 20, 37, 192):
            for local in range(ppn):
                seen = set()
                for step in range(ppn):
                    other = (local + step) % ppn
                    self.assertEqual((other - step + ppn) % ppn, local)
                    seen.add(other)
                self.assertEqual(seen, set(range(ppn)))

    def test_levels_respect_real_capacity(self):
        self.assertEqual(support.levels(20), [])
        self.assertEqual(support.levels(28), [28])
        self.assertEqual(support.levels(60), [32, 48, 60])
        self.assertEqual(support.levels(256)[-1], 192)

    def test_transform_retains_mpi_and_mca_drops_old_placement(self):
        source = ['timeout', '60', 'env', 'UCX_TLS=rm_urma', '/mpi/bin/mpirun',
                  '--prefix', '/mpi', '--mca', 'pml', 'ucx', '--mca', 'coll', '^hcoll,ucc',
                  '-np', '2', '--host', 'old:1,peer:1', '--map-by', 'ppr:1:node',
                  '--bind-to', 'core', '-x', 'UCX_TLS', '/osu/osu_latency', '-c', '-m', '8:8']
        result = support.launcher(source, 20, ('a', 'b'), '/tool/wrapper', '/tool/config')
        self.assertEqual(result[0], '/mpi/bin/mpirun')
        self.assertIn('^hcoll,ucc', result)
        self.assertIn('a:20,b:20', result)
        self.assertIn('ppr:20:node', result)
        self.assertNotIn('old:1,peer:1', result)
        self.assertNotIn('-x', result)
        self.assertNotIn('60', result)

    def test_transform_rejects_unknown_options(self):
        with self.assertRaisesRegex(ValueError, 'unrecognized'):
            support.launcher(['/mpirun', '--guess', 'x', '/osu_latency', '-c'],
                             1, ('a', 'b'), '/w', '/c')

    def test_env_read_inline_and_normalized_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'saved.json'
            path.write_text(json.dumps({'command': ['env', 'UCX_NET_DEVICES=dev:1',
                'LD_LIBRARY_PATH=' + str(support.MINIMAL / 'lib'),
                '/mpi/mpirun', '/osu_latency', '-c'], 'environment': {}}))
            command, env = support.saved_spec(path, Path('/new'))
            self.assertEqual(env['UCX_NET_DEVICES'], 'dev:1')
            self.assertEqual(env['LD_LIBRARY_PATH'], '/new/lib')
            self.assertIn('LD_LIBRARY_PATH=/new/lib', command)

    def test_env_cannot_inherit_debug_or_new_mode_into_old(self):
        initial = {'UCX_NET_DEVICES': 'dev:1', 'UCX_RM_URMA_LINK_MODE': 'iface_v2',
                   'UCX_RM_URMA_MAX_PEERS': '999', 'LD_DEBUG': 'libs',
                   'LD_PRELOAD': '/rogue.so', 'LD_LIBRARY_PATH': '/extra',
                   'UCX_LOG_LEVEL': 'debug'}
        env = support.environment(initial, 'old_rm', Path('/old'), support.budget(20))
        self.assertNotIn('UCX_RM_URMA_LINK_MODE', env)
        self.assertNotIn('UCX_RM_URMA_MAX_PEERS', env)
        self.assertNotIn('LD_DEBUG', env)
        self.assertEqual(env['UCX_LOG_LEVEL'], 'warn')
        self.assertEqual(env['LD_LIBRARY_PATH'], '/old/lib:/extra')
        self.assertEqual(env['UCX_RM_URMA_RX_DEPTH'], '512')
        self.assertNotIn('rogue', env['LD_PRELOAD'])

    def test_maps_reject_mixed_library(self):
        valid = '\n'.join('0-1 rw /candidate/lib/' + lib + '.0' for lib in
                          ('libucp.so', 'libuct.so', 'libucs.so', 'libucm.so', 'libuct_ub.so'))
        self.assertTrue(support.check_maps(valid, '/candidate'))
        with self.assertRaisesRegex(ValueError, 'mixed'):
            support.check_maps(valid + '\n0-1 rw /system/lib/libuct.so.0', '/candidate')

    def test_observer_requires_all_ranks_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rank0_pid99.jsonl'
            rows = [{'event': 'start', 'rank': 0},
                    {'event': 'acquire', 'kind': 'jetty', 'rc': 0, 'mode': 1,
                     'ctx': 'a', 'ptr': 'b', 'depth': 64}]
            path.write_text('\n'.join(map(json.dumps, rows)))
            with self.assertRaisesRegex(ValueError, 'leak'):
                support.resource_counts(folder, range(2), 1, '/c', 64)

    def test_statistics_keeps_outliers_and_regressions(self):
        row = support.statistics_for([2.1, 30.0, 2.2])
        self.assertEqual(row['samples'], [2.1, 30.0, 2.2])
        self.assertEqual(row['maximum'], 30.0)
        self.assertEqual(row['median'], 2.2)

    def test_source_hashes_are_sha256(self):
        for value in (support.PRODUCT_MODULE, support.PRE_IFACE_MODULE, support.MINIMAL_MODULE):
            self.assertEqual(len(value), 64)


class CleanupTests(unittest.TestCase):
    def test_simulated_proc_does_not_signal_other_nonce_or_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for pid, nonce, command in ((90001, 'a' * 32, '/run/test/mpi_scale'),
                                         (90002, 'b' * 32, '/run/test/mpi_scale'),
                                         (90003, 'a' * 32, '/other/job')):
                process = root / str(pid)
                process.mkdir()
                fields = ['0'] * 20
                fields[19] = str(pid * 100)
                (process / 'stat').write_text(str(pid) + ' (name with spaces) ' + ' '.join(fields))
                (process / 'environ').write_bytes(('A89_RUN_NONCE=' + nonce).encode() + b'\0')
                (process / 'cmdline').write_bytes(command.encode() + b'\0')
            with patch('rank_exec.os.kill') as kill, patch('rank_exec.time.sleep'):
                self.assertEqual(rank_exec.cleanup('a' * 32, '/run/test', root), [90001])
            self.assertEqual([call.args[0] for call in kill.call_args_list], [90001, 90001])

    def test_cleanup_matches_nonce_path_and_does_not_kill_other_job(self):
        with tempfile.TemporaryDirectory(prefix='a89_cpu_') as folder:
            if int(Path('/proc/self/stat').read_text().split()[0]) != os.getpid():
                self.skipTest('Work运行时PID虚拟化；真实/proc清理留待节点，另测文件夹模拟')
            path = Path(folder) / 'sleeper.py'
            path.write_text('import time\ntime.sleep(30)\n')
            env = dict(os.environ, A89_RUN_NONCE='a' * 32)
            owned = subprocess.Popen([sys.executable, str(path)], env=env)
            unrelated = subprocess.Popen([sys.executable, str(path)], env=dict(os.environ))
            try:
                time.sleep(0.05)
                terminated = rank_exec.cleanup('a' * 32, folder)
                self.assertIn(owned.pid, terminated)
                owned.wait(timeout=3)
                self.assertIsNone(unrelated.poll())
            finally:
                for process in (owned, unrelated):
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
