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
import load_snapshot


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

    def controller_fixture(self, fail_um=False, study=False):
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
                    (run/('rank%d.json'%rank)).write_text(json.dumps({'hostname':'node89' if rank<ppn else 'node97','cpu_affinity':[rank%ppn]}))
                    for point in ('after_init','before_finalize'):
                        (run/('rank%d_%s.maps'%(rank,point))).write_text('libosu_identity.so'+(' liburma_resource_observer.so' if phase=='resources' else ''))
                if phase=='resources':row['jettys']=osu.inspect_jettys(run,ppn,1,{},64)
                if fail_um and profile=='um':row.update(passed=False,exit=124,error='CPU_SIMULATED_UM_GATE_FAILURE')
                base.write_json(run/'trial.json',row)
                return row
            osu.one_trial=one_trial
            snap_index=[0]
            def captures(*a):
                snap_index[0]+=1;n=snap_index[0]
                return {host:{'hostname':host,'monotonic_ns':n*1000000000,'cpus':{'cpu'+str(i):[n*10,0,n*5,n*85,0,0,0,0] for i in range(4)},'processes':{}} for host in ('node89','node97')}
            with patch.object(bootstrap,'load',return_value=(guard,engine,{},{})),patch.object(run_compare.os,'geteuid',return_value=0),patch.object(run_compare.socket,'gethostname',return_value='node89'),patch.object(sys,'argv',['run_compare.py','--run','--timing']+(['--repeat-study'] if study else [])),patch.object(run_compare,'capture_load',side_effect=captures),patch('zipfile.ZipFile') as zipmock,redirect_stdout(io.StringIO()):
                zipmock.return_value.__enter__.return_value.read.return_value=b'# CPU snapshot helper fixture\n'
                if fail_um:
                    with self.assertRaisesRegex(RuntimeError,'mandatory um gate'):run_compare.main()
                else:run_compare.main()
            out=next(case.glob('um_osu_repeat_*' if study else 'um_osu_three_*'));report=json.loads((out/'results.json').read_text())
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

    def test_repeat_schedule_counts_and_balanced_positions(self):
        rows=list(policy.schedule(True,True))
        self.assertEqual(len(rows),180)
        self.assertEqual([sum(r[4]==p for r in rows) for p in ('validation','timing','resources')],[9,162,9])
        for group,n in enumerate((30,12,12)):
            timing=[r for r in rows if r[0]==group and r[4]=='timing']
            self.assertEqual(len(timing),3*n)
            for profile in policy.PROFILES:
                self.assertEqual([sum(timing[j][6]==profile for j in range(position,len(timing),3)) for position in range(3)],[n//3]*3)
    def test_repeat_complete_controller_183_and_load_scope(self):
        report,modes=self.controller_fixture(study=True)
        self.assertEqual(report['status'],'FOCUSED_REPEAT_STUDY_COMPLETE_REVIEW_REQUIRED')
        self.assertEqual(len(report['gates'])+len(report['trials']),183)
        self.assertEqual(len(report['comparisons']),3)
        self.assertEqual([len(g['profiles']['rm']['samples']) for g in report['comparisons']],[30,12,12])
        self.assertEqual(report['load_capture_incomplete_trials'],[])
        self.assertEqual(len(modes),9)
        self.assertIn('not OSU timed-loop',report['trials'][0]['load_evidence']['node89']['scope'])
    def test_repeat_quantile_is_run_average_not_message_tail(self):
        rows=[]
        for profile in policy.PROFILES:
            for phase,i in [('validation',0)]+[('timing',i) for i in range(30)]+[('resources',0)]:
                rows.append(fixture_row(profile,phase,i,20 if phase=='timing' and i>=28 else 4))
        group=policy.summarize(rows,0,30,policy.STUDY_GROUPS)
        value=group['profiles']['rm']
        self.assertEqual(value['p95_nearest_rank'],20)
        self.assertEqual(value['above_2x_median'],2)
        self.assertIn('not per-message',value['quantile_scope'])
        with self.assertRaises(ValueError):policy.summarize(rows[:-1],0,30,policy.STUDY_GROUPS)
    def test_load_delta_and_pid_reuse(self):
        a={'hostname':'node89','monotonic_ns':100,'cpus':{'cpu0':[10,0,5,85,0,0,0,0]},'processes':{
            '2':{'start_ticks':1,'cpu_ticks':10},'3':{'start_ticks':1,'cpu_ticks':10},'4':{'start_ticks':1,'cpu_ticks':10}}}
        b={'hostname':'node89','monotonic_ns':1000000100,'cpus':{'cpu0':[30,0,10,155,0,2,3,0]},'processes':{
            '2':{'start_ticks':1,'cpu_ticks':20,'comm':'worker','last_cpu':0,'allowed_cpus':[0]},
            '3':{'start_ticks':2,'cpu_ticks':30,'comm':'reused','last_cpu':0,'allowed_cpus':[0]},
            '4':{'start_ticks':1,'cpu_ticks':40,'comm':'own','last_cpu':0,'allowed_cpus':[0]}}}
        v=load_snapshot.compare(a,b,[0],[4])
        self.assertEqual(v['cpu_interval']['cpu0']['busy_pct'],30)
        self.assertEqual([p['pid'] for p in v['active_existing_processes_top20']],[2])
        self.assertEqual(v['active_existing_processes_top20'][0]['cpu_ticks_delta'],10)
        b['hostname']='node97'
        with self.assertRaises(ValueError):load_snapshot.compare(a,b,[0])
    def test_live_load_snapshot_no_command_lines(self):
        value=load_snapshot.snapshot()
        self.assertIn('cpu',value['cpus']);self.assertTrue(value['processes'])
        self.assertTrue(all('cmdline' not in p for p in value['processes'].values()))
    def test_reviewed_three_profile_checkpoint_hash_and_trials(self):
        with tempfile.TemporaryDirectory() as f:
            case=Path(f);root=case/bootstrap.COMPARE_NAME;root.mkdir()
            trials=[]
            for group in range(6):
                for row in self.rows():
                    row['group']=group;row['name']='g%d_'%group+row['name'];trials.append(row)
            gates=[fixture_row(p,'validation') for p in policy.PROFILES]
            for row in gates:row['name']='gate_'+row['profile']
            report={'source_commit':'c','module_sha256':'m','status':'SELECTED_THREE_PROFILE_OSU_COMPARISON_COMPLETE_REVIEW_REQUIRED',
                    'gates':gates,'trials':trials,'comparisons':[policy.summarize(trials,i) for i in range(6)]}
            for row in gates+trials:
                d=root/row['name'];d.mkdir();(d/'trial.json').write_text(json.dumps(row))
            path=root/'results.json';path.write_text(json.dumps(report))
            with patch.object(bootstrap,'COMPARE_SHA',bootstrap.digest(path)):
                self.assertEqual(bootstrap.comparison_checkpoint(case,'c','m')['reviewed_mpi'],129)
                p=root/trials[0]['name']/'trial.json';p.write_text('{}')
                with self.assertRaises(ValueError):bootstrap.comparison_checkpoint(case,'c','m')


if __name__=='__main__':unittest.main()
