import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import protocol as p
import recheck_last as recheck
import run_rndv as am
import split_logs as logs
from test_protocol import trace, IDENTITIES


class SplitLogTests(unittest.TestCase):
    def test_observed_fragments_reproduce_rejection_without_weakening_parser(self):
        case = p.cases()[-1]
        text = '# OSU MPI Multi Latency Test v7.5.2\n# Datatype: MPI_CHAR.\n65536 1187.29 Pass\n'
        noise = '65536 data_len 4077 offset 32616 last: no\n5536 data_len 4077 offset 32616 last: no\n'
        with self.assertRaisesRegex(ValueError, 'missing/extra/duplicate/out-of-order'):
            am.window_matrix.parse_output(p.osu_text(noise + text), case, 20)
        self.assertTrue(am.window_matrix.parse_output(text, case, 20)['validation_pass'])
        for bad in (text + '65536 1187.29 Pass\n', text.replace('65536', '65535'),
                    text.replace('Pass', 'Fail'), text.replace(' Pass', ''),
                    text.replace('1187.29', 'nan')):
            with self.assertRaises(ValueError):
                am.window_matrix.parse_output(bad, case, 20)

    def test_profile_changes_log_destination_only_and_restores(self):
        original = lambda *a, **k: {'UCX_LOG_FILE': '/old'}
        osu = SimpleNamespace(arguments=object(), parse_output=object(), environment=original)
        previous = osu.arguments, osu.parse_output, osu.environment
        case = p.cases()[-1]
        directory = logs.trial_directory(Path('/new'), case, 19)
        with self.assertRaises(RuntimeError):
            with p.trial_profile(osu, case, am.window_matrix.parse_output, log_directory=directory):
                self.assertEqual(osu.arguments('osu_multi_lat', 20, 2, True), p.arguments(case))
                env = osu.environment()
                self.assertEqual(env['UCX_LOG_FILE'], str(directory / 'ucx_%h_%p.log'))
                self.assertEqual(env['UCX_LOG_LEVEL'], 'req')
                self.assertEqual(env['UCX_LOG_FILE_SIZE'], 'inf')
                self.assertEqual(env['UCX_LOG_FILE_ROTATE'], '0')
                raise RuntimeError('injected')
        self.assertEqual((osu.arguments, osu.parse_output, osu.environment), previous)
        with self.assertRaises(ValueError):
            logs.log_pattern('relative')

    def collect(self, error=False, mismatch=False):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            run = out / 'trial'
            run.mkdir()
            row = {'name': 'trial', 'ppn': 1, 'configuration': {'environment': {
                'UCX_LOG_FILE': logs.log_pattern(run)}}, 'rank_evidence': {'pids': {0:123, 1:456}}}
            evidence = {'inventory': [{'hostname':'node89'}, {'hostname':'node97'}]}
            (run / 'ucx_node89_123.log').write_text(trace(0))
            (run / 'ucx_node97_999.log').write_text('wrapper noise')
            peer = trace(1) + ('UCX ERROR device failed\n' if error else '')
            manifest = {'ucx_node97_456.log': {'size':len(peer.encode()),
                'sha256':'0'*64 if mismatch else logs.hashlib.sha256(peer.encode()).hexdigest()}}
            def transfer(command, env, path, timeout):
                self.assertIn('root@9.88.43.97:' + str(run / 'ucx_node97_456.log'), command)
                self.assertFalse(any('999' in item for item in command))
                (run / 'ucx_node97_456.log').write_text(peer)
            base = SimpleNamespace(PEER='root@9.88.43.97', run_logged=transfer,
                                   ssh=lambda *a, **k: json.dumps(manifest))
            text, receipt = logs.collect(out, row, evidence, base)
            self.assertEqual(len(receipt), 2)
            self.assertNotIn('wrapper noise', text)
            return p.inspect_trace(text, p.cases()[8], IDENTITIES)

    def test_actual_pid_logs_keep_protocol_evidence(self):
        self.assertEqual(self.collect()['sending_ranks'], [0, 1])

    def test_separate_log_error_still_fails(self):
        with self.assertRaisesRegex(ValueError, 'runtime error'):
            self.collect(error=True)

    def test_peer_hash_mismatch_still_fails(self):
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.collect(mismatch=True)

    def test_wrong_log_pattern_or_pid_type_rejected(self):
        row = {'name':'trial', 'ppn':1, 'configuration':{'environment':{}},
               'rank_evidence':{'pids':{0:123, 1:'456'}}}
        ev = {'inventory':[{'hostname':'node89'}, {'hostname':'node97'}]}
        with self.assertRaises(ValueError):
            logs.log_identities('/new', row, ev)
        row['configuration']['environment']['UCX_LOG_FILE'] = '/new/trial/ucx_%h_%p.log'
        with self.assertRaises(ValueError):
            logs.log_identities('/new', row, ev)


    def streaming_fixture(self, root, trailing='', peer_mismatch=False):
        run = root / 'trial'
        run.mkdir()
        local = trace(0, size=1048576) + trailing
        peer = trace(1, size=1048576)
        (run / 'ucx_node89_123.log').write_text(local)
        (run / 'ucx_node97_456.log').write_text(peer)
        row = {'name': 'trial', 'ppn': 1, 'configuration': {'environment': {
            'UCX_LOG_FILE': logs.log_pattern(run)}}, 'rank_evidence': {'pids': {0:123, 1:456}}}
        evidence = {'inventory': [{'hostname':'node89'}, {'hostname':'node97'}]}
        manifest = {'ucx_node97_456.log': {'size':len(peer.encode()),
            'sha256':'0'*64 if peer_mismatch else logs.hashlib.sha256(peer.encode()).hexdigest()}}
        def no_copy(*args):
            self.fail('existing checkpoint logs must not be overwritten')
        base = SimpleNamespace(PEER='root@9.88.43.97', run_logged=no_copy,
                               ssh=lambda *a, **k: json.dumps(manifest))
        return row, evidence, base, run

    def test_streamed_actual_protocol_and_explicit_existing_log_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, ev, base, run = self.streaming_fixture(root)
            result, receipts = logs.collect(root, row, ev, base, reuse_existing=True,
                parse_lines=lambda lines: p.inspect_trace(lines, p.cases()[7], IDENTITIES))
            self.assertEqual(result['path'], 'am_rndv')
            self.assertEqual(len(receipts), 2)
            self.assertEqual(receipts[0]['sha256'], logs.hashlib.sha256(
                (run / 'ucx_node89_123.log').read_bytes()).hexdigest())

    def test_early_parser_cannot_hide_trailing_runtime_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, ev, base, _ = self.streaming_fixture(root, trailing='UCX ERROR device failed\n')
            with self.assertRaisesRegex(ValueError, 'runtime error'):
                logs.collect(root, row, ev, base, parse_lines=lambda lines: next(lines), reuse_existing=True)

    def test_streamed_peer_hash_mismatch_is_rejected_even_after_parser_returns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, ev, base, _ = self.streaming_fixture(root, peer_mismatch=True)
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                logs.collect(root, row, ev, base, parse_lines=lambda lines: next(lines), reuse_existing=True)

    def test_streaming_keeps_finite_limit_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, ev, base, run = self.streaming_fixture(root)
            with patch.object(logs, 'MAX_LOG', 64), self.assertRaisesRegex(ValueError, 'oversized'):
                logs.collect(root, row, ev, base, parse_lines=lambda lines: next(lines), reuse_existing=True)
            target = run / 'ucx_node89_123.log'
            target.rename(run / 'original.log')
            target.symlink_to(run / 'original.log')
            with self.assertRaisesRegex(ValueError, 'nonregular'):
                logs.collect(root, row, ev, base, parse_lines=lambda lines: next(lines), reuse_existing=True)


