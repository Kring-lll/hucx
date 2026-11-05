"""CPU-only scale schedule, fail-closed evidence and controller tests; no MPI."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bootstrap
import policy
import run_compare
import run_scale_compare as runner
import scale_policy as scale
from test_compare import fixture_row


class ScaleTests(unittest.TestCase):
    def test_plan_exact_255_and_scope(self):
        rows = list(scale.tasks())
        self.assertEqual(scale.plan()['total_mpi'], 255)
        self.assertEqual([sum(r[4] == p for r in rows) for p in ('validation', 'timing', 'resources')], [36, 180, 36])
        self.assertEqual(len(rows), 252)
        self.assertTrue(all(r[2] == 1 for r in rows if r[1] != 'osu_alltoall'))
        self.assertTrue(all(r[2] <= 24 for r in rows if r[6] == 'rc'))
        self.assertEqual(max(r[2] for r in rows), 160)
        self.assertEqual(len(scale.plan()['rc_omissions']), 6)
        self.assertEqual(scale.plan(False)['total_mpi'], 39)
        for group in range(len(scale.GROUPS)):
            modes = scale.profiles(scale.GROUPS[group][1])
            ordered = [r[6] for r in rows if r[0] == group and r[4] == 'timing']
            for repeat in range(5):
                shift = repeat % len(modes)
                self.assertEqual(ordered[repeat*len(modes):(repeat+1)*len(modes)], list(modes[shift:]+modes[:shift]))

    def test_latency_real_header_validation_and_bad_rows(self):
        text = '# OSU MPI Latency Test v7.5.2\n8 2.03 Pass\n'
        self.assertEqual(scale.parse_output(text, 'osu_latency', True, 100)['value'], 2.03)
        for bad in (text.replace('Pass', 'Fail'), text.replace('2.03', 'nan'), text + '8 2.1 Pass\n', text.replace('7.5.2', '7.6')):
            with self.assertRaises(ValueError):
                scale.parse_output(bad, 'osu_latency', True, 100)
        self.assertIn('--validation', scale.arguments('osu_latency', 100, 10, True))

    def inventory(self):
        return [{'hostname': h, 'available_cores': 192, 'mem_available_kib': 64*1024**2,
                 'nofile_soft': 4096, 'nproc_soft': -1} for h in ('node89', 'node97')]

    def disks(self):
        return [{'hostname': h, 'free_bytes': 64*1024**3} for h in ('node89', 'node97')]

    def test_capacity_no_oversubscribe_and_staging_floors(self):
        self.assertTrue(scale.capacity(self.inventory(), 160, self.disks())['passed'])
        for key, value in (('available_cores', 159), ('mem_available_kib', 1), ('nofile_soft', 512), ('nproc_soft', 20)):
            rows = self.inventory()
            rows[1][key] = value
            self.assertFalse(scale.capacity(rows, 160, self.disks())['passed'])
        disks = self.disks()
        disks[0]['free_bytes'] = 1024
        self.assertFalse(scale.capacity(self.inventory(), 1, disks)['passed'])
        with self.assertRaises(ValueError):
            scale.capacity(self.inventory(), 25, self.disks())

    def group_rows(self, group):
        rows = []
        benchmark, ppn, size = scale.GROUPS[group]
        for item in scale.tasks():
            if item[0] != group:
                continue
            _, _, _, _, phase, repeat, profile = item
            row = fixture_row(profile, phase, repeat, 20 if phase == 'timing' and repeat == 2 else 4)
            row.update(group=group, benchmark=benchmark, ppn=ppn, message_bytes=size)
            row['jettys'] = {i: {'peak_per_process': 1} for i in range(2*ppn)}
            rows.append(row)
        return rows

    def test_high_ppn_summary_preserves_outlier_and_rc_omission(self):
        group = len(scale.GROUPS)-1
        rows = self.group_rows(group)
        value = scale.summarize(rows, group)
        self.assertEqual(set(value['profiles']), {'rm', 'um'})
        self.assertEqual(value['rc_status'], 'NOT_PLANNED_RC_LIMIT_24')
        self.assertEqual(value['profiles']['rm']['samples'], [4, 4, 20, 4, 4])
        self.assertNotIn('rc', value['rm_improvement_pct'])
        with self.assertRaises(ValueError):
            scale.summarize(rows[:-1], group)
        rows[0]['ppn'] = 128
        with self.assertRaises(ValueError):
            scale.summarize(rows, group)

    def test_table_does_not_create_rc_zeros(self):
        group = len(scale.GROUPS)-1
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            report = {'comparisons': [scale.summarize(self.group_rows(group), group)],
                      'plan': scale.plan(), 'status': 'CPU_FIXTURE'}
            scale.write_tables(out, report)
            self.assertIn('NOT_PLANNED', (out/'comparison.md').read_text())
            self.assertIn('NOT_PLANNED_RC_LIMIT_24', (out/'comparison.csv').read_text(encoding='utf-8-sig'))

    def controller(self, fail_ppn=None):
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)/'case'
            case.mkdir()
            source = case/'source/entry.py'
            source.parent.mkdir()
            source.write_text('CPU_ORIGINAL')
            lib = case/'install/lib/ucx/libuct_ub.so.0.0.0'
            lib.parent.mkdir(parents=True)
            lib.write_bytes(b'CPU_MODULE')
            helper = case/'helpers'
            helper.mkdir()
            for name in ('osu_rank.py', 'rank_exec.py'):
                (helper/name).write_text('# CPU_ONLY')
            guard = SimpleNamespace(CASE=case, COMMIT='CPU', MODULE_SHA=bootstrap.digest(lib),
                                    RELATIVE_ENTRY='entry.py', ENTRY_SHA=bootstrap.digest(source))
            inventory_calls = []
            def inventory():
                inventory_calls.append(1)
                return self.inventory()
            base = SimpleNamespace(parsed_config=None, inventory=inventory, HOSTS=['a','b'],
                write_json=lambda path, value: Path(path).write_text(json.dumps(value)),
                ssh=lambda script, *a, **kw: bootstrap.digest(a[0]) if 'sha256sum' in script else '',
                copy_remote=lambda *a: None)
            osu = SimpleNamespace(base=base, DEFAULT_CASE=None, PRODUCT_COMMIT=None, PRODUCT_MODULE=None,
                BENCHMARKS={}, arguments=None, parse_output=None, environment=None, budget=None,
                inspect_jettys=lambda *a: {}, launcher=lambda *a: ['CPU_LAUNCHER'],
                LAB=case, ROOT=helper, A89=helper, saved_spec=lambda *a: (['mpirun'], {}))
            engine = {'osu': osu, 'environment': lambda *a: {}, 'parsed_config': lambda *a: None,
                      'validate_source': lambda *a: {}, 'deploy': lambda *a: None}
            def preflight(out, commands):
                for name in ('osu_bw', 'osu_bibw'):
                    (out/(name+'_help.txt')).write_text('--window-size --buffer-num')
                (out/'libosu_identity.so').write_bytes(b'CPU_PROBE')
                return {'inventory': base.inventory()}
            osu.preflight = preflight
            observed = []
            def run_trial(engine, out, commands, evidence, item, index, capture, trial_args):
                base.inventory()
                group, benchmark, ppn, size, phase, repeat, profile = item
                observed.append(item)
                rows = self.group_rows(group) if group >= 0 else []
                row = next((r for r in rows if r['phase'] == phase and r['repeat'] == repeat and r['profile'] == profile),
                           fixture_row(profile, phase))
                row = json.loads(json.dumps(row))
                row.update(name='trial%d' % index, group=group, ppn=ppn, benchmark=benchmark, message_bytes=size)
                row['load_evidence'] = {'node89': {}, 'node97': {}}
                if ppn == fail_ppn:
                    row.update(passed=False, exit=124, error='CPU_SIMULATED_TIMEOUT')
                run = out/row['name']
                run.mkdir()
                base.write_json(run/'trial.json', row)
                return row
            with patch.object(bootstrap, 'load', return_value=(guard, engine, {}, {})), \
                 patch.object(runner, 'repeat_checkpoint', return_value={'cpu_fixture': True}), \
                 patch.object(runner, 'disk_inventory', return_value=self.disks()), \
                 patch.object(runner.os, 'geteuid', return_value=0), \
                 patch.object(runner.socket, 'gethostname', return_value='node89'), \
                 patch.object(sys, 'argv', ['scale', '--run', '--timing']), \
                 patch.object(run_compare, 'run_trial', side_effect=run_trial), \
                 patch('zipfile.ZipFile') as bundle, redirect_stdout(io.StringIO()):
                bundle.return_value.__enter__.return_value.read.return_value = b'# CPU_SNAPSHOT'
                if fail_ppn:
                    with self.assertRaisesRegex(RuntimeError, 'CPU_SIMULATED_TIMEOUT'):
                        runner.main()
                else:
                    runner.main()
            self.assertIs(base.inventory, inventory)
            out = next(case.glob('um_osu_scale_*'))
            report = json.loads((out/'results.json').read_text())
            for row in report['gates'] + report['trials']:
                self.assertEqual(json.loads((out/row['name']/'trial.json').read_text()), row)
            self.assertEqual(source.read_text(), 'CPU_ORIGINAL')
            self.assertEqual(lib.read_bytes(), b'CPU_MODULE')
            self.assertGreater(len(inventory_calls), 1)
            return report, observed

    def test_complete_255_controller_and_320_rank_resource(self):
        report, observed = self.controller()
        self.assertEqual(report['status'], 'SELECTED_P2P_AND_ALLTOALL_SCALE_COMPLETE_REVIEW_REQUIRED')
        self.assertEqual(len(report['gates']) + len(report['trials']), 255)
        self.assertEqual(len(report['comparisons']), 14)
        self.assertEqual(len(report['comparisons'][-1]['profiles']['rm']['samples']), 5)
        self.assertTrue(all(r[2] == 1 for r in observed if r[1] != 'osu_alltoall'))
        self.assertTrue(all(r[2] <= 24 for r in observed if r[6] == 'rc'))

    def test_fail_stops_higher_levels_preserves_partial(self):
        report, observed = self.controller(fail_ppn=32)
        self.assertEqual(report['status'], 'STOPPED_INCOMPLETE')
        self.assertEqual(max(r[2] for r in observed), 32)
        self.assertFalse(report['trials'][-1]['passed'])
        self.assertEqual(report['active_task']['ppn'], 32)
        self.assertEqual(len(report['comparisons']), 8)


if __name__ == '__main__':
    unittest.main()
