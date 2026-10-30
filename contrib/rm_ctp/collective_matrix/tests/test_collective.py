import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import coll_matrix as matrix
import run_collective as runner


def output(case):
    header = matrix.BENCHMARKS[case['benchmark']][1] + ' v7.5.2\n# Datatype: MPI_CHAR.\n'
    return header + '\n'.join('%d 2.0 1.0 3.0 20 Pass' % size for size in case['sizes'])


def config(case):
    return '\n'.join(key + '=' + value for key, value in matrix.settings(case).items())


class CollectiveTests(unittest.TestCase):
    def test_bounded_standard_cases_and_no_alternation_claim(self):
        rows = matrix.cases()
        self.assertEqual(len(rows), 10)
        self.assertEqual(sum(len(row['sizes']) for row in rows), 36)
        self.assertEqual(max(row['ppn'] for row in rows), 4)
        self.assertEqual(max(max(row['sizes']) for row in rows), 65536)
        self.assertEqual(sum(row['mode'] == 'rc' for row in rows), 2)
        self.assertTrue(all(row['ppn'] == 1 and row['sizes'] == (8,)
                            for row in rows if row['mode'] == 'rc'))
        for row in rows:
            args = matrix.arguments(row)
            self.assertEqual(args[-2:], ['--validation', '--full'])
            self.assertNotIn('--window-size', args)
        self.assertIn('NOT_COVERED', runner.plan()['size_alternation'])
        self.assertFalse(runner.plan()['performance_accepted'])

    def test_reject_unknown_size_order_scale_and_iterations(self):
        original = matrix.cases()[4]
        for key, value in (('sizes', (8, 65536, 8)), ('ppn', 20), ('mode', 'rc'),
                           ('threshold', 8192), ('benchmark', 'osu_bw')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                matrix.arguments(dict(original, **{key: value}))
        with self.assertRaises(ValueError):
            matrix.arguments(original, 100, 10)

    def test_parse_all_benchmarks_sizes_and_full_statistics(self):
        for row in matrix.cases():
            parsed = matrix.parse_output(output(row), row, 20)
            self.assertEqual([value['size_bytes'] for value in parsed['rows']], list(row['sizes']))
            self.assertFalse(parsed['performance_accepted'])
            self.assertTrue(parsed['validation_pass'])

    def test_reject_corruption_duplicates_fragments_and_missing_pass(self):
        row = matrix.cases()[4]
        good = output(row)
        for bad in (good.replace('16 2.0 1.0 3.0 20 Pass\n', ''),
                    good + '\n8 2.0 1.0 3.0 20 Pass',
                    good + '\n65536 data_len 4077 offset 32616 last: no',
                    good.replace('16 2.0', '16 nan'),
                    good.replace('1.0 3.0', '3.0 1.0'),
                    good.replace('1.0 3.0', '1.0 inf'),
                    good.replace('20 Pass', '19 Pass'),
                    good.replace('20 Pass', '20 Fail'),
                    good.replace('20 Pass', '20 Pass EXTRA'),
                    good.replace('MPI_CHAR', 'MPI_INT'),
                    good.replace('7.5.2', '7.5.1'),
                    good.replace('16 2.0 1.0 3.0 20 Pass\n32 2.0 1.0 3.0 20 Pass',
                                 '32 2.0 1.0 3.0 20 Pass\n16 2.0 1.0 3.0 20 Pass'),
                    good + '\nUCX ERROR transport error'):
            with self.subTest(bad=bad[-90:]), self.assertRaises(ValueError):
                matrix.parse_output(bad, row, 20)

    def test_allgather_cannot_be_parsed_as_alltoall(self):
        row = matrix.cases()[2]
        with self.assertRaises(ValueError):
            matrix.parse_output(output(row).replace('Allgather', 'All-to-All'), row, 20)

    def test_profile_and_registry_restore_after_failure(self):
        original = object(), object(), lambda *args: {'UCX_LOG_FILE': '/stale', 'KEEP': 'value'}
        osu = SimpleNamespace(arguments=original[0], parse_output=original[1], environment=original[2])
        case = matrix.cases()[4]
        with self.assertRaises(RuntimeError):
            with matrix.profile(osu, case, '/tmp/fixed', 4, runner.log_pattern):
                env = osu.environment()
                self.assertEqual(env['KEEP'], 'value')
                self.assertTrue(env['UCX_LOG_FILE'].endswith('/ucx_%h_%p.log'))
                self.assertNotIn('/stale', env['UCX_LOG_FILE'])
                self.assertEqual(env['UCX_LOG_LEVEL'], 'info')
                self.assertEqual(osu.arguments(case['benchmark'], 20, 2, True), matrix.arguments(case))
                self.assertTrue(osu.parse_output(output(case), case['benchmark'], True, 20)['validation_pass'])
                with self.assertRaises(ValueError):
                    osu.arguments(case['benchmark'], 20, 2, False)
                raise RuntimeError('test unwind')
        self.assertEqual((osu.arguments, osu.parse_output, osu.environment), original)
        previous = runner.osu.BENCHMARKS
        with self.assertRaises(RuntimeError):
            with runner.registry():
                self.assertEqual(set(runner.osu.BENCHMARKS), set(matrix.BENCHMARKS))
                raise RuntimeError('test registry unwind')
        self.assertIs(runner.osu.BENCHMARKS, previous)

    def test_effective_config_requires_exact_scheme_threshold_and_logs(self):
        for row in matrix.cases():
            matrix.check_config(config(row), row, runner.candidate.bytes_value)
            for old, new in (('UCX_RNDV_SCHEME=am', 'UCX_RNDV_SCHEME=auto'),
                             ('UCX_LOG_LEVEL=info', 'UCX_LOG_LEVEL=req'),
                             ('UCX_ZCOPY_THRESH=inf', 'UCX_ZCOPY_THRESH=8192')):
                with self.assertRaises(ValueError):
                    matrix.check_config(config(row).replace(old, new), row, runner.candidate.bytes_value)
        row = matrix.cases()[4]
        matrix.check_config(config(row).replace('16384', '16K'), row, runner.candidate.bytes_value)
        with self.assertRaises(ValueError):
            matrix.check_config(config(row).replace('16384', '8192'), row, runner.candidate.bytes_value)

    def finish_fixture(self, temporary):
        out = Path(temporary)
        (out / 'trial').mkdir()
        case = matrix.cases()[4]
        for host in ('node89', 'node97'):
            (out / 'trial' / (host + '_ucx_config.txt')).write_text(config(case))
        return out, {'name': 'trial', 'passed': True}, case

    def test_finish_does_not_override_a_data_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            out, row, case = self.finish_fixture(temporary)
            row.update(passed=False, error='actual OSU validation failed')
            with patch.object(runner.window, 'finish_trial'), patch.object(runner, 'collect') as collect:
                runner.finish(out, row, case, {}, '/probe.so')
                collect.assert_not_called()
            self.assertFalse(json.loads((out / 'trial/trial.json').read_text())['passed'])

    def test_finish_fails_on_missing_pid_log_or_logged_runtime_error(self):
        for error in (FileNotFoundError('actual PID log'), ValueError('UCX ERROR in rank6')):
            with tempfile.TemporaryDirectory() as temporary:
                out, row, case = self.finish_fixture(temporary)
                with patch.object(runner.window, 'finish_trial'), patch.object(runner, 'collect', side_effect=error):
                    runner.finish(out, row, case, {}, '/probe.so')
                self.assertFalse(row['passed'])
                self.assertIn(str(error), row['error'])

    def test_finish_requires_all_rank_logs_and_never_claims_request_path(self):
        for count, passed in ((7, False), (8, True)):
            with tempfile.TemporaryDirectory() as temporary:
                out, row, case = self.finish_fixture(temporary)
                with patch.object(runner.window, 'finish_trial'), \
                        patch.object(runner, 'collect', return_value=('', [{}] * count)):
                    runner.finish(out, row, case, {}, '/probe.so')
                self.assertEqual(row['passed'], passed)
                self.assertEqual(row['actual_request_path_proof'], 'NOT_CLAIMED')

    def test_new_allgather_cli_full_option_is_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            for name in matrix.BENCHMARKS:
                (out / (name + '_help.txt')).write_text('--full --message-size --iterations --warmup --validation')
            runner.check_help(out)
            (out / 'osu_allgather_help.txt').write_text('--message-size --iterations --warmup --validation')
            with self.assertRaises(ValueError):
                runner.check_help(out)

    def test_default_plan_never_calls_node_helpers(self):
        with patch.object(sys, 'argv', ['run_collective.py']), \
                patch.object(runner.candidate, 'validate_build') as validate, \
                patch.object(runner.osu, 'preflight') as preflight, \
                patch.object(runner.osu, 'one_trial') as trial, contextlib.redirect_stdout(io.StringIO()) as output:
            runner.main()
            plan = json.loads(output.getvalue())
            validate.assert_not_called()
            preflight.assert_not_called()
            trial.assert_not_called()
            self.assertEqual(plan['product_commit'], matrix.PRODUCT_COMMIT)
            self.assertFalse(plan['rebuild_required'])
            self.assertFalse(plan['full_B_acceptance'])


if __name__ == '__main__':
    unittest.main()
