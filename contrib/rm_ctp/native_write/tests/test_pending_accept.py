import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pending_accept_tests', ROOT / 'accept_pending_fix.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

class AcceptTests(unittest.TestCase):
    def test_new_product_binding_and_result_links(self):
        with tempfile.TemporaryDirectory() as folder:
            case = Path(folder)
            (case / 'native_write_old').mkdir()
            build = {'source_commit': 'new_product', 'module_sha256': 'new_module'}
            normal = types.SimpleNamespace()
            def write_main():
                self.assertEqual(sys.argv[1:], [str(case), '--run'])
                out = case / 'native_write_new'
                out.mkdir()
                (out / 'results.json').write_text(json.dumps({'status': 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS'}))
            def normal_main():
                self.assertEqual(normal.CASE, case)
                self.assertEqual(normal.COMMIT, 'new_product')
                self.assertEqual(normal.MODULE, 'new_module')
                self.assertEqual(normal.PROOF, 'native_write_new')
                out = case / 'write_normal_new'
                out.mkdir()
                (out / 'results.json').write_text(json.dumps({'status': 'WRITE_ENABLED_NORMAL_28_PASS_RMA_INVENTORY_COMPLETE'}))
            normal.main = normal_main
            argv = sys.argv
            result = a.stages(case, types.SimpleNamespace(main=write_main), normal, build)
            self.assertEqual(sys.argv, argv)
            self.assertEqual(Path(result['normal']).parent.name, 'write_normal_new')

    def test_write_failure_stops_before_normal_and_restores_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            def write_main():
                raise SystemExit(1)
            normal = types.SimpleNamespace(main=lambda: calls.append('unexpected'))
            argv = sys.argv
            with self.assertRaises(SystemExit):
                a.stages(Path(folder), types.SimpleNamespace(main=write_main), normal, {})
            self.assertEqual(calls, [])
            self.assertEqual(sys.argv, argv)

if __name__ == '__main__':
    unittest.main()
