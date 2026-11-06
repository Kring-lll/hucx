import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('nopsn_candidate',ROOT/'run_candidate.py')
n=importlib.util.module_from_spec(spec);spec.loader.exec_module(n)

class Tests(unittest.TestCase):
    def test_plan_scope_order_and_no_timed_probe(self):
        rows=list(n.schedule(True));p=n.plan(True)
        self.assertEqual(p['total_mpi'],215)
        self.assertEqual((p['functional_and_validation'],p['timing'],p['resources']),(47,140,28))
        self.assertEqual(n.plan(False)['total_mpi'],47)
        self.assertTrue(all(r[2]==1 for r in rows if r[1] in ('osu_latency','osu_multi_lat','osu_bw','osu_bibw')))
        self.assertTrue(all(not r[7] for r in rows if r[4]=='timing'))
        self.assertEqual(sum(r[7] for r in rows),2)
        for g in range(14):
            t=[r for r in rows if r[0]==g and r[4]=='timing']
            self.assertEqual([r[6] for r in t],['full','nopsn','nopsn','full','full','nopsn','nopsn','full','full','nopsn'])

    def rows(self,group=0):
        rows=[]
        for item in n.schedule(True):
            g,b,ppn,size,phase,repeat,v,observed=item
            if g!=group:continue
            values=[2,2,20,2,2] if v=='nopsn' else [3]*5
            row={'group':g,'benchmark':b,'ppn':ppn,'message_bytes':size,'phase':phase,'repeat':repeat,'variant':v,'passed':True,
                'configuration':{'arguments':['same']},'output':{'value':values[repeat] if phase=='timing' else 1,'unit':'us'},
                'rank_evidence':{'before_finalize_rss_sum_mib':80},'jettys':{str(i):{'peak_per_process':1} for i in range(2*ppn)},
                'effective_config':{'node89':{'RX_DEPTH':'4096'},'node97':{'RX_DEPTH':'4096'}}}
            rows.append(row)
        return rows

    def test_summary_preserves_outlier_and_rejects_missing(self):
        rows=self.rows();v=n.summarize(rows,0)
        self.assertEqual(v['variants']['nopsn']['samples'],[2,2,20,2,2])
        self.assertEqual(v['variants']['nopsn']['maximum'],20)
        with self.assertRaises(ValueError):n.summarize(rows[:-1],0)
        rows[0]['passed']=False
        with self.assertRaises(ValueError):n.summarize(rows,0)

    def test_summary_timing_resource_workload_identity(self):
        rows=self.rows();rows[-1]['configuration']['arguments']=['different']
        with self.assertRaises(ValueError):n.summarize(rows,0)

    def test_wire_receipts_reject_wrong_header_identity_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)
            row={'ppn':1,'rank_evidence':{'pids':{0:100,1:101}},'configuration':{'nonce':'c'*32}}
            values=[{'rank':r,'pid':100+r,'nonce':'c'*32,'schema':1,'errors':0,'data_success':1,'short_success':1,'header_mask':64,'max_wr_bytes':22,'retry_success':0} for r in range(2)]
            def write():
                for r,v in enumerate(values):n.dump(run/('nopsn_wire_rank%d_pid%d.json'%(r,100+r)),v)
            def ssh(script,*args):return json.dumps({x:(run/x).read_text() for x in json.loads(args[2])})
            write();self.assertEqual(len(n.wire_evidence(SimpleNamespace(ssh=ssh),run,row,6)),2)
            for key,value in [('header_mask',256),('nonce','d'*32),('pid',999),('max_wr_bytes',4097),('retry_success',1),('errors',1)]:
                old=values[1][key];values[1][key]=value;write()
                with self.assertRaises(ValueError):n.wire_evidence(SimpleNamespace(ssh=ssh),run,row,6)
                values[1][key]=old

    def test_candidate_guard_checks_archive_marker_source_and_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab=Path(tmp);case=lab/'case';source=case/'source';entry=source/'contrib/rm_ctp/nopsn';entry.mkdir(parents=True)
            archive=lab/'source.tar.gz';archive.write_bytes(b'CPU_ARCHIVE')
            module=case/'install/lib/ucx/libuct_ub.so.0.0.0';module.parent.mkdir(parents=True);module.write_bytes(b'CPU_MODULE')
            marker=source/'.hucx_source_commit';marker.write_text('a'*40)
            payload=source/'data.c';payload.write_text('CPU_SOURCE')
            manifest=source/'contrib/rm_ctp/r1/candidate_manifest.json';manifest.parent.mkdir();n.dump(manifest,{'files':{'data.c':n.digest(payload)}})
            build={'source_commit':'a'*40,'archive':str(archive),'archive_sha256':n.digest(archive),'module_sha256':n.digest(module)};n.dump(case/'build_manifest.json',build)
            with patch.object(n,'ROOT',entry),patch.object(n,'LAB',lab):
                self.assertEqual(n.validate_candidate(case,'a'*40),build)
                for path,data in [(archive,b'BAD_ARCHIVE'),(marker,b'b'*40),(payload,b'BAD_SOURCE'),(module,b'BAD_MODULE')]:
                    old=path.read_bytes();path.write_bytes(data)
                    with self.assertRaises(ValueError):n.validate_candidate(case,'a'*40)
                    path.write_bytes(old)

    def controller(self,fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);full=root/'full';candidate=root/'candidate'
            for case in (full,candidate):
                p=case/'install/lib/ucx/libuct_ub.so.0.0.0';p.parent.mkdir(parents=True);p.write_bytes(case.name.encode())
            build={'source_commit':'candidate_commit','module_sha256':n.digest(candidate/'install/lib/ucx/libuct_ub.so.0.0.0')}
            inventory=[{'hostname':h,'available_cores':192,'mem_available_kib':64*1024**2,'nofile_soft':4096,'nproc_soft':-1,'boot_id':'same','allowed_cpus':list(range(192))} for h in ('node89','node97')]
            def ssh(script,*args,**kw):
                if 'sha256sum' in script:return n.digest(args[0])+'  fixture'
                if len(args)==3 and 'nopsn_wire' in args[2]:
                    return json.dumps({x:(Path(args[1])/x).read_text() for x in json.loads(args[2])})
                return ''
            def compile_run(cmd,env,log,timeout):
                Path(log).write_text('CPU_FAKE_COMPILE')
                if '-o' in cmd:Path(cmd[cmd.index('-o')+1]).write_bytes(b'CPU_FIXTURE')
                return {'exit':0}
            base=SimpleNamespace(inventory=lambda:inventory,ssh=ssh,copy_remote=lambda *a:None,run_logged=compile_run,
                module_check=lambda *a:True,HOSTS=['node89','node97'],parsed_config=None)
            original=ROOT.parent/'osu_compare'
            osu=SimpleNamespace(base=base,ROOT=original,A89=ROOT.parent/'a89',LAB=root,OSU=root,
                saved_spec=lambda *a:(['/fake/bin/mpirun'],{}),inspect_jettys=lambda *a:{})
            def preflight(out,commands):
                (out/'libosu_identity.so').write_bytes(b'CPU_FIXTURE');return {'inventory':inventory,'benchmarks':{k:{'mpi_library':'/fake/libmpi.so'} for k in ('osu_latency','osu_multi_lat','osu_bw','osu_bibw','osu_alltoall')}}
            osu.preflight=preflight
            osu.mpi_dependency=lambda text:Path('/fake/libmpi.so')
            engine={'osu':osu,'deploy':lambda *a:None,'parsed_config':lambda *a:True,
                'environment':lambda saved,mode,install,cfg:{'LD_PRELOAD':str(install/'lib/libucp.so.0')}}
            sys.path.insert(0,str(ROOT.parent/'um_send_compare'))
            import policy,scale_policy
            attempted=[]
            def trial(engine,out,commands,evidence,item,index,capture,trial_args):
                group,b,ppn,size,phase,repeat,profile=item;attempted.append(item)
                env=osu.environment({},'rm',osu.DEFAULT_CASE/'install',osu.budget(ppn))
                self.assertEqual(env['UCX_RM_URMA_RX_DEPTH'],'4096')
                variant='full' if osu.DEFAULT_CASE==full else 'nopsn'
                observed='libnopsn_wire.so' in env['LD_PRELOAD']
                self.assertFalse(phase=='timing' and observed)
                name='%s_rm_p%d_%s_%d'%(b,ppn,phase,index)
                if size!=8:name+='_b%d'%size
                run=out/name;run.mkdir()
                row={'name':name,'group':group,'benchmark':b,'ppn':ppn,'phase':phase,'repeat':repeat,'passed':True,
                    'configuration':{'environment':env,'arguments':['same'],'nonce':'c'*32},'output':{'value':2,'unit':'us'},
                    'rank_evidence':{'before_finalize_rss_sum_mib':80,'pids':{i:1000+i for i in range(2*ppn)}},
                    'jettys':{i:{'peak_per_process':1} for i in range(2*ppn)},'effective_config':{'node89':{'RX_DEPTH':'4096'},'node97':{'RX_DEPTH':'4096'}}}
                for rank,pid in row['rank_evidence']['pids'].items():
                    for point in ('after_init','before_finalize'):(run/('rank%d_%s.maps'%(rank,point))).write_text('libnopsn_wire.so' if observed else 'no_observer')
                    if observed:n.dump(run/('nopsn_wire_rank%d_pid%d.json'%(rank,pid)),{'rank':rank,'pid':pid,'nonce':'c'*32,'schema':1,'errors':0,'data_success':1,'short_success':1,'header_mask':1<<(8 if variant=='full' else 6),'max_wr_bytes':24,'retry_success':0})
                if fail and ppn==128 and variant=='nopsn':row.update(passed=False,error='CPU_SIMULATED_FAILURE')
                n.dump(run/'trial.json',row);return row
            runner=SimpleNamespace(run_trial=trial)
            disks=[{'hostname':h['hostname'],'free_bytes':64*1024**3} for h in inventory]
            mod=SimpleNamespace(disk_inventory=lambda *a:disks)
            with patch.object(n,'FULL_CASE',full),patch.object(n,'FULL_MODULE',n.digest(full/'install/lib/ucx/libuct_ub.so.0.0.0')),\
                 patch.object(n,'validate_candidate',return_value=build),patch.object(n,'load_engine',return_value=(engine,policy,scale_policy,runner)),\
                 patch.object(n,'prior_checkpoint',return_value={'reviewed_mpi':255}),\
                 patch.object(n.subprocess,'check_output',return_value='CPU_LDD'),\
                 patch.object(n.os,'geteuid',return_value=0),patch.object(n.socket,'gethostname',return_value='node89'),\
                 patch.dict(sys.modules,{'run_scale_compare':mod}),\
                 patch.object(sys,'argv',['candidate',str(candidate),'--expected-commit','a'*40,'--run','--timing']),contextlib.redirect_stdout(io.StringIO()):
                if fail:
                    with self.assertRaisesRegex(RuntimeError,'CPU_SIMULATED_FAILURE'):n.main()
                else:n.main()
            out=next(candidate.glob('nopsn_accept_*'));report=json.loads((out/'results.json').read_text())
            for row in report['trials']:self.assertEqual(json.loads((out/row['name']/'trial.json').read_text()),row)
            return report,attempted

    def test_full_controller_215_and_products_probes(self):
        r,items=self.controller();self.assertEqual(len(items),215);self.assertEqual(len(r['comparisons']),14)
        self.assertEqual(r['status'],'SELECTED_NOPSN_NORMAL_PATH_COMPARISON_COMPLETE_REVIEW_REQUIRED')
        for row in r['trials']:
            self.assertEqual(row['module_sha256'],r['products'][row['variant']]['module_sha256'])
            self.assertEqual(row['source_commit'],r['products'][row['variant']]['source_commit'])

    def test_failure_stops_before_160(self):
        r,items=self.controller(True);self.assertEqual(r['status'],'STOPPED_INCOMPLETE')
        self.assertEqual(max(x[2] for x in items),128)
        self.assertFalse(r['trials'][-1]['passed'])

if __name__=='__main__':unittest.main()
