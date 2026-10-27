"""Component prerequisite and captured CLI diagnostics; no real MPI claim."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as runner


class PreflightTests(unittest.TestCase):
    def component_text(self, components):
        return '\n'.join('mca:coll:%s:version:"component":4.1.6' % name for name in components)

    def test_blocking_only_whitelist_is_rejected_before_singleton_mpi_init(self):
        self.assertEqual(set(runner.COLL.split(',')), {'tuned', 'basic', 'libnbc', 'self'})
        old = self.component_text(['tuned', 'basic', 'self'])
        with self.assertRaisesRegex(ValueError, 'libnbc'):
            runner.check_collective_components(old)
        good = old + '\nmca:coll:libnbc:param:coll_libnbc_priority:value:10\n'
        self.assertIn('libnbc', runner.check_collective_components(good))

    def test_cli_exit_and_exact_relevant_environment_are_saved_even_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, OMPI_MCA_coll=runner.COLL, LD_LIBRARY_PATH='/fixture/mpi')
            result = runner.capture_cli([sys.executable, '-c', 'import sys;print("fixture coll error");sys.exit(7)'],
                                         env, root, 'cli')
            self.assertEqual(result.returncode, 7)
            saved = json.loads((root / 'cli.json').read_text())
            self.assertEqual(saved['exit'], 7)
            self.assertEqual(saved['environment']['OMPI_MCA_coll'], runner.COLL)
            self.assertEqual(saved['environment']['LD_LIBRARY_PATH'], '/fixture/mpi')
            self.assertIn('fixture coll error', (root / 'cli.txt').read_text())

    def test_both_hosts_must_supply_all_mandatory_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = self.component_text(runner.COLL.split(','))
            local = type('Result', (), {'returncode': 0, 'stdout': good, 'stderr': ''})()
            peer = {'exit': 0, 'stdout': self.component_text(['tuned','basic','self']), 'stderr': ''}
            with patch.object(runner, 'capture_cli', return_value=local), \
                 patch.object(runner.base, 'ssh', return_value=json.dumps(peer)):
                with self.assertRaisesRegex(ValueError, 'libnbc'):
                    runner.collective_preflight(root, Path('/mpi/ompi_info'), {}, {})
            peer['stdout'] = good
            with patch.object(runner, 'capture_cli', return_value=local), \
                 patch.object(runner.base, 'ssh', return_value=json.dumps(peer)):
                result = runner.collective_preflight(root, Path('/mpi/ompi_info'), {}, {})
                self.assertIn('libnbc', result['node97_available'])
