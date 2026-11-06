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

    def controller(self, fail_ppn=None, rnr=False, rnr_failure=False, other_failure=False):
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
                (helper/name).write_text('# CPU_ONLY\n        shared.main()\n')
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
                if rnr:
                    engine['PROFILE'] = profile
                    row['configuration']['environment'].update(osu.environment({}, 'rm', case/'install', osu.budget(ppn)))
                    for value in row['jettys'].values():
                        value['jfr_rx_depths'] = [int(row['configuration']['environment']['UCX_'+profile.upper()+'_URMA_RX_DEPTH'])]
                if ppn == fail_ppn:
                    row.update(passed=False, exit=124, error='CPU_SIMULATED_TIMEOUT')
                run = out/row['name']
                run.mkdir()
                if rnr and ppn == 128 and profile == 'rm' and phase == 'validation' and row['configuration']['environment']['UCX_RM_URMA_RX_DEPTH'] == '512' and (rnr_failure or other_failure):
                    row.update(passed=False, error='CPU_SIMULATED_RNR' if rnr_failure else 'CPU_SIMULATED_OTHER',
                               cleanup={'local': {'terminated_own_pids': []}, 'peer': {'terminated_own_pids': []}})
                    (run/'launcher.log').write_text('Fatal: The send cr status is err: stale_errno; status:10.\n' if rnr_failure else 'Fatal: Unknown provider failure\n')
                base.write_json(run/'trial.json', row)
                return row
            with patch.object(bootstrap, 'load', return_value=(guard, engine, {}, {})), \
                 patch.object(runner, 'repeat_checkpoint', return_value={'cpu_fixture': True}), \
                 patch.object(runner, 'failed_scale_checkpoint', return_value={'cpu_failed_scale': True}), \
                 patch.object(runner, 'disk_inventory', return_value=self.disks()), \
                 patch.object(runner.os, 'geteuid', return_value=0), \
                 patch.object(runner.socket, 'gethostname', return_value='node89'), \
                 patch.object(sys, 'argv', ['scale', '--run', '--timing'] + (['--rnr-study'] if rnr else [])), \
                 patch.object(run_compare, 'run_trial', side_effect=run_trial), \
                 patch('zipfile.ZipFile') as bundle, redirect_stdout(io.StringIO()):
                bundle.return_value.__enter__.return_value.read.return_value = b'# CPU_SNAPSHOT'
                if fail_ppn or other_failure:
                    with self.assertRaisesRegex(RuntimeError, 'CPU_SIMULATED_TIMEOUT|CPU_SIMULATED_OTHER'):
                        runner.main()
                else:
                    runner.main()
            self.assertIs(base.inventory, inventory)
            out = next(case.glob('um_rnr_study_*' if rnr else 'um_osu_scale_*'))
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

    def test_rnr_schedule_scope_and_31_attempt_ceiling(self):
        rows = list(scale.rnr_tasks())
        self.assertEqual(len(rows), 28)
        self.assertEqual(scale.rnr_plan()['total_mpi'], 31)
        self.assertTrue(all(item[1:4] == ('osu_alltoall', 128, 8) for item, depth in rows))
        self.assertEqual({(item[6], depth) for item, depth in rows}, set(scale.RNR_CONDITIONS))

    def test_rnr_complete_controller_changes_only_rx_policy(self):
        report, observed = self.controller(rnr=True)
        self.assertEqual(report['status'], 'RNR_STUDY_ALL_CONDITIONS_COMPLETE_REVIEW_REQUIRED')
        self.assertEqual(len(observed), 31)
        self.assertTrue(report['all_conditions_passed'])
        self.assertEqual([r['status'] for r in report['conditions']], ['COMPLETE']*4)
        for row in report['trials']:
            env = row['configuration']['environment']
            prefix = 'UCX_'+row['profile'].upper()+'_URMA_'
            self.assertEqual(int(env[prefix+'RX_DEPTH']), row['requested_rx_depth'])
            self.assertEqual(env[prefix+'RX_MAX_BATCH'], '16')
            self.assertEqual(env[prefix+'RX_MAX_POLL'], '16')
        self.assertEqual(report['skipped_tasks'], [])

    def test_rnr_failure_preserved_other_conditions_continue(self):
        report, observed = self.controller(rnr=True, rnr_failure=True)
        self.assertEqual(report['status'], 'RNR_STUDY_COMPLETED_WITH_OBSERVED_FAILURES_REVIEW_REQUIRED')
        self.assertFalse(report['all_conditions_passed'])
        self.assertEqual(len(observed), 25)
        self.assertEqual(len(report['skipped_tasks']), 6)
        self.assertEqual([r['status'] for r in report['conditions']], ['FAILED_RNR', 'COMPLETE', 'COMPLETE', 'COMPLETE'])
        self.assertIsNone(report['conditions'][0]['median_us'])
        self.assertTrue(any(r['requested_rx_depth'] == 4096 for r in report['trials']))
        self.assertEqual(max(r[2] for r in observed), 128)

    def test_unknown_failure_stops_rnr_study(self):
        report, observed = self.controller(rnr=True, other_failure=True)
        self.assertEqual(report['status'], 'STOPPED_INCOMPLETE')
        self.assertEqual(len(observed), 4)
        self.assertFalse(any(r.get('requested_rx_depth') == 4096 for r in report['trials']))

    def test_rnr_continuation_is_narrow_and_requires_cleanup(self):
        row = {'passed': False, 'cleanup': {'local': {'terminated_own_pids': []}, 'peer': {'terminated_own_pids': []}}}
        text = 'Fatal: The send cr status is err: Cannot allocate memory; status:10.\n'
        self.assertTrue(scale.rnr_failure(row, text))
        self.assertFalse(scale.rnr_failure(row, text.replace('status:10', 'status:4')))
        self.assertFalse(scale.rnr_failure(row, text+'Fatal: Unknown error\n'))
        self.assertFalse(scale.rnr_failure(row, text.rstrip()+' Fatal: The send cr status is err: stale; status:4.\n'))
        row['cleanup']['peer']['terminated_own_pids'] = [123]
        self.assertFalse(scale.rnr_failure(row, text))
        row['cleanup'] = {}
        self.assertFalse(scale.rnr_failure(row, text))

    def test_actual_jfr_proof_rejects_clamp_and_pid_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            for rank in range(2):
                (out/('rank%d_pid%d.jsonl' % (rank, rank+100))).write_text(json.dumps(
                    {'event': 'acquire', 'kind': 'jfr', 'rc': 0, 'depth': 4096})+'\n')
            self.assertEqual(scale.jfr_depths(out, 1, {0:100, 1:101}, 4096), {0:[4096], 1:[4096]})
            with self.assertRaises(ValueError):
                scale.jfr_depths(out, 1, {0:100, 1:101}, 512)
            with self.assertRaises(ValueError):
                scale.jfr_depths(out, 1, {0:999, 1:101}, 4096)

    def test_failed_scale_checkpoint_reviews_229_without_upgrading_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            root = case/runner.FAILED_SCALE
            root.mkdir()
            gates = [fixture_row(p, 'validation') for p in scale.PROFILES]
            for row in gates:
                row['name'] = 'gate_'+row['profile']
            trials = []
            for group in range(12):
                for row in self.group_rows(group):
                    row['name'] = 'g%d_' % group + row['name']
                    trials.append(row)
            for row in self.group_rows(12):
                if row['phase'] == 'validation':
                    row['name'] = 'g12_'+row['name']
                    trials.append(row)
            failed = fixture_row('rm', 'timing')
            failed.update(name='osu_alltoall_rm_p128_timing_229', group=12,
                          benchmark='osu_alltoall', message_bytes=8, ppn=128, passed=False,
                          cleanup={'local': {'terminated_own_pids': []}, 'peer': {'terminated_own_pids': []}})
            trials.append(failed)
            for row in gates + trials:
                run = root/row['name']
                run.mkdir()
                (run/'trial.json').write_text(json.dumps(row))
            (root/failed['name']/'launcher.log').write_text('Fatal: The send cr status is err: stale; status:10.\n')
            report = {'gates': gates, 'trials': trials, 'source_commit': 'cpu', 'module_sha256': 'module',
                      'tool': {'tool_commit': '4c0ef1a6ee7d7df97ef8d08e4666836a1ff026f0'},
                      'status': 'STOPPED_INCOMPLETE',
                      'comparisons': [scale.summarize(trials, i) for i in range(12)]}
            (root/'results.json').write_text(json.dumps(report))
            value = runner.failed_scale_checkpoint(case, 'cpu', 'module')
            self.assertEqual(value['successful_tasks_reviewed'], 229)
            self.assertFalse(value['prior_failure_upgraded'])
            log = root/failed['name']/'launcher.log'
            log.write_text('Fatal: The send cr status is err: stale; status:10.\nFatal: interleaved/secondary error\nUCX ERROR secondary\n')
            value = runner.failed_scale_checkpoint(case, 'cpu', 'module')
            observation = value['historical_failure_log']
            self.assertTrue(observation['rnr_observed'])
            self.assertFalse(observation['live_continuation_allowed'])
            self.assertTrue(observation['live_continuation_rejections'])
            self.assertFalse(value['historical_log_exclusivity_required'])
            self.assertFalse(value['prior_failure_upgraded'])
            log.write_text('Fatal: The send cr status is err: stale; status:4.\n')
            with self.assertRaisesRegex(ValueError, 'historical scale RNR not observed'):
                runner.failed_scale_checkpoint(case, 'cpu', 'module')
            log.write_text('Fatal: The send cr status is err: stale; status:10.\n')
            (root/trials[0]['name']/'trial.json').write_text('{}')
            with self.assertRaises(ValueError):
                runner.failed_scale_checkpoint(case, 'cpu', 'module')


if __name__ == '__main__':
    unittest.main()
