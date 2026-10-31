import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('read_acceptance_test', ROOT / 'accept_candidate.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class AcceptanceTests(unittest.TestCase):
    def test_both_flags_and_restore(self):
        original = lambda mode, debug=False: {'kept': mode, 'debug': debug}
        legacy = types.SimpleNamespace(values=original)
        with self.assertRaises(RuntimeError):
            with a.both_enabled(legacy):
                self.assertEqual(legacy.values('iface', True),
                                 {'kept': 'iface', 'debug': True,
                                  'UCX_RM_URMA_NATIVE_WRITE': 'y',
                                  'UCX_RM_URMA_NATIVE_READ': 'y'})
                raise RuntimeError('forced failure')
        self.assertIs(legacy.values, original)

    def test_failed_read_stops_write_and_restores(self):
        calls = []
        def fail():
            raise SystemExit(1)
        environment = lambda case: {}
        enabled = object()
        write = types.SimpleNamespace(main=lambda: calls.append('write'), trial_environment=environment)
        normal = types.SimpleNamespace(main=lambda: calls.append('normal'), enabled_normal=enabled)
        argv = sys.argv
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(SystemExit):
                a.stages(Path(folder), types.SimpleNamespace(main=fail), write, normal, {})
        self.assertEqual(calls, [])
        self.assertIs(sys.argv, argv)
        self.assertIs(write.trial_environment, environment)
        self.assertIs(normal.enabled_normal, enabled)

    def test_result_rejects_foreign_product(self):
        with tempfile.TemporaryDirectory() as folder:
            case = Path(folder)
            out = case / 'native_read_wrong'
            out.mkdir()
            (out / 'results.json').write_text(json.dumps({'status': 'PASS', 'source_commit': 'old', 'module_sha256': 'm'}))
            with self.assertRaisesRegex(ValueError, 'product/status'):
                a.new_result(case, 'native_read_', set(), 'PASS',
                             {'source_commit': 'new', 'module_sha256': 'm'})

    def test_stages_bind_new_write_and_recheck_read_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            case = Path(folder)
            build = {'source_commit': 'new', 'module_sha256': 'module'}
            def receipt(name, status):
                out = case / name
                out.mkdir()
                (out / 'results.json').write_text(json.dumps(dict(build, status=status)))
                return out
            read = types.SimpleNamespace(main=lambda: receipt('native_read_one',
                       'DIRECT_NATIVE_READ_DATA_MAPS_REGISTRATION_RELEASE_PASS'))
            write = types.SimpleNamespace(trial_environment=lambda case: {'UCX_RM_URMA_NATIVE_WRITE': 'y'})
            normal = types.SimpleNamespace(enabled_normal=object())
            def write_main():
                self.assertEqual(write.trial_environment(case)['UCX_RM_URMA_NATIVE_READ'], 'n')
                receipt('native_write_one', 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS')
            write.main = write_main
            def normal_main():
                self.assertEqual(normal.CASE, case)
                self.assertEqual(normal.PROOF, 'native_write_one')
                self.assertEqual(normal.COMMIT, 'new')
                out = receipt('write_normal_one', 'WRITE_ENABLED_NORMAL_28_PASS_RMA_INVENTORY_COMPLETE')
                matrix = case / 'matrix'
                matrix.mkdir()
                log = matrix / 'row.log'
                (matrix / 'row.json').write_text(json.dumps({'environment': {'UCX_RM_URMA_NATIVE_READ': 'y'}}))
                (matrix / 'results.json').write_text(json.dumps({'cases': [{'log': str(log)}]}))
                summary = dict(build, status='WRITE_ENABLED_NORMAL_28_PASS_RMA_INVENTORY_COMPLETE',
                               normal={'results_json': str(matrix / 'results.json')})
                (out / 'results.json').write_text(json.dumps(summary))
            normal.main = normal_main
            result = a.stages(case, read, write, normal, build)
            self.assertEqual(set(result), {'read', 'write', 'normal'})


if __name__ == '__main__':
    unittest.main()
