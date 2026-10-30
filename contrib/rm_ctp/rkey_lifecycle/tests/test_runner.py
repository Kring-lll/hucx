import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / 'run_candidate.py'
spec = importlib.util.spec_from_file_location('rkey_candidate_controller', ENTRY)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RunnerTests(unittest.TestCase):
    def test_plan_does_not_touch_node_and_does_not_enable_rma(self):
        with patch.object(sys, 'argv', [str(ENTRY)]), \
                patch.object(r.candidate, 'validate_build') as validate, \
                patch.object(r.candidate, 'deploy') as deploy, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            r.main()
        validate.assert_not_called()
        deploy.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['max_ppn'], 4)
        self.assertFalse(plan['native_RMA_enabled'])
        self.assertEqual(plan['reference_product'], r.REFERENCE_COMMIT)

    def matrix(self, failed):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root = Path(temporary)
            case = root / 'candidate'
            build = {'source_commit': 'a' * 40, 'module_sha256': 'b' * 64}
            old = r.osu.DEFAULT_CASE, r.osu.PRODUCT_COMMIT, r.osu.PRODUCT_MODULE
            calls = []
            def trial(out, commands, evidence, benchmark, mode, ppn, phase, index, args, **kwargs):
                self.assertEqual(r.osu.DEFAULT_CASE, case)
                self.assertEqual(r.osu.PRODUCT_COMMIT, build['source_commit'])
                self.assertLessEqual(ppn, 4)
                calls.append(index)
                return {'name': 'trial%d' % index, 'passed': not failed,
                        'error': 'injected' if failed else ''}
            for name in ('validate_package',):
                stack.enter_context(patch.object(r.osu, name))
            stack.enter_context(patch.object(r.osu, 'saved_spec', return_value=([], {})))
            stack.enter_context(patch.object(r.osu, 'preflight', return_value={
                'inventory': [{'available_cores': 4}, {'available_cores': 4}]}))
            stack.enter_context(patch.object(r.osu, 'one_trial', side_effect=trial))
            stack.enter_context(patch.object(r.collective, 'check_help'))
            stack.enter_context(patch.object(r.collective.window, 'prepare_helpers', return_value=root / 'probe.so'))
            stack.enter_context(patch.object(r.collective, 'finish'))
            with contextlib.redirect_stdout(io.StringIO()):
                if failed:
                    with self.assertRaisesRegex(ValueError, 'injected'):
                        r.collective_matrix(root / 'matrix', case, build)
                else:
                    r.collective_matrix(root / 'matrix', case, build)
            self.assertEqual(old, (r.osu.DEFAULT_CASE, r.osu.PRODUCT_COMMIT, r.osu.PRODUCT_MODULE))
            result = json.loads((root / 'matrix/results.json').read_text())
            self.assertEqual(len(result['cases']), len(calls))
            self.assertEqual(result['status'], 'STOPPED_INCOMPLETE' if failed else
                             'SELECTED_COLLECTIVE_NORMAL_MATRIX_PASS')
            return calls

    def test_collective_uses_new_candidate_for_all_ten_tasks(self):
        self.assertEqual(self.matrix(False), list(range(10)))

    def test_collective_failure_keeps_evidence_and_stops_immediately(self):
        self.assertEqual(self.matrix(True), [0])


if __name__ == '__main__':
    unittest.main()
