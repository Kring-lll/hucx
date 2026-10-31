import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch
from test_resume import fixture as first_fixture

ENTRY = Path(__file__).resolve().parents[1] / 'resume_large_log.py'
spec = importlib.util.spec_from_file_location('large_log_resume', ENTRY)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def fixture(root):
    case, first, normal, _, _ = first_fixture(root)
    _, cp = r.prior.checkpoint(case, first)
    checkpoint = case / 'rkey_resume'
    checkpoint.mkdir()
    boundary = case / 'boundary/results.json'
    boundary.parent.mkdir()
    boundary.write_text(json.dumps({'product_commit': r.prior.COMMIT,
        'module_sha256': r.prior.MODULE, 'cases':[{'passed':True}]*18}))
    trials = [{'passed':True,'comparison_label':label,'output':{'latency_us':2.2}}
              for label in ('old_rm','new_rm','rc') for _ in range(5)]
    timing = {label:{'samples_us':[2.2]*5} for label in ('old_rm','new_rm','rc')}
    directory = checkpoint / 'comparison'
    directory.mkdir()
    (directory / 'comparison_trials.json').write_text(json.dumps(trials))
    tasks = [('WINDOW', item) for item in r.window.cases()]
    tasks += [('AM_RNDV', item) for item in r.am.cases()]
    matrix_root = checkpoint / 'window_and_am'
    matrix_root.mkdir()
    rows = [{'name':r.am.trial_directory(matrix_root,item,i).name,
             'family':family,'matrix_case':item,'passed':i<17,'exit':0}
            for i,(family,item) in enumerate(tasks[:18])]
    rows[-1].update(error='ValueError: missing/nonregular/oversized actual application UCX log',
                    cleanup={'local':{'terminated_own_pids':[]},'peer':{'terminated_own_pids':[]}})
    matrix = {'source_commit':r.prior.COMMIT,'module_sha256':r.prior.MODULE,'cases':rows}
    (matrix_root / 'results.json').write_text(json.dumps(matrix))
    old = {'source_commit':r.prior.COMMIT,'module_sha256':r.prior.MODULE,
           'status':'STOPPED_INCOMPLETE','error':rows[-1]['error'],'checkpoint':cp,
           'boundary_matrix':str(boundary),'timing':timing}
    (checkpoint / 'results.json').write_text(json.dumps(old))
    return case, checkpoint, matrix


