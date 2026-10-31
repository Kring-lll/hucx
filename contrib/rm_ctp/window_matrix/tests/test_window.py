import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import subprocess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import matrix
import run_window as runner


def output(case):
    lines = [matrix.BENCHMARKS[case['benchmark']][1] + ' v7.5.2',
             '# Datatype: MPI_CHAR.']
    for size in case['sizes']:
        suffix = ' 1.0 3.0 100 Pass' if case['benchmark'] == 'osu_alltoall' else ' Pass'
        lines.append('%d 2.0%s' % (size, suffix))
    return '\n'.join(lines)


class WindowTests(unittest.TestCase):
    def test_bounded_matrix_and_exact_real_osu_options(self):
        rows = matrix.cases()
        self.assertEqual(len(rows), 10)
        self.assertEqual(sum(len(row['sizes']) for row in rows), 54)
        self.assertEqual([row['mode'] for row in rows[:2]], ['rc', 'rc'])
        self.assertEqual(max(row['ppn'] for row in rows), 4)
        for row in rows:
            args = matrix.arguments(row, 100, 10)
            self.assertIn('--validation', args)
            if row['benchmark'] in ('osu_bw', 'osu_bibw'):
                self.assertEqual(args[-4:], ['--window-size', str(row['window']),
                                            '--buffer-num', 'multiple'])
                self.assertEqual(row['ppn'], 1)
            if len(row['sizes']) > 1:
                self.assertIn('8:16384:2', args)

    def test_invalid_sizes_windows_and_topologies_are_rejected(self):
        original = matrix.cases()[2]
        for key, value in (('sizes', (8, 16385)), ('sizes', (8, 4096, 8)),
                           ('window', 192), ('ppn', 4)):
            row = dict(original, **{key: value})
            with self.assertRaises(ValueError):
                matrix.arguments(row, 100, 10)
        with self.assertRaises(ValueError):
            matrix.arguments(original, 5000, 1000)

    def test_every_size_requires_exact_order_and_pass(self):
        for row in matrix.cases():
            parsed = matrix.parse_output(output(row), row, 100)
            self.assertEqual([v['size_bytes'] for v in parsed['rows']], list(row['sizes']))
            self.assertFalse(parsed['performance_accepted'])
        row = matrix.cases()[2]
        good = output(row)
        for bad in (good.replace('16384 2.0 Pass', ''), good + '\n8 2.0 Pass',
                    good.replace('16 2.0 Pass', '16 2.0 Fail'),
                    good.replace('16 2.0 Pass', '16 nan Pass'),
                    good.replace('16 2.0 Pass', '16 -1.0 Pass'),
                    good.replace('MPI_CHAR', 'MPI_INT'),
                    good.replace('7.5.2', '7.5.1'),
                    good + '\nUCX ERROR failed',
                    good.replace('16 2.0 Pass\n32 2.0 Pass', '32 2.0 Pass\n16 2.0 Pass')):
            with self.subTest(bad=bad[-80:]), self.assertRaises(ValueError):
                matrix.parse_output(bad, row, 100)

    def test_alltoall_checks_full_columns_and_iterations(self):
        row = matrix.cases()[-1]
        for text in (output(row).replace('100 Pass', '99 Pass'),
                     output(row).replace('1.0 3.0', '3.0 1.0'),
                     output(row).replace('1.0 3.0', '1.0 nan'),
                     output(row).replace('100 Pass', '100 Pass EXTRA')):
            with self.assertRaises(ValueError):
                matrix.parse_output(text, row, 100)

    def test_adapter_restores_parent_and_does_not_modify_osu_binary(self):
        old_arguments = object()
        old_parser = object()
        osu = SimpleNamespace(arguments=old_arguments, parse_output=old_parser)
        case = matrix.cases()[2]
        with self.assertRaises(RuntimeError):
            with matrix.adapt_trial(osu, case):
                self.assertEqual(osu.arguments('osu_bw', 100, 10, True),
                                 matrix.arguments(case, 100, 10))
                self.assertTrue(osu.parse_output(output(case), 'osu_bw', True, 100)['validation_pass'])
                with self.assertRaises(ValueError):
                    osu.arguments('osu_bw', 100, 10, False)
                raise RuntimeError('injected failure')
        self.assertIs(osu.arguments, old_arguments)
        self.assertIs(osu.parse_output, old_parser)

    def test_registry_and_observer_environment_unwind_after_exception(self):
        old_registry = runner.osu.BENCHMARKS
        old_environment = runner.osu.environment
        with self.assertRaises(RuntimeError):
            with runner.benchmark_registry(), runner.traced_environment('/probe.so'):
                self.assertIn('osu_bw', runner.osu.BENCHMARKS)
                raise RuntimeError('injected failure')
        self.assertIs(runner.osu.BENCHMARKS, old_registry)
        self.assertIs(runner.osu.environment, old_environment)

    def test_no_claim_of_size_alternation_or_full_feature_acceptance(self):
        plan = runner.plan()
        self.assertEqual(plan['size_alternation'], 'NOT_COVERED')
        self.assertFalse(plan['performance_claim'])
        self.assertFalse(plan['transport_changed'])
        self.assertEqual(plan['product_commit'], matrix.PRODUCT_COMMIT)

    def test_final_gate_persists_missing_observer_as_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            run = out / 'trial'
            run.mkdir()
            row = {'name': 'trial', 'passed': True}
            with patch.object(runner.candidate, 'check_trial_default'), \
                    patch.object(runner.wr, 'fetch_peer_summaries', side_effect=FileNotFoundError('rank7')):
                runner.finish_trial(out, row, matrix.cases()[-1], Path('/probe.so'))
            self.assertFalse(row['passed'])
            self.assertIn('rank7', json.loads((run / 'trial.json').read_text())['error'])

    def test_rc_gate_does_not_claim_send_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            (out / 'trial').mkdir()
            row = {'name': 'trial', 'passed': True}
            with patch.object(runner.candidate, 'check_trial_default'), \
                    patch.object(runner.wr, 'fetch_peer_summaries') as fetch:
                runner.finish_trial(out, row, matrix.cases()[0], Path('/probe.so'))
            self.assertTrue(row['passed'])
            self.assertNotIn('wr_probe_passed', row)
            fetch.assert_not_called()

    def test_required_window_cli_must_be_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            for name in ('osu_bw', 'osu_bibw'):
                (out / (name + '_help.txt')).write_text('--window-size --buffer-num')
            runner.check_help(out)
            (out / 'osu_bibw_help.txt').write_text('--window-size')
            with self.assertRaises(ValueError):
                runner.check_help(out)


    def test_helper_directory_creates_missing_peer_parents_with_real_shell(self):
        class DirectoryReady(Exception):
            pass
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'missing parent' / 'remaining_am'
            def remote(script, *args, **kwargs):
                subprocess.run(['bash','-s','--',*map(str,args)],input=script,
                               text=True,capture_output=True,check=True)
                raise DirectoryReady()
            with patch.object(runner.osu.base,'ssh',side_effect=remote):
                with self.assertRaises(DirectoryReady):
                    runner.prepare_helpers(out,{})
            self.assertTrue(out.is_dir())

    def test_helper_directory_refuses_existing_target_with_real_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'existing'
            out.mkdir()
            marker = out / 'keep.txt'
            marker.write_text('keep')
            def remote(script, *args, **kwargs):
                subprocess.run(['bash','-s','--',*map(str,args)],input=script,
                               text=True,capture_output=True,check=True)
            with patch.object(runner.osu.base,'ssh',side_effect=remote):
                with self.assertRaises(subprocess.CalledProcessError):
                    runner.prepare_helpers(out,{})
            self.assertEqual(marker.read_text(),'keep')


if __name__ == '__main__':
    unittest.main()