class CheckpointTests(unittest.TestCase):
    def fixture(self):
        case = Path('/candidate')
        root = case / 'old'
        tasks = [('WINDOW', c) for c in am.window.cases()] + [('AM_RNDV', c) for c in am.cases()]
        rows = [{'name':logs.trial_directory(root/'window_and_rndv', item, i).name,
                 'family':family, 'matrix_case':item, 'passed':i<19, 'exit':0}
                for i, (family, item) in enumerate(tasks)]
        rows[-1].update(error='missing/extra/duplicate/out-of-order OSU size row',
                        cleanup={'local':{'terminated_own_pids':[]},'peer':{'terminated_own_pids':[]}})
        normal, boundary = case/'normal/results.json', case/'boundary/results.json'
        data = {root/'results.json':{'source_commit':recheck.COMMIT,'module_sha256':recheck.MODULE,
                    'status':'STOPPED_INCOMPLETE','boundary_matrix':str(boundary),
                    'normal_regression':{'results_json':str(normal),'passed':True,'cases':28}},
                normal:{'cases':[{'passed':True}]*28}, boundary:{'cases':[{'passed':True}]*18},
                root/'comparison/comparison_trials.json':[
                    {'comparison_label':label,'passed':True}
                    for label in ('old_rm','new_rm','rc') for _ in range(5)],
                root/'window_and_rndv/results.json':{'source_commit':recheck.COMMIT,
                    'module_sha256':recheck.MODULE,'cases':rows}}
        return root, data

    def test_checkpoint_requires_prior_passes(self):
        root, data = self.fixture()
        with patch.object(recheck, 'read_json', side_effect=lambda path:data[Path(path)]), \
                patch.object(recheck.osu, 'digest', return_value='a'*64):
            self.assertEqual(recheck.check_checkpoint(root, root.parent)['prior_matrix_passed'],19)
            data[root/'window_and_rndv/results.json']['cases'][4]['passed'] = False
            with self.assertRaises(ValueError):
                recheck.check_checkpoint(root, root.parent)

    def test_wrong_product_and_nonzero_exit_are_not_reusable(self):
        for condition in ('source','exit'):
            root,data = self.fixture()
            if condition=='source':
                data[root/'results.json']['source_commit']='0'*40
            else:
                data[root/'window_and_rndv/results.json']['cases'][-1]['exit']=134
            with patch.object(recheck, 'read_json', side_effect=lambda path:data[Path(path)]), \
                    patch.object(recheck.osu, 'digest', return_value='a'*64):
                with self.assertRaises(ValueError):
                    recheck.check_checkpoint(root,root.parent)

    def test_default_recheck_does_not_touch_node(self):
        with patch.object(sys,'argv',['recheck_last.py']), \
                patch.object(recheck.candidate,'validate_build') as build, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            recheck.main()
        build.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['new_mpi_tasks'],1)


if __name__=='__main__':
    unittest.main()
