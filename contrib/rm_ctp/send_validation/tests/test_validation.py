"""CPU contracts: reject false passes, preserve failure evidence and phase boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import validation_policy as policy
import run_validation as runner
import failure


def output(benchmark='osu_multi_lat', row='8 2.5 Pass'):
    return '# ' + policy.BENCHMARKS[benchmark][1] + ' v7.5.2\n# Datatype: MPI_CHAR.\n' + row + '\n'


class ParserTests(unittest.TestCase):
    def test_latency_bandwidth_and_collective_units(self):
        for name, row, unit in (
            ('osu_multi_lat','8 2.5 Pass','us'),
            ('osu_alltoall','8 3 2 4 20 Pass','us'),
            ('osu_allgather','8 3 2 4 20 Pass','us'),
            ('osu_bw','8 1200 Pass','MB/s'),
            ('osu_bibw','8 2400 Pass','MB/s')):
            with self.subTest(name=name):
                self.assertEqual(policy.parse_output(output(name,row),name,True,20)['unit'],unit)

    def test_diagnostic_noise_not_accepted_as_result(self):
        bad = ('8 2.5 Pass\n65536 data_len 4077 offset 32616 last: no',
               '8 2.5 Pass\n8 2.6 Pass', '8 nan Pass', '8 -1 Pass', '8 2.5 Fail',
               '64 2.5 Pass', '8 2.5 Pass\nUCX ERROR broken')
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.parse_output(output(row=value),'osu_multi_lat',True,20)

    def test_wrong_iteration_and_full_statistics_rejected(self):
        for row in ('8 3 4 2 20 Pass','8 3 2 4 19 Pass','8 3 2 inf 20 Pass'):
            with self.assertRaises(ValueError):
                policy.parse_output(output('osu_alltoall',row),'osu_alltoall',True,20)

    def test_timing_has_no_validation_and_bw_fixed_window(self):
        args = policy.arguments('osu_bw',1000,100,False,65536)
        self.assertNotIn('--validation',args)
        self.assertEqual(args[-2:],['--window-size','8'])
        self.assertEqual(policy.parse_output(output('osu_bw','65536 2000'),'osu_bw',False,1000,65536)['value'],2000)


class FailureTests(unittest.TestCase):
    def logs(self, fault='drop_data', limit='retry_limit'):
        expected = fault != 'none'
        retries = 3 if expected and limit == 'retry_limit' else 0
        values = {}
        for role in ('sender','receiver'):
            callbacks = 2 if expected and role == 'sender' else 0
            received = int(role == 'receiver' and fault != 'drop_data')
            data = (retries+1) if role == 'receiver' and fault == 'drop_data' else 0
            ack = (retries+1) if role == 'sender' and fault == 'drop_ack' else 0
            values[role] = ('SEND_FAILURE_PROBE_PASS ROLE=%s PROFILE=%s CALLBACKS=%d '
                'RECEIVED=%d ELAPSED=0.900000 CLOSED=YES\n'
                'FAILURE_INJECTION CALLS=4 DATA=%d ACK=%d\n' % (role,fault,callbacks,received,data,ack))
        values['sender'] += 'RM reliable retry peer=0 psn=1 attempt=1\n' * retries
        return values

    def test_all_six_control_and_failure_profiles(self):
        for case in policy.failure_cases():
            with self.subTest(case=case):
                value = failure.inspect(self.logs(case['fault'],case['limit']),case['fault'],case['limit'])
                self.assertIn('expected_error',value)

    def test_no_fault_trigger_or_callback_cannot_pass(self):
        for old,new in (('CALLBACKS=2','CALLBACKS=1'),('DATA=4','DATA=0'),
                        ('CLOSED=YES','CLOSED=NO'),('RECEIVED=0','RECEIVED=1')):
            logs = {k:v.replace(old,new) for k,v in self.logs().items()}
            with self.subTest(old=old), self.assertRaises(ValueError):
                failure.inspect(logs,'drop_data','retry_limit')

    def test_outer_timeout_and_early_provider_failure_not_accepted(self):
        logs = self.logs('drop_ack','deadline')
        logs['sender'] = logs['sender'].replace('ELAPSED=0.900000','ELAPSED=0.010000')
        with self.assertRaises(ValueError): failure.inspect(logs,'drop_ack','deadline')
        with self.assertRaises(ValueError): failure.inspect({'sender':'EXIT=124','receiver':''},'drop_ack','deadline')

    def test_real_injector_forwards_or_drops_36byte_ack(self):
        # Execute compiled DSO + real dlopen(RTLD_LOCAL), not a Python implementation of it.
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out/'provider.c').write_text('#include <stddef.h>\nstatic int n;\n'
                'int uct_rm_urma_reliable_receive(void *p,const void *b,size_t z)'
                '{(void)p;(void)b;(void)z;++n;return 0;}\nint forwarded(void){return n;}\n')
            for source,target,extra in ((out/'provider.c',out/'provider.so',[]),
                                       (ROOT/'drop_all.c',out/'inject.so',['-ldl'])):
                subprocess.run(['gcc','-std=gnu99','-Wall','-Wextra','-Werror','-shared','-fPIC',
                                str(source),*extra,'-o',str(target)],check=True)
            code = '''import ctypes,os,struct
provider=ctypes.CDLL(os.environ['HUCX_SEND_TEST_MODULE'],mode=os.RTLD_LOCAL|os.RTLD_NOW)
shim=ctypes.CDLL(os.environ['SHIM']);f=shim.uct_rm_urma_reliable_receive
f.argtypes=(ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t)
for kind,size in ((1,168),(2,36)):
 b=bytearray(size);struct.pack_into('<I',b,0,0x33524d53);b[4]=3;b[5]=kind
 p=ctypes.create_string_buffer(bytes(b));assert f(None,p,size)==0
assert provider.forwarded()==int(os.environ['EXPECTED'])
'''
            for profile,expected in (('none',2),('drop_data',1),('drop_ack',1)):
                env=dict(os.environ,HUCX_SEND_TEST_MODULE=str(out/'provider.so'),SHIM=str(out/'inject.so'),
                         HUCX_SEND_FAILURE_PROFILE=profile,EXPECTED=str(expected))
                result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('FAILURE_INJECTION CALLS=2',result.stderr)


class RunnerContracts(unittest.TestCase):
    def test_timing_profile_omits_diagnostic_and_fault_libraries(self):
        for profile in policy.MODES:
            env=runner.profile_environment(Path('/accepted'),4,{},profile,'eager',Path('/run'))
            self.assertEqual(env['UCX_LOG_LEVEL'],'warn')
            self.assertNotIn('probe.so',env['LD_PRELOAD'])
            self.assertNotIn('drop.so',env['LD_PRELOAD'])
            self.assertEqual(env['UCX_RM_URMA_NATIVE_RMA'],'n')
            self.assertEqual(env['UCX_RM_URMA_LINK_MODE'],'reliable_v3' if profile=='reliable_v3' else 'iface_v2')
            self.assertEqual(env['UCX_RC_URMA_SEG_SIZE'],'8192')

    def test_registry_and_config_restored_on_failure(self):
        before=runner.osu.BENCHMARKS,runner.osu.arguments,runner.osu.parse_output,runner.osu.base.parsed_config
        with self.assertRaises(RuntimeError):
            with runner.registered_runner(Path('/accepted'),{'source_commit':'a'*40,'module_sha256':'b'*64}):
                self.assertEqual(set(runner.osu.BENCHMARKS),set(policy.BENCHMARKS))
                raise RuntimeError('intentional')
        self.assertEqual(before,(runner.osu.BENCHMARKS,runner.osu.arguments,runner.osu.parse_output,runner.osu.base.parsed_config))

    def test_rc_validation_does_not_require_rm_4k_wr_observer(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            item=dict(benchmark='osu_alltoall',profile='rc',ppn=1,size=8,path='eager')
            row=dict(name='test',passed=True,configuration={})
            with mock.patch.object(runner.osu,'one_trial',return_value=row), \
                 mock.patch.object(runner.integration.wr_probe,'fetch_peer_summaries') as fetch:
                value=runner.run_trial(Path('/accepted'),out,{}, {}, {}, item,'validation',0)
            self.assertTrue(value['passed'])
            fetch.assert_not_called()

    def exercise(self, run=True, timing=False, fail_at=None):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            helpers={name:out/(name+'.so') for name in ('wr_probe','injector','mpi_probe')}
            for path in helpers.values(): path.write_bytes(b'CPU contract fixture')
            for name in ('libosu_identity.so','failure_probe','failure.so'): (out/name).write_bytes(b'CPU fixture')
            scheduled=[]
            def trial(case,root,commands,evidence,tools,item,phase,index,repeat=0,inject=False):
                scheduled.append(('mpi',item['profile'],phase))
                return dict(item,name='trial_%d'%index,passed=len(scheduled)!=fail_at,
                    error='CPU failure',production_retransmit_posts=4)
            def fault(*args):
                scheduled.append(('uct',args[-1]['fault'],'failure'))
                return dict(name='fault',passed=len(scheduled)!=fail_at,error='CPU failure',
                            evidence={'production_retry_posts':0})
            with mock.patch.object(runner,'prior_checkpoint',return_value={}), \
                 mock.patch.object(runner.osu,'validate_package'), \
                 mock.patch.object(runner.osu,'saved_spec',return_value=([],{})), \
                 mock.patch.object(runner.osu,'preflight',return_value={}), \
                 mock.patch.object(runner,'config_capture',return_value={}), \
                 mock.patch.object(runner.osu.base,'ssh'), \
                 mock.patch.object(runner.integration,'compile_helpers',return_value=helpers), \
                 mock.patch.object(runner.failure,'compile_probe',return_value=(out/'failure_probe',out/'failure.so')), \
                 mock.patch.object(runner.integration,'copy_checked'), \
                 mock.patch.object(runner,'run_trial',side_effect=trial), \
                 mock.patch.object(runner.failure,'run',side_effect=fault), \
                 mock.patch.object(runner.policy,'compare',side_effect=lambda rows,item:dict(item,profiles={})):
                # Preflight checks the actual help text, including bandwidth window option.
                for name in ('osu_allgather','osu_bw','osu_bibw'):
                    (out/(name+'_help.txt')).write_text('--full --window-size')
                if fail_at:
                    with self.assertRaises(RuntimeError):
                        runner.execute(Path('/accepted'),out,{'source_commit':'a','module_sha256':'b'},run,timing)
                else:
                    runner.execute(Path('/accepted'),out,{'source_commit':'a','module_sha256':'b'},run,timing)
            return json.loads((out/'results.json').read_text()),scheduled

    def test_preflight_has_no_two_node_tasks(self):
        report,scheduled=self.exercise(run=False)
        self.assertEqual(scheduled,[])
        self.assertEqual(report['status'],'PREFLIGHT_PASS')

    def test_functional_count_and_no_unrequested_performance(self):
        report,scheduled=self.exercise()
        self.assertEqual(len(report['trials']),23)
        self.assertEqual(len(report['failures']),6)
        self.assertEqual(len(scheduled),29)
        self.assertFalse(any(v[2]=='timing' for v in scheduled))

    def test_full_schedule_and_first_failure_stop(self):
        report,scheduled=self.exercise(timing=True)
        self.assertEqual(len(scheduled),155)
        self.assertEqual(sum(v[2]=='timing' for v in scheduled),90)
        self.assertEqual(sum(v[2]=='resources' for v in scheduled),18)
        failed,calls=self.exercise(timing=True,fail_at=3)
        self.assertEqual(len(calls),3)
        self.assertEqual(failed['status'],'STOPPED_INCOMPLETE')
        self.assertEqual(len(failed['trials']),3)

class ComparisonTests(unittest.TestCase):
    def trials(self):
        rows=[]
        for profile in policy.MODES:
            common=dict(benchmark='osu_multi_lat',ppn=1,size=8,path='eager',profile=profile,passed=True,
                        configuration={'arguments':['same-workload']},run='raw/' + profile)
            rows.append(dict(common,phase='validation',output={'validation_pass':True}))
            for repeat,value in enumerate((2,2.1,9,2.2,2.3)):
                rows.append(dict(common,phase='timing',repeat=repeat,output={'value':value},
                                 rank_evidence={'before_finalize_rss_sum_mib':90+repeat}))
            rows.append(dict(common,phase='resources',output={'value':4},
                rank_evidence={'before_finalize_rss_sum_mib':95},
                jettys={0:{'peak_per_process':1},1:{'peak_per_process':2}}))
        return rows

    def test_outlier_preserved_and_resource_round_not_used_as_timing(self):
        group=policy.compare(self.trials(),policy.timing_cases()[0])
        value=group['profiles']['reliable_v3']
        self.assertEqual(value['metric']['samples'],[2,2.1,9,2.2,2.3])
        self.assertEqual(value['metric']['median'],2.2)
        self.assertEqual(value['metric']['maximum'],9)
        self.assertEqual(value['jetty_peak_range'],[1,2])
        self.assertEqual(value['rss_mib']['median'],92)
        self.assertEqual(group['reliable_changes_pct']['rc']['metric'],0)

    def test_failed_or_incomplete_group_cannot_create_comparison(self):
        rows=self.trials()
        rows[2]['passed']=False
        with self.assertRaises(ValueError): policy.compare(rows,policy.timing_cases()[0])
        with self.assertRaises(ValueError): policy.compare(self.trials()[:-1],policy.timing_cases()[0])


if __name__ == '__main__':
    unittest.main()
