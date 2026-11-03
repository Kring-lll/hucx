import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import matrix
import run_integration as runner


class MatrixTests(unittest.TestCase):
    def test_reliable_boundaries_and_scope(self):
        self.assertEqual((matrix.AM_BCOPY, matrix.SINGLE_BOUNDARY, matrix.TWO_FRAGMENT_BOUNDARY), (3989, 3981, 7938))
        self.assertEqual(matrix.plan()['total_mpi_tasks'], 40)
        self.assertFalse(matrix.plan()['native_rma'])
        self.assertEqual(matrix.plan()['max_ppn'], 4)

    def test_configuration_canonical_units_and_failure(self):
        env = matrix.reliable_environment(Path('/accepted'), 4, path='am_rndv')
        text = '\n'.join('%s=%s' % (k, v) for k, v in env.items() if k.startswith('UCX_'))
        text = text.replace('SEG_SIZE=4096', 'SEG_SIZE=4K')
        text = text.replace('THRESH=16384', 'THRESH=16K')
        text = text.replace('SEND_RTO=100ms', 'SEND_RTO=100.00ms')
        text = text.replace('SEND_TIMEOUT=30s', 'SEND_TIMEOUT=30000ms')
        matrix.check_config(text, env, runner.boundary_config.bytes_value)
        for old, new in (('4K', '8K'), ('reliable_v3', 'iface_v2'), ('100.00ms', '200ms')):
            with self.assertRaises(ValueError):
                matrix.check_config(text.replace(old, new), env, runner.boundary_config.bytes_value)

    def test_environment_cleans_inherited_profiles(self):
        env = runner.effective_environment(Path('/accepted'), 1,
            {'UCX_TLS': 'tcp', 'LD_PRELOAD': '/old.so', 'MPI_UCX_TLS': 'rc_urma',
             'HUCX_SEND_INJECT_PROFILE': 'stale', 'OMPI_MCA_pml': 'ob1'}, Path('/proof/wr.so'))
        self.assertNotIn('MPI_UCX_TLS', env)
        self.assertNotIn('HUCX_SEND_INJECT_PROFILE', env)
        self.assertNotIn('/old.so', env['LD_PRELOAD'])
        self.assertEqual(env['OMPI_MCA_pml'], 'ucx')
        self.assertEqual(env['UCX_RM_URMA_NATIVE_RMA'], 'n')

    def test_probe_rejects_zero_receive_wrong_pid_and_counts(self):
        sizes, cycles = matrix.probe_schedule()
        rows = [dict(rank=r, world=2, peer=1-r, pid=100+r, nonce='a'*32, sent=len(sizes)*cycles,
                     received=len(sizes)*cycles, send_bytes=sum(sizes)*cycles,
                     receive_bytes=sum(sizes)*cycles, cycles=cycles, sizes=list(sizes), injection=False,
                     finalized=True, unexpected_messages=0, content='PASS', order='PASS', guards='PASS')
                for r in range(2)]
        matrix.inspect_probe(rows, 1, {0:100,1:101}, 'a'*32)
        for key, value in (('received',0), ('pid',999), ('receive_bytes',8), ('finalized',False), ('guards','FAIL')):
            bad = copy.deepcopy(rows)
            bad[1][key] = value
            with self.assertRaises(ValueError):
                matrix.inspect_probe(bad, 1, {0:100,1:101}, 'a'*32)

    def test_injection_requires_actual_interior_fragment_identity(self):
        rows = [dict(rank=r, pid=100+r, nonce='a'*32, calls=30, data_drops=1, ack_drops=2,
                     duplicates=1, total_bytes=65536, duplicate_offset=3965, drop_offset=7938,
                     duplicate_psn=7, drop_psn=8) for r in range(2)]
        matrix.inspect_injection(rows, {0:100,1:101}, 'a'*32)
        for key, value in (('drop_offset',0), ('duplicates',0), ('ack_drops',0), ('pid',333)):
            bad = copy.deepcopy(rows)
            bad[0][key] = value
            with self.assertRaises(ValueError):
                matrix.inspect_injection(bad, {0:100,1:101}, 'a'*32)


if __name__ == '__main__':
    unittest.main()
