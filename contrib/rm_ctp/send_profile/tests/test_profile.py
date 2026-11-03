"""Actual DSO forwarding/phase tests + reject false-positive parsing/identity."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import perf_agent
import run_profile
import node_entry


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.out=Path(cls.temp.name)
        def cc(sources,name,options):
            subprocess.run(['gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O2','-g',
                            '-I'+str(ROOT/'tests'),*[str(s) for s in sources],'-o',str(cls.out/name),*options],check=True)
        cc([ROOT/'tests/provider.c'],'liburma.so',['-shared','-fPIC'])
        cc([ROOT/'tests/mpi_provider.c'],'libfakempi.so',['-shared','-fPIC'])
        cc([ROOT/'phase.c',ROOT/'counters.c'],'libprofile.so',['-shared','-fPIC','-ldl','-pthread'])
        cc([ROOT/'phase.c'],'libphase.so',['-shared','-fPIC','-DPROFILE_PHASE_ONLY','-ldl'])
        cc([ROOT/'tests/client.c'],'client',['-L'+str(cls.out),'-Wl,-rpath,'+str(cls.out),'-lfakempi','-ldl'])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def invoke(self,**extra):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(os.environ,LD_PRELOAD=str(self.out/'libprofile.so'),
                     HUCX_OSU_PROBE_DIR=directory,OMPI_COMM_WORLD_RANK='0',A89_RUN_NONCE='a'*32)
            env.update(extra)
            subprocess.run([str(self.out/'client'),str(self.out/'liburma.so')],env=env,check=True,timeout=5)
            paths=list(Path(directory).glob('profile_rank0_pid*.json'))
            self.assertEqual(len(paths),3)
            final=next(p for p in paths if not p.stem.endswith(('_ready','_end')))
            value=json.loads(final.read_text())
            end=json.loads(final.with_name(final.stem+'_end.json').read_text())
            self.assertEqual(value['at_ns'],end['at_ns'])
            return value

    def test_local_provider_exact_forwarding_flags_and_replay(self):
        row=self.invoke()
        run_profile.inspect_counter(row,0,row['pid'],'a'*32,'reliable_v3')
        self.assertEqual(sum(r['success'] for r in row['send_flags']),4)
        self.assertEqual(row['reliable_data'],3)
        self.assertEqual(row['reliable_ack'],1)
        self.assertEqual(row['repeated_successful_data_psn'],1)
        self.assertEqual(row['process_cqes'],2)
        self.assertEqual(row['rm_send_bytes'],512)

    def test_error_errno_and_no_success_prefix_inference(self):
        row=self.invoke(PROVIDER_ERROR='1')
        self.assertEqual(row['error_calls'],4)
        self.assertEqual(row['rm_send_bytes'],0)
        self.assertEqual(sum(r['success'] for r in row['send_flags']),0)
        with self.assertRaises(ValueError):
            run_profile.inspect_counter(row,0,row['pid'],'a'*32,'reliable_v3')

    def test_init_thread_phase(self):
        self.assertTrue(self.invoke(INIT_THREAD='1')['finalized'])


class ContractTests(unittest.TestCase):
    def test_perf_parser_phase_pid_and_flat_inclusive(self):
        text=''' 123 10.500000001: cpu-clock:u:
  aabb uct_rm_urma_reliable_progress+0x20 (/x/libuct_ub.so)
  bbcc opal_progress (/x/libopen-pal.so)

 124 10.600000001: cpu-clock:u:
  aabb irrelevant (/x/other.so)

 123 11.500000001: cpu-clock:u:
  aabb MPI_Finalize (/x/libmpi.so)
'''
        result=perf_agent.summarize(text,123,10000000000,11000000000)
        self.assertEqual(result['samples'],1)
        self.assertEqual(result['status'],'INSUFFICIENT_SAMPLES')
        self.assertEqual(result['flat_top'][0]['symbol'],'uct_rm_urma_reliable_progress+0x20')
        self.assertEqual(result['inclusive_category_samples'],{'reliable':1,'poll_progress':1})

    def test_perf_malformed_output_not_a_success(self):
        self.assertEqual(perf_agent.summarize('garbage',1,0,10)['status'],'INSUFFICIENT_SAMPLES')

    def test_pid_identity_reuse_and_wrong_nonce_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError,OSError)):
                perf_agent.identity(os.getpid(),sys.executable,'b'*32,Path(directory))

    def test_checkpoint_wrong_hash_stops_before_parsing(self):
        with patch.object(run_profile.integration,'digest',return_value='wrong'),self.assertRaises(ValueError):
            run_profile.checkpoint()

    def test_plan_is_bounded_and_not_formal_performance(self):
        plan=run_profile.plan()
        self.assertEqual((plan['validation_tasks'],plan['counter_tasks'],plan['optional_sample_tasks']),(4,4,4))
        self.assertFalse(plan['product_changed'])
        self.assertFalse(plan['performance_acceptance'])
        self.assertFalse(plan['rto_changed'])

    def test_sampling_command_scoped_and_does_not_use_systemwide(self):
        command=perf_agent.record_command(123,Path('/tmp/test.data'),3)
        self.assertEqual(command[command.index('-p')+1],'123')
        self.assertIn('--no-inherit',command)
        self.assertNotIn('-a',command)

    def test_unobserved_validation_and_clean_sampling_environment(self):
        # Exercise the real scoped environment callback, not a copied dictionary.
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            for phase,expected in (('validation',None),('counter','libsend_profile_counts.so'),('sample','libsend_profile_phase.so')):
                item={'benchmark':'osu_multi_lat','ppn':1,'profile':'reliable_v3'}
                def fake_trial(output,commands,evidence,benchmark,mode,ppn,actual_phase,index,args,**kw):
                    run=out/('osu_multi_lat_rm_p1_%s_0'%phase)
                    run.mkdir()
                    env=run_profile.osu.environment({},'iface',run_profile.CASE/'install',run_profile.osu.budget(1))
                    preloads=env['LD_PRELOAD'].split(':')
                    if expected is None:
                        self.assertFalse(any('libsend_profile_' in p for p in preloads))
                    else:
                        self.assertEqual(Path(preloads[0]).name,expected)
                    self.assertEqual(env['UCX_RM_URMA_LINK_MODE'],'reliable_v3')
                    self.assertEqual(env['UCX_LOG_LEVEL'],'warn')
                    self.assertEqual(args.timeout,180)
                    return {'passed':False,'error':'fixture','phase':phase}
                old=run_profile.osu.environment
                with patch.object(run_profile.osu,'one_trial',side_effect=fake_trial):
                    row=run_profile.trial(run_profile.CASE,out,{}, {},item,phase,0,True)
                self.assertFalse(row['passed'])
                self.assertIs(run_profile.osu.environment,old)

    def test_environment_binding_rejects_wrong_install(self):
        with tempfile.TemporaryDirectory() as directory:
            def fake_trial(*args,**kwargs):
                run_profile.osu.environment({},'iface',Path('/wrong'),run_profile.osu.budget(1))
            with patch.object(run_profile.osu,'one_trial',side_effect=fake_trial),self.assertRaises(ValueError):
                run_profile.trial(run_profile.CASE,Path(directory),{}, {},
                    {'benchmark':'osu_multi_lat','ppn':1,'profile':'iface_v2'},'counter',0,True)


if __name__=='__main__':
    unittest.main()
