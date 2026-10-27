"""Synthetic standard-OMB text/accounting fixtures, never hardware results."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import analysis
import run as runner


class CompareTests(unittest.TestCase):
    def test_standard_multi_and_collective_rows_are_not_mixed(self):
        multi = '# OSU MPI Multi Latency Test v7.5.2\n# Size Latency(us)\n8 2.23\n'
        self.assertEqual(analysis.parse_output(multi, 'osu_multi_lat', False, 5000)['latency_us'], 2.23)
        checked = multi.replace('8 2.23', '8 2.23 Pass')
        self.assertTrue(analysis.parse_output(checked, 'osu_multi_lat', True, 1000)['validation_pass'])
        coll = '# OSU MPI All-to-All Personalized Exchange Latency Test v7.5.2\n8 4.23 4.10 4.50 5000\n'
        self.assertEqual(analysis.parse_output(coll, 'osu_alltoall', False, 5000)['rank_max_us'], 4.50)
        with self.assertRaisesRegex(ValueError, 'header'):
            analysis.parse_output(multi, 'osu_alltoall', False, 5000)
        for bad in (multi.replace('7.5.2', '7.5.20'), multi + '8 3.0\n',
                    multi.replace('2.23', 'nan'), multi + 'UCX ERROR broken\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                analysis.parse_output(bad, 'osu_multi_lat', False, 5000)
        with self.assertRaisesRegex(ValueError, 'column'):
            analysis.parse_output(multi, 'osu_multi_lat', True, 5000)
        with self.assertRaisesRegex(ValueError, 'iterations'):
            analysis.parse_output(coll, 'osu_alltoall', False, 1000)

    def test_fixed_mca_preserves_known_launcher_and_places_one_pml_coll_selection(self):
        command = ['/hmpi/mpirun', '--prefix', '/hmpi', '--mca', 'pml', 'ucx',
                   '--mca', 'coll', '^hcoll,ucc', '--mca', 'btl', 'self,tcp',
                   '-np', '40', '--map-by', 'ppr:20:node', '/usr/bin/python3', '/rank', '/config']
        result = runner.fixed_mca(command)
        self.assertEqual(result.count('coll'), 1)
        self.assertEqual(result.count('pml'), 1)
        self.assertIn('tuned,basic,libnbc,self', result)
        self.assertIn('self,tcp', result)
        self.assertIn('ppr:20:node', result)
        self.assertNotIn('^hcoll,ucc', result)

    def test_shared_observer_counts_actual_process_peak_and_still_rejects_leaks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for rank in (0, 1):
                rows = [{'event': 'start', 'rank': rank}]
                for mode, pointer, context in ((1, 'rm_a', 'a'), (1, 'rm_b', 'b'), (4, 'aux', 'um')):
                    rows.append({'event': 'acquire', 'kind': 'jetty', 'mode': mode, 'rc': 0,
                                 'ptr': pointer, 'ctx': context, 'depth': 64})
                for pointer in ('rm_a', 'rm_b', 'aux'):
                    rows.append({'event': 'release', 'kind': 'jetty', 'rc': 0, 'ptr': pointer})
                (root / ('rank%d_pid%d.jsonl' % (rank, rank + 100))).write_text('\n'.join(map(json.dumps, rows)))
            result = analysis.inspect_jettys(root, 1, 1, {0: 100, 1: 101}, 64)
            self.assertEqual(result[0]['peak_per_process'], 2)
            self.assertEqual(result[0]['peak_per_context'], {'a': 1, 'b': 1})
            path = root / 'rank0_pid100.jsonl'
            rows = path.read_text().splitlines()
            path.write_text('\n'.join(rows[:-2] + rows[-1:]))
            with self.assertRaisesRegex(ValueError, 'leak'):
                analysis.inspect_jettys(root, 1, 1, {0: 100, 1: 101}, 64)

    def samples(self):
        rows = []
        for mode in ('rc', 'rm'):
            for phase, repeat in [('validation', 0), ('resources', 0)] + [('timing', i) for i in range(3)]:
                validation = phase == 'validation'
                rows.append({'name': mode + phase + str(repeat), 'benchmark': 'osu_multi_lat',
                    'ppn': 1, 'mode': mode, 'phase': phase, 'repeat': repeat, 'passed': True,
                    'configuration': {'arguments': runner.arguments('osu_multi_lat', 1000, 100, validation)},
                    'output': {'latency_us': (2.0 if mode == 'rc' else 3.0) + repeat,
                               'validation_pass': validation, 'validation_enabled': validation},
                    'rank_evidence': {'before_finalize_rss_sum_mib': 100.0 + repeat},
                    'jettys': {0: {'peak_per_process': 1, 'peak_per_context': {'a': 1}}}})
        return rows

    def test_tables_preserve_regressions_and_reject_incomplete_or_mixed_resource_arguments(self):
        rows = self.samples()
        result = analysis.compare(rows, 'osu_multi_lat', 1, 3)
        self.assertLess(result['rm_latency_reduction_pct'], 0)
        self.assertEqual(result['modes']['rm']['latency_us']['samples'], [3, 4, 5])
        with tempfile.TemporaryDirectory() as temporary:
            analysis.write_tables(Path(temporary), {'comparisons': [result], 'status': 'CPU_FIXTURE_ONLY'})
            self.assertIn('CPU_FIXTURE_ONLY', (Path(temporary) / 'comparison.md').read_text())
            self.assertIn('-33.33', (Path(temporary) / 'comparison.md').read_text())
            self.assertTrue((Path(temporary) / 'comparison.csv').read_bytes().startswith(b'\xef\xbb\xbf'))
        with self.assertRaises(ValueError):
            analysis.compare(rows[:-1], 'osu_multi_lat', 1, 3)
        rows[0]['passed'] = False
        with self.assertRaises(ValueError):
            analysis.compare(rows, 'osu_multi_lat', 1, 3)
        rows = self.samples()
        next(r for r in rows if r['phase'] == 'resources')['configuration']['arguments'] += ['--validation']
        with self.assertRaisesRegex(ValueError, 'arguments'):
            analysis.compare(rows, 'osu_multi_lat', 1, 3)

    def test_arguments_exactly_one_message_and_separate_correctness(self):
        for benchmark in analysis.BENCHMARKS:
            timing = runner.arguments(benchmark, 5000, 1000, False)
            resource = runner.arguments(benchmark, 5000, 1000, False)
            validation = runner.arguments(benchmark, 1000, 100, True)
            self.assertEqual(timing, resource)
            self.assertNotIn('--validation', timing)
            self.assertIn('--validation', validation)
            self.assertEqual(timing[timing.index('--message-size') + 1], '8:8')

    def test_mpi_dependency_requires_a_real_resolved_library(self):
        with self.assertRaises(ValueError):
            runner.mpi_dependency('libmpi.so.40 => not found')
        self.assertEqual(str(runner.mpi_dependency('libmpi.so.40 => /mpi/libmpi.so.40 (0x1)')), '/mpi/libmpi.so.40')
