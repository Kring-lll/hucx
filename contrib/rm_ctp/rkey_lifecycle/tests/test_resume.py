import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / 'resume.py'
spec = importlib.util.spec_from_file_location('rkey_resume_tool', ENTRY)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def fixture(root):
    case = root / 'case'
    normal = case / 'iface_accept_normal/results.json'
    old = case / 'rkey_accept_failed/results.json'
    names = ['新模式加载与8B校验']
    names += ['%s_8B_%d' % (m, n) for m in ('legacy', 'iface', 'rc') for n in range(3)]
    names += ['%s_%s_peers%d' % (m, p, n) for m in ('legacy', 'iface', 'rc')
              for p in ('many', 'burst') for n in (1, 2, 4)]
    report = {'source_commit': r.COMMIT, 'module_sha256': r.MODULE,
              'functional_acceptance': True,
              'cases': [{'name': name, 'passed': True} for name in names]}
    previous = {'source_commit': r.COMMIT, 'module_sha256': r.MODULE,
                'status': 'STOPPED_INCOMPLETE', 'error': "KeyError: 'controller_dir'",
                'normal_regression': {'passed': True, 'cases': 28,
                                      'results_json': str(normal)}}
    normal.parent.mkdir(parents=True)
    old.parent.mkdir()
    normal.write_text(json.dumps(report))
    old.write_text(json.dumps(previous))
    return case, old, normal, report, previous


class ResumeTests(unittest.TestCase):
    def test_accepts_only_exact_passed_normal_matrix_and_hashes_both_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            case, old, source, report, previous = fixture(Path(temp))
            normal, receipt = r.checkpoint(case, old)
            self.assertEqual(normal['cases'], 28)
            self.assertEqual(receipt['source_sha256'], r.digest(old))
            self.assertEqual(receipt['normal_sha256'], r.digest(source))

    def test_rejects_wrong_product_missing_duplicate_or_failed_normal_row(self):
        with tempfile.TemporaryDirectory() as temp:
            case, old, source, report, previous = fixture(Path(temp))
            for defect in ('identity', 'missing', 'duplicate', 'failed', 'functional'):
                bad = json.loads(json.dumps(report))
                if defect == 'identity':
                    bad['module_sha256'] = '0' * 64
                elif defect == 'missing':
                    bad['cases'].pop()
                elif defect == 'duplicate':
                    bad['cases'][-1] = bad['cases'][0]
                elif defect == 'failed':
                    bad['cases'][0]['passed'] = False
                else:
                    bad['functional_acceptance'] = False
                source.write_text(json.dumps(bad))
                with self.subTest(defect=defect), self.assertRaises(ValueError):
                    r.checkpoint(case, old)

    def test_rejects_other_failure_or_later_stage_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            case, old, source, report, previous = fixture(Path(temp))
            for bad in (dict(previous, error='data error'), dict(previous, timing={}),
                        dict(previous, source_commit='0' * 40)):
                old.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    r.checkpoint(case, old)

    def test_resume_handoff_preserves_old_results_and_stops_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            case, old, source, _, _ = fixture(Path(temp))
            normal, receipt = r.checkpoint(case, old)
            old_bytes, source_bytes = old.read_bytes(), source.read_bytes()
            out = case / 'resume'
            out.mkdir()
            calls = []
            def boundary(_, result):
                self.assertEqual(result['controller_dir'], str(out))
                calls.append('boundary')
                raise ValueError('stop here')
            def save(path, value):
                path.write_text(json.dumps(value))
            runner = SimpleNamespace(plan=lambda: {}, candidate=SimpleNamespace(
                deploy=lambda *a: {}, bind_candidate=lambda *a: contextlib.nullcontext(),
                boundary_matrix=boundary), osu=SimpleNamespace(base=SimpleNamespace(write_json=save)))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'stop here'):
                r.run(runner, case, {'source_commit': r.COMMIT, 'module_sha256': r.MODULE}, out, normal, receipt)
            saved = json.loads((out / 'results.json').read_text())
            self.assertEqual(saved['status'], 'STOPPED_INCOMPLETE')
            self.assertEqual(old.read_bytes(), old_bytes)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(calls, ['boundary'])


    def test_resume_runs_remaining_stages_without_calling_normal_regression(self):
        with tempfile.TemporaryDirectory() as temp:
            case, old, source, _, _ = fixture(Path(temp))
            normal, receipt = r.checkpoint(case, old)
            out = case / 'resume'
            out.mkdir()
            calls = []
            def boundary(_, result):
                self.assertEqual(result['controller_dir'], str(out))
                calls.append('boundary')
            def save(path, value):
                path.write_text(json.dumps(value))
            def normal_forbidden(*args):
                self.fail('normal MPI tasks must not run again')
            def timing(*args):
                calls.append('timing')
                return {key: {'median_us': 2.0} for key in ('old_rm', 'new_rm', 'rc')}
            def matrix(*args):
                calls.append('window_and_am')
                return {}
            def collective(*args):
                calls.append('collective')
                return {}
            runner = SimpleNamespace(plan=lambda: {}, candidate=SimpleNamespace(
                deploy=lambda *a: {}, bind_candidate=lambda *a: contextlib.nullcontext(),
                boundary_matrix=boundary, normal_regression=normal_forbidden),
                osu=SimpleNamespace(base=SimpleNamespace(write_json=save)),
                pending=SimpleNamespace(timing=timing, matrix=matrix),
                collective_matrix=collective, REFERENCE_CASE='ref',
                REFERENCE_COMMIT='ref_commit', REFERENCE_MODULE='ref_module')
            with contextlib.redirect_stdout(io.StringIO()):
                result = r.run(runner, case, {'source_commit': r.COMMIT, 'module_sha256': r.MODULE},
                               out, normal, receipt)
            self.assertEqual(calls, ['boundary', 'timing', 'window_and_am', 'collective'])
            self.assertEqual(result['status'], 'RKEY_FIX_AM_REGRESSION_PASS_PERFORMANCE_REVIEW_REQUIRED')


if __name__ == '__main__':
    unittest.main()
