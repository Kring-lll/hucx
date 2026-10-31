import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rma_controller_test', ROOT / 'run_candidate.py')
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class ControllerTest(unittest.TestCase):
    def fixture(self, root, kind='basic'):
        n = 8 if kind == 'basic' else 16
        prefix = 'NATIVE_BASIC' if kind == 'basic' else 'UCP_RMA'
        for role in ('sender', 'receiver'):
            name = ('reader' if kind == 'basic' else 'initiator') if role == 'sender' else 'provider'
            if kind == 'basic':
                sizes = (8, 64, 8, 4096, 8, 64, 4096, 8)
                offsets = (0, 8, 64, 128, 0, 8, 64, 128)
                text = ''.join('NATIVE_BASIC_ROUND=%d BYTES=%d OFFSET=%d ROLE=%s DATA=PASS\n' % (i, sizes[i], offsets[i], name) for i in range(n))
            else:
                sizes = (8, 65536, 64, 16385, 4096, 8193, 8, 65536)
                text = ''.join('UCP_RMA_ROUND=%d OP=%s BYTES=%d ROLE=%s DATA=PASS\n' % (i, 'PUT' if i < 8 else 'GET', sizes[i % 8], name) for i in range(n))
            text += '%s_PASS ROUNDS=%d GUARDS=PASS\n' % (prefix, n)
            (root / (role + '.log')).write_text(text)
            (root / (role + '.maps')).write_text('maps fixture')
            (root / (role + '.ucx.log')).write_text('RM native RMA post kind=get_bcopy length=4096\n')

    def inspect(self, root, kind='basic'):
        checker = types.SimpleNamespace(check_maps=lambda *args: None)
        with patch.object(controller, 'load', return_value=checker):
            return controller.inspect_pair(root, Path('/candidate'), kind, ['get_bcopy'])

    def test_pass_and_missing_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            self.assertEqual(len(self.inspect(root)), 2)
            (root / 'sender.ucx.log').write_text('unrelated trace\n')
            with self.assertRaisesRegex(ValueError, 'not observed'):
                self.inspect(root)

    def test_duplicate_or_missing_round(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for change in ('duplicate', 'missing'):
                self.fixture(root)
                path = root / 'sender.log'
                text = path.read_text()
                text = text + 'NATIVE_BASIC_ROUND=7 DATA=PASS\n' if change == 'duplicate' else text.replace('NATIVE_BASIC_ROUND=7 BYTES=8 OFFSET=128 ROLE=reader DATA=PASS\n', '')
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, 'receipt'):
                    self.inspect(root)

    def test_log_error_and_request_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for invalid in ('UCX  ERROR hardware failure\n',
                            'RM native RMA post kind=get_bcopy length=4097\n',
                            'RM native RMA post kind=get_bcopy length=0\n'):
                self.fixture(root)
                (root / 'sender.ucx.log').write_text(invalid)
                with self.assertRaises(ValueError):
                    self.inspect(root)

    def test_missing_log_and_bad_maps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            (root / 'receiver.ucx.log').unlink()
            with self.assertRaisesRegex(ValueError, 'nonregular'):
                self.inspect(root)
            self.fixture(root)
            checker = types.SimpleNamespace(check_maps=lambda *args: (_ for _ in ()).throw(ValueError('wrong library')))
            with patch.object(controller, 'load', return_value=checker):
                with self.assertRaisesRegex(ValueError, 'wrong library'):
                    controller.inspect_pair(root, Path('/candidate'), 'basic', ['get_bcopy'])

    def test_ucp_rows_and_failed_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, 'ucp')
            self.assertEqual(len(self.inspect(root, 'ucp')), 2)
            path = root / 'sender.log'
            path.write_text(path.read_text().replace('DATA=PASS', 'DATA=FAIL', 1))
            with self.assertRaisesRegex(ValueError, 'receipt'):
                self.inspect(root, 'ucp')

    def test_environment_and_context_restore(self):
        env = controller.environment(Path('/candidate'))
        self.assertEqual(env['UCX_RM_URMA_NATIVE_RMA'], 'y')
        self.assertEqual(env['UCX_RM_URMA_NATIVE_READ'], 'n')
        self.assertEqual(env['UCX_RM_URMA_NATIVE_WRITE'], 'n')
        self.assertEqual(env['UCX_TLS'], 'rm_urma')
        self.assertFalse(any('SEG_SIZE' in key for key in env))
        old = lambda *args: {'old': 'kept'}
        legacy = types.SimpleNamespace(values=old)
        with self.assertRaises(RuntimeError):
            with controller.enabled_normal(legacy):
                self.assertEqual(legacy.values('iface')['UCX_RM_URMA_NATIVE_RMA'], 'y')
                raise RuntimeError('intentional')
        self.assertIs(legacy.values, old)


if __name__ == '__main__':
    unittest.main()
