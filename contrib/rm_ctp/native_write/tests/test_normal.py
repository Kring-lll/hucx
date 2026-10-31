import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('write_normal_tests', ROOT / 'normal_validation.py')
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)

class NormalTests(unittest.TestCase):
    def test_scoped_configuration_preserves_original_and_restores(self):
        def values(mode, debug=False):
            return {'ORIGINAL': mode, 'DEBUG': debug}
        legacy = types.SimpleNamespace(values=values)
        with n.enabled_normal(legacy):
            result = legacy.values('iface', True)
            self.assertEqual(result, {'ORIGINAL': 'iface', 'DEBUG': True, 'UCX_RM_URMA_NATIVE_WRITE': 'y'})
        self.assertIs(legacy.values, values)

    def test_configuration_restored_after_failure(self):
        original = lambda *a, **k: {}
        legacy = types.SimpleNamespace(values=original)
        with self.assertRaises(RuntimeError):
            with n.enabled_normal(legacy):
                raise RuntimeError('expected')
        self.assertIs(legacy.values, original)

    def proof(self, root):
        out = root / n.PROOF
        out.mkdir()
        result = {'status': 'DIRECT_NATIVE_WRITE_DATA_MAPS_REGISTRATION_RELEASE_PASS',
                  'source_commit': n.COMMIT, 'module_sha256': n.MODULE,
                  'sender_exit': 0, 'receiver_exit': 0, 'GET_enabled': False}
        (out / 'results.json').write_text(json.dumps(result))
        for role in ('sender', 'receiver'):
            rows = ['NATIVE_WRITE_ROUND=%d BYTES=%d ROLE=%s DATA=PASS' % (i, (8,64,4096)[i % 3], role) for i in range(6)]
            (out / (role + '.log')).write_text('\n'.join(rows) + '\nNATIVE_WRITE_PASS ROUNDS=6 SIZE=8/64/4096\n')
        return out

    def test_native_proof_read_only_and_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = self.proof(root)
            before = (out / 'results.json').read_bytes()
            self.assertIn('sha256', n.prior_write(root))
            self.assertEqual(before, (out / 'results.json').read_bytes())
            (out / 'sender.log').write_text((out / 'sender.log').read_text().replace('ROUND=1', 'ROUND=0'))
            with self.assertRaises(ValueError):
                n.prior_write(root)

    def test_normal_matrix_requires_flag_in_all_real_command_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            case = Path(folder)
            out = case / 'iface_accept_test'
            out.mkdir()
            rows = []
            for i in range(28):
                log = out / (str(i) + '.log')
                log.with_suffix('.json').write_text(json.dumps({'environment': {'UCX_RM_URMA_NATIVE_WRITE': 'y'}}))
                rows.append({'name': str(i), 'passed': True, 'log': str(log)})
            report = {'source_commit': n.COMMIT, 'module_sha256': n.MODULE,
                      'functional_acceptance': True, 'cases': rows, 'median_8B_us': {}}
            path = out / 'results.json'
            path.write_text(json.dumps(report))
            self.assertEqual(n.check_normal_receipts({'results_json': str(path)}, case)['cases'], 28)
            Path(rows[0]['log']).with_suffix('.json').write_text('{"environment":{}}')
            with self.assertRaises(ValueError):
                n.check_normal_receipts({'results_json': str(path)}, case)

    def test_inventory_remote_function_can_be_extracted_from_overlay(self):
        import ast
        source = (ROOT / 'normal_validation.py').read_text()
        function = next(v for v in ast.parse(source).body if isinstance(v, ast.FunctionDef) and v.name == 'local_inventory')
        code = 'import os,json,hashlib,subprocess\nfrom pathlib import Path\n' + ast.get_source_segment(source, function)
        compile(code, '<remote-inventory>', 'exec')
        namespace = {}
        exec(code, namespace)
        self.assertTrue(callable(namespace['local_inventory']))

if __name__ == '__main__':
    unittest.main()
