import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import resume_probe as r
spec=importlib.util.spec_from_file_location('probe_runner_test',HERE/'run_candidate.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ProbeResumeTests(unittest.TestCase):
    def report(self):
        rows=[]
        for i,(p,b,k,n,f) in enumerate(m.plan()[:14]):
            rows.append({'profile':p,'benchmark':b,'ppn':k,'software_fault':f,'phase':'validation',
                'configuration':{'arguments':['--message-size','%d:%d'%(n,n)]},'exit':0,
                'output':{'validation_pass':True},'passed':i<13,
                **({'error':'ValueError: protocol receipt identity/coverage failed'} if i==13 else {})})
        return {'source_commit':r.guard.COMMIT,'module_sha256':r.guard.MODULE_SHA,'status':'STOPPED_INCOMPLETE',
            'cases':rows,'timing':[],'controller_timefix':{'fixed_runner_sha256':'c36157064a0c389f0023ea808a4b96c5cc6b742e4320506faf5daa007ab8d612'}}
    def test_review_only_13_not_failed_1MiB(self):
        self.assertEqual(len(r.checkpoint_shape(self.report(),m.plan())),13)
    def test_product_or_controller_changed(self):
        for key in ('source_commit','module_sha256','controller_timefix'):
            data=self.report();data[key]='wrong'
            with self.subTest(key=key),self.assertRaises((ValueError,AttributeError)):
                r.checkpoint_shape(data,m.plan())
    def test_wrong_task_or_cleanup_rejected(self):
        for key,value in (('passed',False),('exit',1),('cleanup_error','remaining'),('ppn',4),('phase','timing')):
            data=self.report();data['cases'][2][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):r.checkpoint_shape(data,m.plan())
    def test_bad_stop_and_coverage_rejected(self):
        for alter in (lambda d:d['cases'].pop(),lambda d:d['cases'][-1].update(passed=True),
                lambda d:d['cases'][-1].update(error='network failed'),lambda d:d['timing'].append({})):
            data=self.report();alter(data)
            with self.assertRaises(ValueError):r.checkpoint_shape(data,m.plan())
    def test_wrong_size_rejected(self):
        data=self.report();data['cases'][2]['configuration']['arguments']=['--message-size','999:999']
        with self.assertRaises(ValueError):r.checkpoint_shape(data,m.plan())
    def protocol(self,rank):
        return {'schema':2,'retry_method':'PINNED_UM_SGE_LAYOUT','rank':rank,'pid':100+rank,
            'nonce':'a'*32,'header_bytes':8,'data_attempts':200006,'data_success':200006,
            'retry_data_posts':2,'unknown_data_layout':0,'duplicate_failed':0}
    def check(self,mutate=None):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);a=self.protocol(0);b=self.protocol(1)
            if mutate:mutate(a)
            (root/'protocol_rank0_pid100.json').write_text(json.dumps(a))
            peer={'protocol_rank1_pid101.json':json.dumps(b)}
            row={'ppn':1,'rank_evidence':{'pids':{0:100,1:101}},'configuration':{'nonce':'a'*32}}
            with mock.patch.object(m.osu.base,'ssh',return_value=json.dumps(peer)):
                return m.fetch_protocol(root,row)
    def test_checkpoint_io_keeps_original_rows_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            case=Path(folder)/'candidate';root=case/'prior';root.mkdir(parents=True)
            probe=root/'libum_send_probe.so';probe.write_bytes(b'CPU_OBSERVER_FIXTURE')
            data=self.report();data['tool_hashes']={'libum_send_probe.so':r.guard.digest(probe)}
            data['preflight']={'inventory':[], 'benchmarks':{'osu_multi_lat':{'mpi_library':'/fixture/libmpi.so'}}}
            specs=m.plan();observed=[{'lengths':[24],'inline_wrs':1,'rank':k} for k in (0,1)]
            for i,row in enumerate(data['cases']):
                row['name']='fixture_'+str(i);row['iterations']=100
                row['configuration'].update(nonce='a'*32)
                row['rank_evidence']={'pids':{'0':100,'1':101}}
                m.PROFILE=row['profile'];env=m.environment({},'iface',case/'install',{})
                row['configuration']['environment']=env
                text='\n'.join(k+'='+v for k,v in env.items() if k.startswith('UCX_'))+'\nUCX_RM_URMA_SEG_SIZE=4K\n'
                text=text.replace('TIMEOUT=10s','TIMEOUT=10000000.00us')
                run=root/row['name'];run.mkdir()
                for host in ('node89','node97'):(run/(host+'_ucx_config.txt')).write_text(text)
                log='# OSU MPI Multi Latency Test v7.5.2\n%d 2.0 Pass\n'%specs[i][3]
                (run/'launcher.log').write_text(log)
                row['output']=m.osu.parse_output(log,'osu_multi_lat',True,100,specs[i][3])
                if row['profile']=='rm':
                    row['send_observations']=observed
                    for rank in (0,1):
                        old={'rank':rank,'pid':100+rank,'nonce':'a'*32,'header_bytes':8,
                            'key_overflow':0,'duplicate_failed':0,'data_drop':0,'ack_drop':0,'duplicate':0}
                        (run/('protocol_rank%d_pid%d.json'%(rank,100+rank))).write_text(json.dumps(old))
                (run/'trial.json').write_text(json.dumps(row))
            path=root/'results.json';path.write_text(json.dumps(data));before=path.read_bytes()
            with mock.patch.object(r,'PRIOR',root),mock.patch.object(r.guard,'CASE',case),mock.patch.object(m.osu,'inspect_ranks',return_value={'pids':{0:100,1:101}}),mock.patch.object(m.wr,'inspect_trial',return_value=observed):
                result=r.review_checkpoint(m.__dict__)
                self.assertEqual(result['reused_passed_tasks'],13)
                self.assertFalse(result['legacy_retry_numbers_used'])
                self.assertFalse(result['prior_1MiB_promoted_to_pass'])
                self.assertEqual(path.read_bytes(),before)
                p=root/'fixture_0/trial.json';p.write_text('{}')
                with self.assertRaisesRegex(ValueError,'trial differs'):r.review_checkpoint(m.__dict__)
            m.PROFILE='rm'
    def test_new_receipts_accept_multiple_wraps(self):
        self.assertEqual(len(self.check()),2)
    def test_unknown_layout_or_wrong_identity_rejected(self):
        for key,value in (('unknown_data_layout',1),('rank',1),('nonce','b'*32),('schema',1),
                ('retry_method','PSN_HISTORY'),('data_success',0),('data_success',2),('retry_data_posts',-1)):
            with self.subTest(key=key),self.assertRaises(ValueError):self.check(lambda a:a.update({key:value}))

if __name__=='__main__':unittest.main()
