import ast
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('write_env_controller', ROOT / 'run_candidate.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

class ControllerTests(unittest.TestCase):
    def test_configuration_matches_accepted_direct_probe(self):
        source = ast.parse((ROOT.parent / 'r1/a7_iface_four_ep_20261026.py').read_text())
        assignments = [n for n in ast.walk(source) if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'environment' for t in n.targets)]
        value = next(n.value for n in assignments if isinstance(n.value, ast.Dict))
        old = {ast.literal_eval(k): ast.literal_eval(v) for k, v in zip(value.keys, value.values)
               if isinstance(k, ast.Constant) and k.value.startswith('UCX_RM_URMA_')}
        new = c.trial_environment(Path('/case'))
        self.assertGreaterEqual(len(old), 8)
        for key, value in old.items():
            self.assertEqual(new[key], value, key)
        self.assertEqual(new['UCX_RM_URMA_NATIVE_WRITE'], 'y')

    def test_budget(self):
        env = c.trial_environment(Path('/case'))
        k = int(env['UCX_RM_URMA_MAX_PEERS'])
        w = int(env['UCX_RM_URMA_FC_WND_SIZE'])
        self.assertEqual(env['UCX_RM_URMA_FC_ENABLE'], 'y')
        self.assertLessEqual(k * w + 2 * k + 2, int(env['UCX_RM_URMA_RX_DEPTH']))

    def test_failed_process_does_not_fetch_maps(self):
        fetched = []
        with self.assertRaisesRegex(RuntimeError, 'sender_exit=2 receiver_exit=2'):
            c.collect_receiver_outputs(Path('/run'), 2, 2, fetched.append)
        self.assertEqual(fetched, ['receiver.log'])

    def test_success_requires_maps(self):
        fetched = []
        c.collect_receiver_outputs(Path('/run'), 0, 0, fetched.append)
        self.assertEqual(fetched, ['receiver.log', 'receiver.maps'])

    def test_failure_reason_is_printed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'receiver.log').write_text('DIRECT_IFACE_STATUS: open RM iface: Invalid parameter\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                c.print_probe_errors(root)
            self.assertIn('Invalid parameter', output.getvalue())

if __name__ == '__main__':
    unittest.main()
