import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import protocol as p
import run_rndv as runner

IDENTITIES = {0: {'host': 'node89', 'pid': 123}, 1: {'host': 'node97', 'pid': 456}}


def config(case):
    return '\n'.join(key + '=' + value for key, value in p.settings(case).items())


def trace(rank, request='0xaa', size=65536, am=True, threshold=16384):
    identity = IDENTITIES[rank]
    prefix = '[12.0] [%s:%d   :0] tag_send.c:78 UCX REQ ' % (identity['host'], identity['pid'])
    value = prefix + 'select tag request(%s) progress algorithm length=%d mem_type:host rndv_thresh=%d zcopy_thresh=16384\n' % (request, size, threshold)
    if am:
        value += prefix + 'req %s: using rndv_data protocol\n' % request
    return value


class ProtocolTests(unittest.TestCase):
    def test_fixed_matrix_remains_bounded(self):
        cases = p.cases()
        self.assertEqual(len(cases), 10)
        self.assertEqual(max(c['ppn'] for c in cases), 4)
        self.assertEqual(max(c['sizes'][0] for c in cases), 1048576)
        self.assertEqual([c['sizes'][0] for c in cases[2:5]], [16383, 16384, 16385])
        self.assertEqual(cases[5]['sizes'], cases[6]['sizes'])
        self.assertEqual([cases[5]['path'], cases[6]['path']], ['eager', 'am_rndv'])
        for case in cases:
            args = p.arguments(case)
            self.assertIn('--validation', args)
            self.assertIn('%d:%d' % (case['sizes'] * 2), args)
        with self.assertRaises(ValueError):
            p.arguments(dict(cases[7], sizes=(1048577,)))

    def test_configuration_accepts_units_and_rejects_wrong_real_option(self):
        case = p.cases()[6]
        text = config(case).replace('=16384', '=16K').replace('=req', '=REQ')
        self.assertEqual(p.check_config(text, case, runner.candidate.bytes_value)['UCX_RNDV_SCHEME'], 'am')
        for bad in (text.replace('=am', '=auto'), text.replace('=REQ', '=trace_req'),
                    text.replace('=16K', '=8K'), text.replace('UCX_ZCOPY_THRESH=inf', 'UCX_ZCOPY_THRESH=auto')):
            with self.assertRaises(ValueError):
                p.check_config(bad, case, runner.candidate.bytes_value)

    def test_capability_gate_checks_target_not_other_devices(self):
        text = '# Device: udmac0d1e2:1\n# am_bcopy: <= 4093\n# Device: other:1\n# put_zcopy: unlimited\n'
        result = p.check_am_only_capability(text, runner.parse_capability)
        self.assertFalse(result['native_RMA_and_AM_zcopy_exposed'])
        for capability in ('put_short', 'put_bcopy', 'put_zcopy', 'get_short', 'get_bcopy', 'get_zcopy', 'am_zcopy'):
            bad = text.replace('# am_bcopy:', '# '+capability+': 1\n# am_bcopy:')
            with self.assertRaises(ValueError):
                p.check_am_only_capability(bad, runner.parse_capability)
        with self.assertRaises(ValueError):
            p.check_am_only_capability(text.replace('4093', '8189'), runner.parse_capability)

    def test_requires_every_actual_sender_and_full_identity(self):
        case = p.cases()[8]
        good = trace(0) + trace(1)
        self.assertEqual(p.inspect_trace(good, case, IDENTITIES)['sending_ranks'], [0, 1])
        for bad in (trace(0), good.replace('node97', 'wrong'), good.replace(':456', ':457'),
                    good.replace('length=65536', 'length=65535')):
            with self.assertRaises(ValueError):
                p.inspect_trace(bad, case, IDENTITIES)

    def test_one_way_bw_does_not_require_receiver_to_send_large_payload(self):
        self.assertEqual(p.inspect_trace(trace(0), p.cases()[6], IDENTITIES)['sending_ranks'], [0])

    def test_request_pointer_reuse_is_not_global_protocol_proof(self):
        case = p.cases()[6]
        text = trace(0, am=False) + trace(0, size=8)
        with self.assertRaises(ValueError):
            p.inspect_trace(text, case, IDENTITIES)
        with self.assertRaises(ValueError):
            p.inspect_trace(trace(0, request='0xaa', am=False) + trace(0, request='0xbb', size=8), case, IDENTITIES)

    def test_native_rma_selection_is_not_mistaken_for_am_rendezvous(self):
        text = trace(0, am=False) + '[12.0] [node89:123:0] rndv.c:375 UCX REQ req 0xaa: rndv_put completed with status Success\n'
        with self.assertRaises(ValueError):
            p.inspect_trace(text, p.cases()[6], IDENTITIES)

    def test_eager_and_threshold_controls_are_checked(self):
        self.assertEqual(p.inspect_trace(trace(0, size=16383, am=False), p.cases()[2], IDENTITIES)['path'], 'eager')
        with self.assertRaises(ValueError):
            p.inspect_trace(trace(0, size=16383), p.cases()[2], IDENTITIES)
        with self.assertRaises(ValueError):
            p.inspect_trace(trace(0, threshold=8192), p.cases()[6], IDENTITIES)
        self.assertEqual(p.inspect_trace(trace(0, threshold=2**64-1, am=False), p.cases()[5], IDENTITIES)['status'], 'PASS')

    def test_debug_allocation_fallback_is_retained_but_not_a_data_error(self):
        noise = '[12.0] [node89:123:0] uct_mem.c:282 UCX TRACE failed to allocate hugetlb\n'
        case = p.cases()[6]
        osu = '# OSU MPI Bandwidth Test v7.5.2\n# Datatype: MPI_CHAR.\n65536 2.0 Pass\n'
        result = runner.window_matrix.parse_output(p.osu_text(noise + osu), case, 20)
        self.assertTrue(result['validation_pass'])
        for bad in ('UCX ERROR failure', 'UCX FATAL failure', 'DATA VALIDATION ERROR', 'MPI_ERR_OTHER'):
            with self.assertRaises(ValueError):
                p.osu_text(noise + osu + '\n' + bad)
        with self.assertRaises(ValueError):
            runner.window_matrix.parse_output(p.osu_text(osu.replace('Pass', 'Fail')), case, 20)

    def test_profile_restores_every_parent_hook_on_failure(self):
        previous = lambda *args, **kwargs: {'UCX_LOG_FILE': '/old', 'OTHER': 'ok'}
        osu = SimpleNamespace(arguments=object(), parse_output=object(), environment=previous)
        old = osu.arguments, osu.parse_output, osu.environment
        with self.assertRaises(RuntimeError):
            with p.trial_profile(osu, p.cases()[6], runner.window_matrix.parse_output):
                self.assertEqual(osu.environment()['UCX_LOG_LEVEL'], 'req')
                self.assertNotIn('UCX_LOG_FILE', osu.environment())
                self.assertEqual(osu.arguments('osu_bw', 20, 2, True), p.arguments(p.cases()[6]))
                raise RuntimeError('injected')
        self.assertEqual((osu.arguments, osu.parse_output, osu.environment), old)

    def test_protocol_failure_is_persisted_separately_from_payload_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            run = out / 'trial'
            run.mkdir()
            case = p.cases()[6]
            for host in ('node89', 'node97'):
                (run / (host + '_ucx_config.txt')).write_text(config(case))
            (run / 'launcher.log').write_text('no protocol proof')
            row = {'name': 'trial', 'passed': True, 'rank_evidence': {'pids': {0:123, 1:456}}}
            evidence = {'inventory': [{'hostname':'node89'}, {'hostname':'node97'}]}
            runner.finish_protocol(out, row, case, evidence)
            self.assertFalse(row['passed'])
            self.assertIn('selection missing', json.loads((run / 'trial.json').read_text())['error'])

    def test_preflight_capabilities_use_existing_real_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            for host in ('node89', 'node97'):
                (out / (host + '_ucx_device.txt')).write_text('# Device: udmac0d1e2:1\n# am_bcopy: <= 4093\n')
            with patch.object(runner.candidate, 'default_capabilities', return_value={}):
                result = runner.inspect_defaults(out, {})
            self.assertEqual(len(result['am_only_capabilities']), 2)

    def test_default_action_does_not_inspect_or_launch_node(self):
        import contextlib
        import io
        with patch.object(sys, 'argv', ['run_rndv.py']), \
                patch.object(runner.candidate, 'validate_build') as build, \
                patch.object(runner.osu, 'preflight') as preflight, \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            runner.main()
        build.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual(json.loads(stream.getvalue())['task_count'], 10)


if __name__ == '__main__':
    unittest.main()
