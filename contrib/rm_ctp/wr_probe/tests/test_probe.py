import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run_probe import inspect_summary

class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.build = Path(cls.temporary.name)
        include = os.environ.get('URMA_TEST_INCLUDE', '/usr/include/ub/umdk/urma')
        def compile_file(source, output, extra):
            subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                            '-I' + include, str(source), '-o', str(output)] + extra,
                           check=True, capture_output=True)
        compile_file(ROOT / 'send_probe.c', cls.build / 'libprobe.so',
                     ['-fPIC', '-shared', '-ldl', '-pthread'])
        compile_file(ROOT / 'tests/fake_provider.c', cls.build / 'liburma.so', ['-fPIC', '-shared'])
        compile_file(ROOT / 'tests/fake_client.c', cls.build / 'client',
                     ['-L' + str(cls.build), '-Wl,-rpath,' + str(cls.build), '-lurma'])
        compile_file(ROOT / 'tests/local_client.c', cls.build / 'local_client', ['-ldl'])
    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()
    def invoke(self, count=1, **extra):
        with tempfile.TemporaryDirectory() as run:
            env = dict(os.environ, LD_PRELOAD=str(self.build / 'libprobe.so'),
                       HUCX_OSU_PROBE_DIR=run, OMPI_COMM_WORLD_RANK='0', A89_RUN_NONCE='a'*32)
            env.update(extra)
            subprocess.run([str(self.build / 'client'), str(count)], env=env, check=True)
            paths = list(Path(run).glob('wr_rank0_pid*.json'))
            if extra.get('FAKE_UM'):
                self.assertEqual(paths, [])
                return None
            self.assertEqual(len(paths), 1)
            return json.loads(paths[0].read_text())
    def test_sum_sge_chain_forwarding_unchanged(self):
        row = self.invoke(2)
        self.assertTrue(row['provider_is_liburma'])
        self.assertEqual(row['incomplete'], 0)
        self.assertEqual(sum(b['submitted'] for b in row['buckets']), 2)
        self.assertEqual({b['bytes'] for b in row['buckets']}, {4096})
        value = inspect_summary(row, 0, row['pid'], 'a'*32)
        self.assertEqual(value['inline_wrs'], 1)
    def test_error_never_counts_success(self):
        row = self.invoke(2, FAKE_ERROR='1')
        self.assertEqual(row['error_calls'], 1)
        self.assertEqual(sum(b['success'] for b in row['buckets']), 0)
        with self.assertRaises(ValueError): inspect_summary(row, 0, row['pid'], 'a'*32)
    def test_non_send_is_excluded(self):
        row = self.invoke(2, FAKE_OTHER='1')
        self.assertEqual(row['other_wrs'], 1)
        self.assertEqual(sum(b['submitted'] for b in row['buckets']), 1)
    def test_bad_metadata_rejected_without_changing_provider_result(self):
        row = self.invoke(1, FAKE_INVALID='1')
        self.assertEqual(row['incomplete'], 1)
        with self.assertRaises(ValueError): inspect_summary(row, 0, row['pid'], 'a'*32)
    def test_um_calls_forwarded_but_not_counted(self):
        self.assertIsNone(self.invoke(1, FAKE_UM='1'))
    def local_invoke(self, missing=False, error=False):
        with tempfile.TemporaryDirectory() as run:
            env = dict(os.environ, LD_PRELOAD=str(self.build / 'libprobe.so'),
                       HUCX_OSU_PROBE_DIR=run, OMPI_COMM_WORLD_RANK='0', A89_RUN_NONCE='a'*32)
            env.pop('LD_LIBRARY_PATH', None)
            if error: env['FAKE_ERROR'] = '1'
            result = subprocess.run([str(self.build / 'local_client'),
                                     'missing' if missing else str(self.build / 'liburma.so')],
                                    env=env, capture_output=True, text=True)
            if missing:
                self.assertEqual(result.returncode, 127)
                self.assertIn('WR_PROBE_RESOLVE_ERROR', result.stderr)
                self.assertEqual(list(Path(run).glob('wr_*.json')), [])
                return None
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = list(Path(run).glob('wr_rank0_pid*.json'))
            self.assertEqual(len(paths), 1)
            row = json.loads(paths[0].read_text())
            self.assertEqual(row['loaded_handle_used'], 1)
            self.assertTrue(row['provider_is_liburma'])
            return row
    def test_local_provider_success(self):
        row = self.local_invoke()
        self.assertEqual(inspect_summary(row, 0, row['pid'], 'a'*32)['max_send_bytes'], 4096)
    def test_local_provider_error_forwarded(self):
        row = self.local_invoke(error=True)
        self.assertEqual(row['error_calls'], 1)
        self.assertEqual(row['buckets'][0]['success'], 0)
    def test_missing_provider_is_tool_failure(self):
        self.local_invoke(missing=True)
    def test_long_chain_rejected(self):
        row = self.invoke(257)
        self.assertGreater(row['incomplete'], 0)
    def test_identity_and_length_rejected(self):
        row = self.invoke()
        with self.assertRaises(ValueError): inspect_summary(row, 1, row['pid'], 'a'*32)
        row['buckets'][0]['bytes'] = 4097
        with self.assertRaises(ValueError): inspect_summary(row, 0, row['pid'], 'a'*32)

if __name__ == '__main__': unittest.main()
