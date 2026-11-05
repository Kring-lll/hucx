import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import policy
import bootstrap
import run_compare


def fixture_row(profile,phase,repeat=0,value=2):
    env={'UCX_TLS':'rc_urma,um_urma:aux,self' if profile=='rc' else profile+'_urma,self',
         'UCX_PROTO_ENABLE':'n','UCX_RNDV_THRESH':'inf','UCX_ZCOPY_THRESH':'inf',
         'UCX_LOG_LEVEL':'warn','LD_PRELOAD':'/candidate/lib/libucp.so.0:/out/libosu_identity.so'}
    return {'group':0,'profile':profile,'phase':phase,'repeat':repeat,'name':profile+phase+str(repeat),
            'passed':True,'exit':0,'configuration':{'environment':env,'resource_probe':phase=='resources',
            'arguments':['same_workload']},'output':{'value':value,'unit':'us','validation_pass':phase=='validation'},
            'rank_evidence':{'before_finalize_rss_sum_mib':90},
            'jettys':{0:{'peak_per_process':1},1:{'peak_per_process':1}},
            'effective_config':{'node89':{'SEG_SIZE':'4K'},'node97':{'SEG_SIZE':'4K'}}}


class CompareTests(unittest.TestCase):
    def test_schedule_exact_counts_and_rotating_order(self):
        rows=list(policy.schedule(True))
        self.assertEqual(len(rows),126)
        self.assertEqual([sum(r[4]==p for r in rows) for p in ('validation','timing','resources')],[18,90,18])
        timing=[r for r in rows if r[0]==0 and r[4]=='timing']
        self.assertEqual([r[6] for r in timing[:9]],['rc','rm','um','rm','um','rc','um','rc','rm'])
        self.assertEqual(len(list(policy.schedule(False))),18)
        self.assertEqual(max(r[2] for r in rows),4)
    def test_bandwidth_and_latency_arguments(self):
        for name in policy.BENCHMARKS:
            args=policy.arguments(name,1000,100,False,65536)
            self.assertEqual('--window-size' in args,name in ('osu_bw','osu_bibw'))
            self.assertEqual('--full' in args,name=='osu_alltoall')
            self.assertNotIn('--validation',args)
            self.assertIn('--validation',policy.arguments(name,100,10,True))
    def test_parse_original_modes_and_alltoall_ranges(self):
        for name in policy.BENCHMARKS:
            header='# '+policy.BENCHMARKS[name][1]+' v7.5.2\n'
            row='65536 3.0 2.0 4.0 100' if name=='osu_alltoall' else '65536 3.0'
            for validate in (False,True):
                value=policy.parse_output(header+row+(' Pass' if validate else ''),name,validate,100,65536)
                self.assertEqual(value['value'],3.0)
                self.assertEqual(value['validation_pass'],validate)
    def test_parser_rejects_duplicate_nonfinite_and_error(self):
        header='# OSU MPI Multi Latency Test v7.5.2\n'
        for row in ('8 2 Pass\n8 2 Pass','8 nan Pass','8 2 Fail','8 2','8 2 Pass\nUCX ERROR broken'):
            with self.subTest(row=row),self.assertRaises(ValueError):
                policy.parse_output(header+row,'osu_multi_lat',True,100)
    def test_fault_or_path_or_observer_in_timing_rejected(self):
        for key,value in (('LD_PRELOAD','/out/libum_send_probe.so'),('HUCX_UM_SEND_FAULT','loss_dup'),
                          ('UCX_TLS','tcp,self'),('UCX_ZCOPY_THRESH','auto')):
            row=fixture_row('um','timing');row['configuration']['environment'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):policy.inspect_trial(row,'um','timing')
    def rows(self):
        return [fixture_row(p,phase,i,30 if p=='rm' and phase=='timing' and i==1 else 2)
                for p in policy.PROFILES for phase,i in [('validation',0)]+[('timing',i) for i in range(5)]+[('resources',0)]]
    def test_outlier_preserved_and_complete_group_required(self):
        rows=self.rows();group=policy.summarize(rows,0)
        self.assertEqual(group['profiles']['rm']['samples'],[2,30,2,2,2])
        self.assertEqual(group['profiles']['rm']['maximum'],30)
        self.assertEqual(group['profiles']['rm']['median'],2)
        with self.assertRaises(ValueError):policy.summarize(rows[:-1],0)
        rows[0]['passed']=False
        with self.assertRaises(ValueError):policy.summarize(rows,0)
    def test_resource_workload_mismatch_rejected(self):
        rows=self.rows();rows[-1]['configuration']['arguments']=['different']
        with self.assertRaises(ValueError):policy.summarize(rows,0)
    def test_tables_only_show_completed_groups(self):
        with tempfile.TemporaryDirectory() as f:
            root=Path(f);policy.write_tables(root,{'comparisons':[]})
            self.assertNotIn('2.000',(root/'comparison.md').read_text())
            policy.write_tables(root,{'comparisons':[policy.summarize(self.rows(),0)]})
            self.assertIn('30',(root/'comparison.csv').read_text())
    def test_jetty_mode_mapping_is_not_rc_for_um(self):
        self.assertEqual(policy.MODES,{'rc':2,'rm':1,'um':4})
    def test_config_records_defaults_and_rejects_peer_difference(self):
        with tempfile.TemporaryDirectory() as f:
            root=Path(f)
            for h in ('node89','node97'):
                (root/(h+'_ucx_config.txt')).write_text('UCX_UM_URMA_SEG_SIZE=8K\nUCX_UM_URMA_TX_DEPTH=64\n')
            self.assertEqual(run_compare.config_summary(root,'um')['node89']['SEG_SIZE'],'8K')
            (root/'node97_ucx_config.txt').write_text('UCX_UM_URMA_SEG_SIZE=4K\n')
            with self.assertRaises(ValueError):run_compare.config_summary(root,'um')
    def test_checkpoint_only_normalizes_timing_profile(self):
        with tempfile.TemporaryDirectory() as f:
            case=Path(f);root=case/bootstrap.PRIOR_NAME;root.mkdir()
            report={'source_commit':'commit','module_sha256':'module',
                    'status':'PRIOR_13_AND_REMAINING_6_PASS_FULL_ACCEPTANCE_PENDING',
                    'coverage':{'prior_reviewed':13,'new_passed':6,'selected_total':19},'cases':[],'timing':[]}
            for kind,count in (('cases',6),('timing',10)):
                for i in range(count):
                    row={'name':kind+str(i),'passed':True,'exit':0}
                    run=root/row['name'];run.mkdir();(run/'trial.json').write_text(json.dumps(row))
                    if kind=='timing':row['profile']='rc' if i%2==0 else 'rm'
                    report[kind].append(row)
            path=root/'results.json';path.write_text(json.dumps(report));before=path.read_bytes()
            with patch.object(bootstrap,'PRIOR_SHA',bootstrap.digest(path)):
                self.assertEqual(bootstrap.checkpoint(case,'commit','module')['new_functional_passed'],6)
                self.assertEqual(path.read_bytes(),before)
                trial=root/'timing0/trial.json';trial.write_text('{"name":"changed"}')
                with self.assertRaises(ValueError):bootstrap.checkpoint(case,'commit','module')
    def test_real_jetty_parser_accepts_um_and_rejects_leak(self):
        sys.path.insert(0,str(HERE.parent/'osu_compare'))
        spec=importlib.util.spec_from_file_location('three_original_analysis',HERE.parent/'osu_compare/analysis.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as f:
            root=Path(f)
            for rank in range(2):
                rows=[{'event':'start','rank':rank},{'event':'acquire','kind':'jetty','rc':0,'mode':4,'ptr':'x','ctx':'c','depth':64},
                      {'event':'release','kind':'jetty','rc':0,'ptr':'x'}]
                (root/('rank%d_pid%d.jsonl'%(rank,100+rank))).write_text('\n'.join(map(json.dumps,rows)))
            self.assertEqual(len(module.inspect_jettys(root,1,4,{0:100,1:101},64)),2)
            p=root/'rank0_pid100.jsonl';p.write_text('\n'.join(p.read_text().splitlines()[:-1]))
            with self.assertRaises(ValueError):module.inspect_jettys(root,1,4,{0:100,1:101},64)

    def controller_fixture(self, fail_um=False):
        """Exercise real controller/checks/tables, replacing only node operations."""
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as f:
            case=Path(f);source=case/'source';source.mkdir();(source/'entry.py').write_text('original')
            lib=case/'install/lib/ucx/libuct_ub.so.0.0.0';lib.parent.mkdir(parents=True);lib.write_bytes(b'CPU_MODULE')
            project=case/'helpers';project.mkdir()
            for name in ('osu_rank.py','rank_exec.py'):(project/name).write_text('# fixture')
            guard=SimpleNamespace(CASE=case,COMMIT='fixture',MODULE_SHA=bootstrap.digest(lib),
                                  RELATIVE_ENTRY='entry.py',ENTRY_SHA=bootstrap.digest(source/'entry.py'))
            modes=[];environment_calls=[]
            base=SimpleNamespace(parsed_config=None,HOSTS=['a','b'],
                write_json=lambda p,v:Path(p).write_text(json.dumps(v)),
                ssh=lambda script,*argv,**kw:bootstrap.digest(argv[0]) if 'sha256sum' in script else '',
                copy_remote=lambda *a:None)
            osu=SimpleNamespace(base=base,DEFAULT_CASE=None,PRODUCT_COMMIT=None,PRODUCT_MODULE=None,
                BENCHMARKS={},arguments=None,parse_output=None,environment=None,budget=None,inspect_jettys=None,
                LAB=case,ROOT=project,A89=project)
            engine={'osu':osu,'PROFILE':'rm','OBSERVE':False,'FAULT':False,
                    'deploy':lambda *a:None,'validate_source':lambda *a:{}}
            def environment(*a):
                profile=engine['PROFILE'];environment_calls.append(profile)
                return fixture_row(profile,'timing')['configuration']['environment']
            engine['environment']=environment
            engine['parsed_config']=lambda *a:True
            osu.saved_spec=lambda *a:(['mpirun'],{})
            def preflight(out,commands):
                for name in ('osu_bw','osu_bibw'):(out/(name+'_help.txt')).write_text('--window-size --buffer-num')
                (out/'libosu_identity.so').write_bytes(b'CPU_IDENTITY')
                return {'inventory':[{'available_cores':4},{'available_cores':4}]}
            osu.preflight=preflight
            def inspect_jettys(run,ppn,mode,pids,depth):
                modes.append((engine['PROFILE'],mode))
                return {i:{'peak_per_process':1} for i in range(2*ppn)}
            osu.inspect_jettys=inspect_jettys
            def one_trial(out,commands,evidence,benchmark,mode,ppn,phase,index,args,message_bytes):
                profile=engine['PROFILE'];run=out/('trial_'+str(index));run.mkdir()
                row=fixture_row(profile,phase,value=2)
                row.update(name=run.name,benchmark=benchmark,ppn=ppn,iterations=args.iterations,warmup=args.warmup)
                row['configuration']['environment']=osu.environment({},mode,case/'install',{})
                row['configuration']['arguments']=osu.arguments(benchmark,args.iterations,args.warmup,phase=='validation',message_bytes)
                row['output']['unit']='MB/s' if benchmark in ('osu_bw','osu_bibw') else 'us'
                for host in ('node89','node97'):
                    (run/(host+'_ucx_config.txt')).write_text('UCX_%s_URMA_SEG_SIZE=%s\n'%(profile.upper(),'4K' if profile=='rm' else '8K'))
                for rank in range(2*ppn):
                    for point in ('after_init','before_finalize'):
                        (run/('rank%d_%s.maps'%(rank,point))).write_text('libosu_identity.so'+(' liburma_resource_observer.so' if phase=='resources' else ''))
                if phase=='resources':row['jettys']=osu.inspect_jettys(run,ppn,1,{},64)
                if fail_um and profile=='um':row.update(passed=False,exit=124,error='CPU_SIMULATED_UM_GATE_FAILURE')
                base.write_json(run/'trial.json',row)
                return row
            osu.one_trial=one_trial
            with patch.object(bootstrap,'load',return_value=(guard,engine,{},{})),patch.object(run_compare.os,'geteuid',return_value=0),patch.object(run_compare.socket,'gethostname',return_value='node89'),patch.object(sys,'argv',['run_compare.py','--run','--timing']),redirect_stdout(io.StringIO()):
                if fail_um:
                    with self.assertRaisesRegex(RuntimeError,'mandatory um gate'):run_compare.main()
                else:run_compare.main()
            out=next(case.glob('um_osu_three_*'));report=json.loads((out/'results.json').read_text())
            for row in report['gates']+report['trials']:
                self.assertEqual(json.loads((out/row['name']/'trial.json').read_text()),row)
            self.assertEqual(lib.read_bytes(),b'CPU_MODULE')
            return report,modes

    def test_whole_controller_129_cpu_fixture_and_persisted_profiles(self):
        report,modes=self.controller_fixture()
        self.assertEqual(report['status'],'SELECTED_THREE_PROFILE_OSU_COMPARISON_COMPLETE_REVIEW_REQUIRED')
        self.assertEqual(len(report['gates']),3);self.assertEqual(len(report['trials']),126)
        self.assertEqual(len(report['comparisons']),6)
        self.assertTrue(all(policy.MODES[p]==mode for p,mode in modes))
        self.assertEqual(len(modes),18)

    def test_um_gate_failure_stops_before_comparison(self):
        report,modes=self.controller_fixture(True)
        self.assertEqual(report['status'],'STOPPED_INCOMPLETE')
        self.assertEqual(len(report['gates']),3)
        self.assertEqual(report['trials'],[]);self.assertEqual(report['comparisons'],[])


if __name__=='__main__':unittest.main()