class LargeLogResumeTests(unittest.TestCase):
    def test_checkpoint_accepts_exact_prior_gates_and_log_only_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            case, root, _ = fixture(Path(temp))
            old, matrix, receipt = r.checkpoint(root, case)
            self.assertEqual(receipt['passed_window_tasks'],10)
            self.assertEqual(receipt['passed_am_tasks'],7)
            self.assertEqual(receipt['timing_trials'],15)
            self.assertEqual(len(matrix['cases']),18)

    def test_checkpoint_rejects_old_data_failures_or_incomplete_workloads(self):
        with tempfile.TemporaryDirectory() as temp:
            case, root, original = fixture(Path(temp))
            for kind in ('early_fail','exit','duplicate','cleanup'):
                bad = json.loads(json.dumps(original))
                if kind == 'early_fail':
                    bad['cases'][0]['passed'] = False
                elif kind == 'exit':
                    bad['cases'][-1]['exit'] = 134
                elif kind == 'duplicate':
                    bad['cases'][1]['name'] = bad['cases'][0]['name']
                else:
                    bad['cases'][-1]['cleanup']['local']['terminated_own_pids'] = [99]
                (root / 'window_and_am/results.json').write_text(json.dumps(bad))
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    r.checkpoint(root, case)

    def run_remaining(self, failed):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root = Path(temp)
            case = root / 'case'
            build = {'source_commit':r.prior.COMMIT,'module_sha256':r.prior.MODULE}
            previous = r.osu.DEFAULT_CASE, r.osu.PRODUCT_COMMIT, r.osu.PRODUCT_MODULE
            calls = []
            def trial(out, commands, evidence, benchmark, mode, ppn, phase, index, args, **kwargs):
                self.assertEqual(r.osu.DEFAULT_CASE,case)
                self.assertEqual(kwargs['message_bytes'],65536)
                calls.append((benchmark,ppn,index))
                return {'name':'case%d'%index,'passed':not failed,'error':'injected'}
            stack.enter_context(patch.object(r.osu,'validate_package'))
            stack.enter_context(patch.object(r.osu,'saved_spec',return_value=([],{})))
            stack.enter_context(patch.object(r.osu,'preflight',return_value={
                'inventory':[{'available_cores':4},{'available_cores':4}]}))
            stack.enter_context(patch.object(r.osu,'one_trial',side_effect=trial))
            stack.enter_context(patch.object(r.window,'check_help'))
            stack.enter_context(patch.object(r.window,'prepare_helpers',return_value=root/'probe.so'))
            stack.enter_context(patch.object(r.window,'finish_trial'))
            stack.enter_context(patch.object(r.am,'finish_protocol'))
            with contextlib.redirect_stdout(io.StringIO()):
                if failed:
                    with self.assertRaisesRegex(ValueError,'injected'):
                        r.remaining_am(root/'remaining',case,build)
                else:
                    r.remaining_am(root/'remaining',case,build)
            self.assertEqual(previous,(r.osu.DEFAULT_CASE,r.osu.PRODUCT_COMMIT,r.osu.PRODUCT_MODULE))
            return calls

    def test_remaining_only_runs_bibw_and_multi_lat_never_repeats_1MiB(self):
        self.assertEqual(self.run_remaining(False),[('osu_bibw',1,18),('osu_multi_lat',4,19)])

    def test_remaining_failure_stops_next_task_and_restores_bindings(self):
        self.assertEqual(self.run_remaining(True),[('osu_bibw',1,18)])


    def test_existing_1MiB_review_checks_full_stream_and_never_rewrites_failed_trial(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case = root / 'case'
            checkpoint = case / 'checkpoint'
            out = checkpoint / 'window_and_am'
            item = r.am.cases()[7]
            run = r.am.trial_directory(out,item,17)
            run.mkdir(parents=True)
            mpi = root / 'mpi.so'
            mpi.write_bytes(b'cpu fixture only')
            library = out / 'libsend_probe.so'
            library.write_bytes(b'cpu observer fixture')
            ranks = {'pids':{0:123,1:456}}
            row = {'name':run.name,'exit':0,'passed':False,'ppn':1,
                   'configuration':{'nonce':'test','environment':{
                       'UCX_LOG_FILE':str(run/'ucx_%h_%p.log')}},
                   'rank_evidence':r.normalized(ranks),
                   'error':'ValueError: missing/nonregular/oversized actual application UCX log'}
            source = run / 'trial.json'
            source.write_text(json.dumps(row))
            original = source.read_bytes()
            (run/'launcher.log').write_text('# OSU MPI Bandwidth Test v7.5.2\n# Datatype: MPI_CHAR.\n1048576 427.70 Pass\n')
            config = '\n'.join(key+'='+value for key,value in r.am.settings(item).items())
            config += '\nUCX_RM_URMA_SEG_SIZE=4096\nUCX_RC_URMA_SEG_SIZE=8192\nUCX_UM_URMA_SEG_SIZE=8192\n'
            for host in ('node89','node97'):
                (run/(host+'_ucx_config.txt')).write_text(config)
            sys.path.insert(0,str(ENTRY.parents[1]/'am_rndv/tests'))
            from test_protocol import trace
            local,peer = trace(0,size=1048576),trace(1,size=1048576)
            (run/'ucx_node89_123.log').write_text(local)
            (run/'ucx_node97_456.log').write_text(peer)
            manifest = {'ucx_node97_456.log':{'size':len(peer.encode()),
                         'sha256':r.osu.digest(run/'ucx_node97_456.log')}}
            matrix = {'cases':[row],'probe_sha256':{'node89':r.osu.digest(library)},
                      'preflight':{'inventory':[{'hostname':'node89'},{'hostname':'node97'}],
                          'benchmarks':{'osu_bw':{'mpi_library':str(mpi),
                                                'mpi_library_sha256':r.osu.digest(mpi)}}}}
            destination = root / 'recheck.json'
            with patch.object(r.osu,'inspect_ranks',return_value=ranks) as inspect, \
                    patch.object(r.window.wr,'inspect_trial',return_value=[{'max_send_bytes':4096}]*2), \
                    patch.object(r.osu.base,'ssh',return_value=json.dumps(manifest)):
                checked = r.review_large(matrix,destination,root=checkpoint,case=case)
            inspect.assert_called_once()
            self.assertTrue(checked['passed'])
            self.assertEqual(checked['protocol_path']['path'],'am_rndv')
            self.assertEqual(len(checked['protocol_log_files']),2)
            self.assertEqual(source.read_bytes(),original)
            self.assertTrue(destination.is_file())


if __name__ == '__main__':
    unittest.main()
