"""RM-only dispatch, file/summary gates and historical provenance fixtures."""
import copy
import contextlib
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis
import baselines
import run as runner
import scale_rm
from support import PRODUCT_COMMIT, PRODUCT_MODULE, budget


def rm_row(benchmark, ppn, phase, repeat):
    validation = phase == 'validation'
    return {'name': benchmark + phase + str(repeat), 'benchmark': benchmark, 'mode': 'rm',
            'ppn': ppn, 'phase': phase, 'repeat': repeat, 'passed': True,
            'configuration': {'arguments': runner.arguments(benchmark, 1000 if validation else 5000,
                                                            100 if validation else 1000, validation)},
            'output': {'latency_us': 3.0 + repeat, 'validation_enabled': validation, 'validation_pass': validation},
            'rank_evidence': {'before_finalize_rss_sum_mib': 100.0},
            'jettys': {'0': {'peak_per_process': 1, 'peak_per_context': {'c': 1}}}}


class ScaleTests(unittest.TestCase):
    def test_baseline_failure_is_recorded_and_does_not_start_mpi(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(scale_rm, 'DEFAULT_CASE', Path(directory)), \
                 patch.object(scale_rm.socket, 'gethostname', return_value='node89'), \
                 patch.object(scale_rm.os, 'geteuid', return_value=0), \
                 patch.object(scale_rm.runner, 'validate_package', return_value={}), \
                 patch.object(scale_rm.baselines, 'load_baselines', side_effect=ValueError('CPU_BAD_SOURCE')), \
                 patch.object(scale_rm.runner, 'preflight') as preflight, \
                 patch.object(sys, 'argv', ['scale_rm.py', '--merge-only']), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    scale_rm.main()
            self.assertEqual(raised.exception.code, 1)
            preflight.assert_not_called()
            report = json.loads(next(Path(directory).glob('osu_rm_scale_*/results.json')).read_text())
            self.assertEqual(report['status'], 'STOPPED_INCOMPLETE')
            self.assertEqual(report['highest_common_passed_ppn'], 0)
            self.assertIn('CPU_BAD_SOURCE', report['error'])

    def test_levels_respect_capacity_and_do_not_request_rc(self):
        self.assertEqual(scale_rm.planned_levels(192, 192), [32,48,64,96,128,160,192])
        self.assertEqual(scale_rm.planned_levels(60, 192), [32,48,60])
        self.assertEqual(scale_rm.planned_levels(192, 64), [32,48,64])
        self.assertEqual(scale_rm.planned_levels(20, 192), [])
        with self.assertRaises(ValueError):
            scale_rm.planned_levels(192, 193)

    def report(self):
        return {'trials': [], 'rm_levels': [], 'comparisons': [], 'highest_common_passed_ppn': 20,
                'highest_by_benchmark': {b:20 for b in analysis.BENCHMARKS}, 'status':'CPU_FIXTURE'}

    def test_every_stage_dispatches_only_rm_and_failure_cannot_advance_common_max(self):
        calls = []
        def execute(out, commands, evidence, benchmark, mode, ppn, phase, repeat, args):
            calls.append((mode, benchmark, phase, repeat))
            row = rm_row(benchmark, ppn, phase, repeat)
            if benchmark == 'osu_alltoall' and phase == 'resources':
                row.update(passed=False, error='CPU_SYNTHETIC_RESOURCE_FAILURE')
            return row
        report = self.report()
        with self.assertRaisesRegex(RuntimeError, 'CPU_SYNTHETIC'):
            scale_rm.run_level(Path('/fixture'), {}, {}, 32, types.SimpleNamespace(), report, lambda:None, execute)
        self.assertEqual({r[0] for r in calls}, {'rm'})
        self.assertEqual(len(calls), 14)
        self.assertEqual(report['highest_common_passed_ppn'], 20)
        self.assertEqual(report['highest_by_benchmark']['osu_multi_lat'], 32)
        self.assertEqual(report['highest_by_benchmark']['osu_alltoall'], 20)

    def test_successful_level_and_xx_have_no_invented_rc_or_percentage(self):
        def execute(out, commands, evidence, benchmark, mode, ppn, phase, repeat, args):
            self.assertEqual(mode, 'rm')
            return rm_row(benchmark, ppn, phase, repeat)
        report = self.report()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            scale_rm.run_level(out, {}, {}, 32, types.SimpleNamespace(), report, lambda:None, execute)
            self.assertEqual(report['highest_common_passed_ppn'], 32)
            scale_rm.write_tables(out, report)
            with (out / 'comparison.csv').open(encoding='utf-8-sig') as file:
                rows = list(csv.reader(file))
            self.assertEqual(rows[1][2], 'XX')
            self.assertEqual(rows[1][4], 'XX')
            self.assertNotIn('rc', report['rm_levels'][0]['modes'])
            self.assertIsNone(report['rm_levels'][0]['rm_latency_reduction_pct'])
            self.assertIn('CPU_FIXTURE', (out / 'comparison.md').read_text())


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='osu_baseline_cpu_')
        self.case = Path(self.temp.name)
        self.run = self.case / 'fixture_batch'
        self.run.mkdir()
        self.path = self.run / 'results.json'
        self.manifest = json.loads(Path(__file__).with_name('checkpoint_manifest_collfix.json').read_text())
        self.assertEqual(baselines.source_digest(self.manifest), baselines.SOURCE_MANIFEST)
        inventory = [{'hostname':'node89','boot_id':'a','allowed_cpus':[0]},
                     {'hostname':'node97','boot_id':'b','allowed_cpus':[0]}]
        preflight = {'inventory':inventory,
            'benchmarks':{name:{'mpi_library':'/fixture/libmpi.so.40'} for name in analysis.BENCHMARKS}}
        self.report = {'plan':{'product_commit':PRODUCT_COMMIT,'module_sha256':PRODUCT_MODULE,
            'ppn':[1],'repeats':5,'iterations':5000,'warmup':1000,'message_bytes':8,
            'pml':runner.PML,'coll_components':runner.COLL,'benchmarks':list(analysis.BENCHMARKS)},
            'status':'REQUESTED_OSU_MATRIX_PASS_REVIEW_ALL_SAMPLES','started':'CPU_SYNTHETIC_ONLY',
            'manifest':self.manifest,'preflight':preflight,'trials':[],'comparisons':[]}
        for benchmark in analysis.BENCHMARKS:
            for mode in ('rm','rc'):
                for phase, repeat in [('validation',0),('resources',0)] + [('timing',i) for i in range(5)]:
                    name = benchmark + '_' + mode + '_' + phase + '_' + str(repeat)
                    directory = self.run / name
                    directory.mkdir()
                    validation = phase == 'validation'
                    iterations,warmup = (1000,100) if validation else (5000,1000)
                    config={'run':str(directory),'nonce':'b'*32,
                            'resource_probe':phase=='resources',
                            'arguments':runner.arguments(benchmark,iterations,warmup,validation)}
                    (directory/'rank_config.json').write_text(json.dumps(config))
                    text = ('# ' + analysis.BENCHMARKS[benchmark][1] + ' v7.5.2\n8 ' +
                            ('4.00 3.90 4.10 '+str(iterations) if benchmark=='osu_alltoall' else '2.30') +
                            (' Pass' if validation else '')+'\n')
                    (directory/'launcher.log').write_text(text)
                    for rank in (0,1):
                        data={'rank':rank,'pid':100+rank,'size':2,'ppn':1,'hostname':inventory[rank]['hostname'],
                              'finalized':True,'proof_error':0,'cpu_affinity':[0],
                              'init_rss_kib':10000,'before_finalize_rss_kib':20000}
                        (directory/('rank%d.json'%rank)).write_text(json.dumps(data))
                        (directory/('pid_rank%d.json'%rank)).write_text(json.dumps({
                            'rank':rank,'nonce':config['nonce'],'hostname':inventory[rank]['hostname'],'cpu_affinity':[0]}))
                        maps='\n'.join('0-1 rw '+str(self.case/'install/lib'/(lib+'.0')) for lib in
                                       ('libucp.so','libuct.so','libucs.so','libucm.so','libuct_ub.so'))
                        maps+='\n0-1 rw /fixture/libmpi.so.40\n'
                        for snapshot in ('after_init','before_finalize'):
                            (directory/('rank%d_%s.maps'%(rank,snapshot))).write_text(maps)
                        if phase=='resources':
                            events=[{'event':'start','rank':rank},
                                {'event':'acquire','kind':'jetty','rc':0,'mode':1 if mode=='rm' else 2,
                                 'ptr':'p','ctx':'c','depth':64},
                                {'event':'release','kind':'jetty','rc':0,'ptr':'p'}]
                            (directory/('rank%d_pid%d.jsonl'%(rank,100+rank))).write_text('\n'.join(map(json.dumps,events)))
                    rank_evidence=analysis.inspect_ranks(directory,1,inventory,self.case/'install',config['nonce'],'/fixture/libmpi.so.40')
                    row={'name':name,'benchmark':benchmark,'ppn':1,'mode':mode,'phase':phase,'repeat':repeat,
                         'configuration':config,'budget':budget(1),'passed':True,'exit':0,
                         'output':analysis.parse_output(text,benchmark,validation,iterations),
                         'rank_evidence':rank_evidence,
                         'cleanup':{'local':{'terminated_own_pids':[]},'peer':{'terminated_own_pids':[]}}}
                    if phase=='resources':
                        row['jettys']=analysis.inspect_jettys(directory,1,1 if mode=='rm' else 2,rank_evidence['pids'],64)
                    row=baselines.normalized(row)
                    (directory/'trial.json').write_text(json.dumps(row))
                    self.report['trials'].append(row)
        self.report['comparisons']=[analysis.compare(self.report['trials'],name,1,5) for name in analysis.BENCHMARKS]
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.report))

    def load(self):
        with patch.dict(baselines.SOURCE_BATCHES, {'fixture_batch':[1]}, clear=True):
            return baselines.load_batch(self.path,self.case,self.manifest)

    def test_complete_original_files_recomputed_without_modifying_sources(self):
        before=self.path.read_bytes()
        loaded=self.load()
        self.assertEqual(len(loaded['report']['trials']),28)
        self.assertEqual(len(loaded['source']['raw_trial_log_sha256']),56)
        self.assertEqual(self.path.read_bytes(),before)

    def test_rejects_modified_maps_and_incomplete_source_status(self):
        path=self.run/self.report['trials'][0]['name']/'rank0_before_finalize.maps'
        original=path.read_bytes()
        path.write_text('wrong mapping')
        with self.assertRaisesRegex(ValueError,'mapping'):
            self.load()
        path.write_bytes(original)
        self.report['status']='STOPPED_INCOMPLETE'
        self.save()
        with self.assertRaisesRegex(ValueError,'identity'):
            self.load()

    def test_rejects_actual_runtime_reboot_or_changed_library(self):
        common={'candidate':{'install':'x','module_sha256':'a','base_libraries_sha256':{}},
            'inventory':[{'hostname':'a','boot_id':'a','allowed_cpus':[0]},
                         {'hostname':'b','boot_id':'b','allowed_cpus':[0]}],
            'observer':{},'saved_launcher_sha256':{},
            'benchmarks':{b:{'path':b,'sha256':'a','mpi_library':'m','mpi_library_sha256':'a'} for b in analysis.BENCHMARKS},
            'coll_components':runner.COLL,'pml':runner.PML,'coll_tuned_dynamic_rules':0}
        changed=copy.deepcopy(common)
        changed['inventory'][1]['boot_id']='new'
        with self.assertRaisesRegex(ValueError,'boot'):
            baselines.assert_same_runtime(common,changed)
        changed=copy.deepcopy(common)
        changed['candidate']['module_sha256']='other'
        with self.assertRaisesRegex(ValueError,'library'):
            baselines.assert_same_runtime(common,changed)
