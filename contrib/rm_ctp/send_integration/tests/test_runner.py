"""Contracts against the reused OSU runner, with all device calls mocked."""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import matrix
import run_integration as runner


class RunnerContracts(unittest.TestCase):
    def test_candidate_binding_restored_even_on_failure(self):
        before = runner.osu.DEFAULT_CASE, runner.osu.PRODUCT_COMMIT, runner.osu.PRODUCT_MODULE, runner.osu.base.parsed_config
        with self.assertRaisesRegex(RuntimeError, 'intentional'):
            with runner.bind_runner(Path('/accepted'), {'source_commit':matrix.COMMIT,'module_sha256':matrix.MODULE}):
                self.assertEqual(runner.osu.DEFAULT_CASE, Path('/accepted'))
                self.assertEqual(runner.osu.PRODUCT_COMMIT, matrix.COMMIT)
                self.assertIs(runner.osu.base.parsed_config, runner.capture_configuration)
                raise RuntimeError('intentional')
        self.assertEqual(before, (runner.osu.DEFAULT_CASE, runner.osu.PRODUCT_COMMIT,
                                  runner.osu.PRODUCT_MODULE, runner.osu.base.parsed_config))

    def test_actual_one_trial_reaches_iface_adapter(self):
        # Execute the existing one_trial through configuration/launch construction.
        # Stop at the first mocked launcher call, without MPI, SSH, or devices.
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            case = Path('/accepted')
            invocation = []
            inventory = [{'hostname':'node89','boot_id':'a','allowed_cpus':[0,1,2,3],
                          'available_cores':4,'mem_available_kib':10**8},
                         {'hostname':'node97','boot_id':'b','allowed_cpus':[0,1,2,3],
                          'available_cores':4,'mem_available_kib':10**8}]
            commands = {'rm': (['/hmpi/bin/mpirun','-np','2','/osu_latency','-c'], {})}
            def stop(command, env, log, timeout):
                invocation.append(command)
                raise RuntimeError('intentional launcher boundary')
            config = mock.Mock(return_value={})
            with runner.bind_runner(case, {'source_commit':matrix.COMMIT,'module_sha256':matrix.MODULE}), \
                 runner.reliable_osu_profile(case, 1, out/'wr.so', out/'trial'), \
                 mock.patch.object(runner.osu.base,'ssh',return_value='{"terminated_own_pids": []}'), \
                 mock.patch.object(runner.osu.base,'inventory',return_value=inventory), \
                 mock.patch.object(runner.osu.base,'parsed_config',config), \
                 mock.patch.object(runner.osu.base,'copy_remote'), \
                 mock.patch.object(runner.osu.base,'run_logged',side_effect=stop), \
                 mock.patch.object(runner.subprocess,'check_output',return_value='{"terminated_own_pids": []}'):
                row = runner.osu.one_trial(out, commands, {'inventory':inventory}, 'osu_multi_lat','rm',1,
                                           'validation',0,argparse.Namespace(iterations=20,warmup=2,timeout=240))
            self.assertEqual(len(invocation),1)
            self.assertIn('intentional launcher boundary',row['error'])
            self.assertEqual(config.call_args.args[0],case/'install')
            self.assertEqual(config.call_args.args[3],'iface')
            self.assertEqual(row['configuration']['environment']['UCX_RM_URMA_LINK_MODE'],'reliable_v3')

    def test_bad_reused_mode_rejected_before_start(self):
        with runner.reliable_osu_profile(Path('/accepted'),1,Path('/wr.so'),Path('/run')):
            with self.assertRaises(ValueError):
                runner.osu.environment({},'rc',Path('/accepted/install'),runner.osu.budget(1))

    def test_preflight_uses_bound_97b_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            def stop(*args):
                self.assertEqual(runner.osu.DEFAULT_CASE,Path('/accepted'))
                self.assertEqual(runner.osu.PRODUCT_MODULE,matrix.MODULE)
                raise RuntimeError('intentional preflight boundary')
            with mock.patch.object(runner.osu,'validate_package'), \
                 mock.patch.object(runner.osu,'saved_spec',return_value=([],{})), \
                 mock.patch.object(runner.osu,'preflight',side_effect=stop), \
                 self.assertRaisesRegex(RuntimeError,'intentional preflight boundary'):
                runner.execute(Path('/accepted'),out,{'source_commit':matrix.COMMIT,'module_sha256':matrix.MODULE},False)
            import json
            report=json.loads((out/'results.json').read_text())
            self.assertEqual(report['status'],'STOPPED_INCOMPLETE')
            self.assertEqual(report['trials'],[])

    def test_failed_osu_keeps_original_reason_and_skips_observer(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            run=out/'osu_multi_lat_rm_p1_validation_0'
            run.mkdir()
            failed={'name':run.name,'passed':False,'error':'original timeout'}
            with mock.patch.object(runner.osu,'one_trial',return_value=failed), \
                 mock.patch.object(runner.wr_probe,'fetch_peer_summaries') as fetch:
                row=runner.run_osu(Path('/accepted'),out,{}, {},{'wr_probe':out/'wr.so'},1,8,0,'eager')
            self.assertFalse(row['passed'])
            self.assertIn('original timeout',row['error'])
            fetch.assert_not_called()

    def test_compile_once_copies_exact_binaries(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            jobs=[]
            copied=[]
            def compile_job(command, env, log, timeout):
                jobs.append(command)
                Path(command[command.index('-o')+1]).write_bytes(b'ELF-test')
            with mock.patch.object(runner.osu.base,'run_logged',side_effect=compile_job), \
                 mock.patch.object(runner,'copy_checked',side_effect=lambda files, dst:copied.extend(files)), \
                 mock.patch.object(runner.subprocess,'check_output',return_value='libc.so => /lib/libc.so'), \
                 mock.patch.object(runner.osu.base,'ssh',return_value='libc.so => /lib/libc.so'):
                files=runner.compile_helpers(Path('/accepted'),out,{'rm':(['/hmpi/bin/mpirun'],{})})
            self.assertEqual(len(jobs),3)
            self.assertEqual(set(copied),set(files.values()))
            self.assertTrue(all('-g' in command for command in jobs))


if __name__=='__main__':
    unittest.main()

class FullScheduleContracts(unittest.TestCase):
    def exercise(self, fail_name=None):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            helpers={name:out/(name+'.so') for name in ('wr_probe','injector','mpi_probe')}
            for path in helpers.values(): path.write_bytes(b'CPU-fake-binary')
            (out/'libosu_identity.so').write_bytes(b'CPU-fake-identity')
            scheduled=[]
            def osu_trial(case, output, commands, evidence, tools, ppn, size, index, path):
                row={'name':'osu_%d_%d_%s'%(ppn,size,path),'passed':True}
                scheduled.append(row['name'])
                return row
            def custom(case, output, commands, evidence, tools, ppn, injection):
                name='custom_%d_%s'%(ppn,injection)
                scheduled.append(name)
                return {'name':name,'passed':name!=fail_name,'error':'injected test failure',
                        'production_retransmit_posts':12}
            with mock.patch.object(runner.osu,'validate_package'), \
                 mock.patch.object(runner.osu,'saved_spec',return_value=([],{})), \
                 mock.patch.object(runner.osu,'preflight',return_value={}), \
                 mock.patch.object(runner,'capture_configuration',return_value={}), \
                 mock.patch.object(runner.osu.base,'ssh'), \
                 mock.patch.object(runner,'compile_helpers',return_value=helpers), \
                 mock.patch.object(runner,'copy_checked'), \
                 mock.patch.object(runner,'run_osu',side_effect=osu_trial), \
                 mock.patch.object(runner,'run_custom',side_effect=custom):
                if fail_name:
                    with self.assertRaises(RuntimeError):
                        runner.execute(Path('/accepted'),out,{'source_commit':matrix.COMMIT,'module_sha256':matrix.MODULE},True)
                else:
                    runner.execute(Path('/accepted'),out,{'source_commit':matrix.COMMIT,'module_sha256':matrix.MODULE},True)
            import json
            return json.loads((out/'results.json').read_text()), scheduled

    def test_all_phases_scheduled_without_rc_or_rma(self):
        report,scheduled=self.exercise()
        self.assertEqual(len(scheduled),40)
        self.assertEqual(report['status'],'SELECTED_RELIABLE_SEND_INTEGRATION_PASS')
        self.assertEqual(scheduled[-1],'custom_1_True')
        self.assertEqual(sum('_am_rndv' in name for name in scheduled),6)

    def test_failed_alternation_stops_before_expanding(self):
        report,scheduled=self.exercise('custom_1_False')
        self.assertEqual(len(scheduled),11)
        self.assertEqual(report['status'],'STOPPED_INCOMPLETE')
        self.assertFalse(report['trials'][-1]['passed'])
        self.assertNotIn('custom_2_False',scheduled)
