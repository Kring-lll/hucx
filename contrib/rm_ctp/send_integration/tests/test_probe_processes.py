"""Run the actual probe C with CPU interface queues, not HMPI/URMA."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import matrix


@unittest.skipUnless(shutil.which('gcc'), 'gcc required for C control-flow tests')
class ProbeProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='send_probe_cpu_')
        cls.binary = Path(cls.build.name) / 'probe'
        subprocess.run(['gcc','-std=c99','-O1','-g','-Wall','-Wextra','-Werror',
                        '-I'+str(ROOT/'tests'), str(ROOT/'mpi_probe.c'),
                        str(ROOT/'tests/cpu_mpi.c'),'-o',str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def exercise(self, world, injection=False, fault=None):
        with tempfile.TemporaryDirectory(prefix='send_probe_processes_') as temp:
            directory = Path(temp)
            processes = []
            logs = []
            try:
                for rank in range(world):
                    env = dict(os.environ, TEST_RANK=str(rank), TEST_WORLD=str(world), TEST_QUEUE=temp,
                               HUCX_OSU_PROBE_DIR=temp, A89_RUN_NONCE='a'*32)
                    if fault:
                        env[fault] = '0' if fault == 'TEST_CORRUPT_RANK' else '1'
                    log = (directory/('rank%d.log'%rank)).open('w')
                    logs.append(log)
                    processes.append(subprocess.Popen([str(self.binary)] + (['--injection'] if injection else []),
                                                       env=env,stdout=log,stderr=subprocess.STDOUT))
                if fault:
                    code = processes[1].wait(timeout=12)
                    self.assertNotEqual(code, 0)
                    self.assertFalse((directory/'mpi_rank1.json').exists())
                    return
                for process in processes:
                    self.assertEqual(process.wait(timeout=20), 0)
                rows = [json.loads((directory/('mpi_rank%d.json'%rank)).read_text()) for rank in range(world)]
                pids = {rank:process.pid for rank,process in enumerate(processes)}
                proof = matrix.inspect_probe(rows, world//2, pids, 'a'*32, injection)
                self.assertGreater(proof['messages_each_direction_per_pair'], 0)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                for log in logs:
                    log.close()

    def test_normal_two_processes(self): self.exercise(2)
    def test_normal_four_processes(self): self.exercise(4)
    def test_normal_eight_processes(self): self.exercise(8)
    def test_injection_workload_two_processes(self): self.exercise(2, True)
    def test_corrupted_content_fails(self): self.exercise(2, fault='TEST_CORRUPT_RANK')
    def test_far_guard_corruption_fails(self): self.exercise(2, fault='TEST_FAR_GUARD_RANK')


if __name__ == '__main__':
    unittest.main()
