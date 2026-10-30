import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'run_candidate.py'
spec = importlib.util.spec_from_file_location('pending_candidate_runner', PATH)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class CandidateTests(unittest.TestCase):
    def test_default_plan_has_no_node_side_effects(self):
        with patch.object(sys, 'argv', [str(PATH), '/not/a/node/case']), \
                patch.object(p.candidate, 'validate_build') as build, \
                patch.object(p.candidate, 'deploy') as deploy, \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            p.main()
        build.assert_not_called()
        deploy.assert_not_called()
        self.assertIn('"max_ppn": 4', stream.getvalue())

    def test_timing_reference_is_80b_and_restored_after_failure(self):
        previous = p.candidate.OLD_CASE, p.candidate.OLD_COMMIT, p.candidate.OLD_MODULE
        def injected(*args):
            self.assertEqual(p.candidate.OLD_CASE, p.TESTED_CASE)
            self.assertEqual(p.candidate.OLD_MODULE, p.TESTED_MODULE)
            raise RuntimeError('injected')
        with patch.object(p.candidate, 'timing_comparison', side_effect=injected):
            with self.assertRaises(RuntimeError):
                p.timing(Path('/out'), Path('/new'), {})
        self.assertEqual(previous, (p.candidate.OLD_CASE, p.candidate.OLD_COMMIT, p.candidate.OLD_MODULE))

    def run_matrix(self, failed):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root = Path(temporary)
            case = root / 'candidate'
            build = {'source_commit': 'a' * 40, 'module_sha256': 'b' * 64}
            old = p.osu.DEFAULT_CASE, p.osu.PRODUCT_COMMIT, p.osu.PRODUCT_MODULE
            observed = []
            def capabilities(install, env, out):
                self.assertEqual(install, case / 'install')
                for host in ('node89', 'node97'):
                    (out / (host + '_ucx_device.txt')).write_text('# Device: udmac0d1e2:1\n# am_bcopy: <= 4093\n')
                return {}
            def one_trial(out, commands, evidence, benchmark, mode, ppn, phase, index, args, **kwargs):
                self.assertEqual(p.osu.DEFAULT_CASE, case)
                self.assertEqual(p.osu.PRODUCT_COMMIT, build['source_commit'])
                observed.append((benchmark, mode, ppn, args.iterations))
                return {'name': 'trial%d' % index, 'passed': not failed,
                        'error': 'injected' if failed else '', 'send_observations': []}
            for name in ('validate_package',):
                stack.enter_context(patch.object(p.osu, name))
            stack.enter_context(patch.object(p.osu, 'saved_spec', return_value=([], {})))
            stack.enter_context(patch.object(p.osu, 'preflight', return_value={
                'inventory': [{'available_cores': 4}, {'available_cores': 4}]}))
            stack.enter_context(patch.object(p.osu, 'one_trial', side_effect=one_trial))
            stack.enter_context(patch.object(p.candidate, 'default_capabilities', side_effect=capabilities))
            stack.enter_context(patch.object(p.window, 'check_help'))
            stack.enter_context(patch.object(p.window, 'prepare_helpers', return_value=root / 'probe.so'))
            stack.enter_context(patch.object(p.window, 'finish_trial'))
            stack.enter_context(patch.object(p.am, 'finish_protocol'))
            stack.enter_context(patch.object(p.osu, 'environment', return_value={}))
            with contextlib.redirect_stdout(io.StringIO()):
                if failed:
                    with self.assertRaisesRegex(ValueError, 'injected'):
                        p.matrix(root / 'matrix', case, build)
                else:
                    result = p.matrix(root / 'matrix', case, build)
                    self.assertEqual(result['window_tasks'], 10)
                    self.assertEqual(result['am_tasks'], 10)
            self.assertEqual((p.osu.DEFAULT_CASE, p.osu.PRODUCT_COMMIT, p.osu.PRODUCT_MODULE), old)
            return observed

    def test_both_matrices_bind_only_new_candidate_and_keep_original_workloads(self):
        rows = self.run_matrix(False)
        self.assertEqual(len(rows), 20)
        self.assertEqual([r[3] for r in rows], [100] * 10 + [20] * 10)
        self.assertLessEqual(max(r[2] for r in rows), 4)

    def test_failure_stops_before_next_mpi_task_and_restores_binding(self):
        self.assertEqual(len(self.run_matrix(True)), 1)


if __name__ == '__main__':
    unittest.main()
