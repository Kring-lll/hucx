import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import evidence

spec = importlib.util.spec_from_file_location('mpi_rma_test_controller', ROOT / 'run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def data_output(sync='fence'):
    lines = []
    for generation in range(2):
        for round_id in range(16):
            for rank in range(2):
                lines.append('MPI_RMA_ROUND rank=%d generation=%d round=%d op=%s bytes=%d '
                             'origin=%d sync=%s DATA=PASS GUARDS=PASS' %
                             (rank, generation, round_id, 'PUT' if round_id < 8 else 'GET',
                              evidence.SIZES[round_id % 8], generation, sync))
    lines += ['MPI_RMA_DATA_PASS rank=%d sync=%s ROUNDS=32 WINDOWS=2' % (r, sync)
              for r in range(2)]
    return '\n'.join(lines) + '\n'


class EvidenceTest(unittest.TestCase):
    def test_two_directions_and_exact_rounds(self):
        self.assertEqual(evidence.probe_output(data_output(), 'fence')['rounds_per_rank'], 32)
        evidence.probe_output(data_output('lock_all'), 'lock_all')

    def test_missing_duplicate_and_wrong_size(self):
        lines = data_output().splitlines(True)
        for text in (''.join(lines[1:]), ''.join(lines + [lines[0]]),
                     data_output().replace('bytes=65536', 'bytes=65535', 1)):
            with self.assertRaises(ValueError):
                evidence.probe_output(text, 'fence')

    def test_per_rank_out_of_order(self):
        lines = data_output().splitlines(True)
        lines[0], lines[2] = lines[2], lines[0]
        with self.assertRaisesRegex(ValueError, 'out-of-order'):
            evidence.probe_output(''.join(lines), 'fence')

    def test_failed_guard_or_cleanup(self):
        for text in (data_output().replace('GUARDS=PASS', 'GUARDS=FAIL', 1),
                     data_output().replace('WINDOWS=2', 'WINDOWS=1', 1),
                     data_output() + 'MPI_RMA_MISMATCH rank=1\n'):
            with self.assertRaises(ValueError):
                evidence.probe_output(text, 'fence')

    def test_atomic_exact_both_ranks(self):
        text = ''.join('MPI_RMA_AMO_PASS rank=%d FAA32=PASS FAA64=PASS CAS32=PASS '
                       'CAS64=PASS CAS_MISS=PASS GUARDS=PASS\n' % rank for rank in range(2))
        evidence.probe_output(text, 'atomics')
        with self.assertRaises(ValueError):
            evidence.probe_output(text + text, 'atomics')
        with self.assertRaises(ValueError):
            evidence.probe_output(text.replace('CAS_MISS=PASS', 'CAS_MISS=FAIL', 1), 'atomics')

    def test_original_osu_no_validation_claim(self):
        for name, (_, header, metric, _) in evidence.BENCHMARKS.items():
            unit = 'Latency (us)' if metric == 'latency_us' else 'Bandwidth (MB/s)'
            text = '# %s test v7.5.2\n# Size %s\n65536 123.4\n' % (header, unit)
            result = evidence.osu_output(text, name, 65536)
            self.assertFalse(result['OSU_builtin_data_validation'])
            self.assertEqual(result[metric], 123.4)
            for bad in (text + '65536 99\n', text.replace('65536 123.4', '8 123.4'),
                        text.replace('123.4', 'nan'), text.replace('7.5.2', '7.5.1')):
                with self.assertRaises(ValueError):
                    evidence.osu_output(bad, name, 65536)

    def test_streaming_native_and_software_amo_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'application.log'
            path.write_text('RM native RMA post kind=put_zcopy length=4096\n'
                            'RM native RMA post kind=get_bcopy length=8\n'
                            'rkey ep cfg basic_rma: lane[0] rkey 0x0 sw_amo: lane[1] rkey 0x0\n')
            scans = [evidence.scan_ucx(path)]
            evidence.require_paths(scans, ('put', 'get', 'put_zcopy'), atomic=True)
            with self.assertRaisesRegex(ValueError, 'get_zcopy'):
                evidence.require_paths(scans, ('get_zcopy',))
            for text in ('RM native RMA post kind=get_bcopy length=4097\n',
                         'RM native RMA post kind=put_short length=0\n',
                         'UCX  ERROR failed device\n'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    evidence.scan_ucx(path)
            path.write_text('ordinary config only\n')
            with self.assertRaises(ValueError):
                evidence.require_paths([evidence.scan_ucx(path)], atomic=True)

    def test_log_missing_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'actual.log'
            with self.assertRaises(ValueError):
                evidence.scan_ucx(path)
            other = Path(tmp) / 'other.log'
            other.write_text('log')
            path.symlink_to(other)
            with self.assertRaises(ValueError):
                evidence.scan_ucx(path)

    def test_environment_prefixes_and_old_overrides(self):
        old = {'MPI_UCX_TLS': 'tcp', 'UCX_RM_URMA_SEG_SIZE': '8192',
               'OMPI_MCA_osc': 'rdma', 'LD_PRELOAD': '/old.so',
               'LD_LIBRARY_PATH': '/mpi/lib', 'PATH': '/usr/bin'}
        env = runner.settings(old, Path('/candidate'), 'rm', True, 'inf')
        self.assertEqual(env['UCX_TLS'], 'rm_urma,self')
        self.assertEqual(env['MPI_UCX_TLS'], env['UCX_TLS'])
        self.assertEqual(env['MPI_UCX_ZCOPY_THRESH'], 'inf')
        self.assertEqual(env['OMPI_MCA_osc'], 'ucx')
        self.assertIn('/mpi/lib', env['LD_LIBRARY_PATH'])
        self.assertFalse(any('SEG_SIZE' in key for key in env))
        self.assertNotIn('/old.so', env['LD_PRELOAD'])
        self.assertEqual(env['UCX_RM_URMA_NATIVE_RMA'], 'y')

    def test_forced_osc_and_retained_launcher_options(self):
        command = ['/mpi/bin/mpirun', '--prefix', '/mpi', '--mca', 'pml', 'ob1',
                   '--gmca', 'osc', 'rdma', '--mca', 'btl', '^tcp', '-np', '2', 'app']
        result = runner.fixed_launcher(command)
        self.assertEqual(result.count('osc'), 1)
        self.assertEqual(result[result.index('osc') + 1], 'ucx')
        self.assertIn('^tcp', result)
        self.assertIn('/mpi', result)


    def test_initialization_posts_cannot_substitute_business_rkey(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'api.jsonl'
            row = {'rank': 0, 'pid': 123, 'target': 1, 'mpi_op': 'GET',
                   'ucp_op': 'get', 'length': 65536, 'rkey': '0xabc', 'status': 1}
            path.write_text(json.dumps(row) + '\n')
            scan = {'rkey_protocols': {'0xother': ['basic_rma']}}
            with self.assertRaisesRegex(ValueError, 'business rkey'):
                evidence.correlate_mpi_data(path, scan, 0, 123, ('get',))
            for protocols in (['sw_rma'], ['sw_rma', 'basic_rma']):
                scan['rkey_protocols']['0xabc'] = protocols
                with self.assertRaisesRegex(ValueError, 'business rkey'):
                    evidence.correlate_mpi_data(path, scan, 0, 123, ('get',))
            scan['rkey_protocols']['0xabc'] = ['basic_rma']
            result = evidence.correlate_mpi_data(path, scan, 0, 123, ('get',))
            self.assertEqual(result['bytes'], 65536)
            with self.assertRaisesRegex(ValueError, 'identity'):
                evidence.correlate_mpi_data(path, scan, 0, 124, ('get',))

    def test_remote_collection_script_compiles_and_empty_is_allowed_on_failure(self):
        from unittest.mock import patch
        def capture(script, code):
            compile(code, '<remote-collection>', 'exec')
            return '[]'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner.osu.base, 'ssh', side_effect=capture):
                self.assertEqual(runner.collect_peer(Path(tmp)), [])


if __name__ == '__main__':
    unittest.main()
