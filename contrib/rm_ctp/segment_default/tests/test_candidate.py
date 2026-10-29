import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import policy
import run_candidate as candidate

CONFIG = 'UCX_RM_URMA_SEG_SIZE=4K\nUCX_RC_URMA_SEG_SIZE=8K\nUCX_UM_URMA_SEG_SIZE=8K\n'


class CandidateTests(unittest.TestCase):
    def test_segment_overrides_removed_without_dropping_other_settings(self):
        env = {key: '8192' for key in policy.SEGMENT_KEYS}
        env.update(UCX_RM_URMA_TX_DEPTH='64', UCX_RNDV_THRESH='inf', LD_PRELOAD='/libs.so')
        cleaned = policy.without_segment_overrides(env)
        self.assertFalse(policy.SEGMENT_KEYS & cleaned.keys())
        self.assertEqual(cleaned, {'UCX_RM_URMA_TX_DEPTH': '64', 'UCX_RNDV_THRESH': 'inf',
                                   'LD_PRELOAD': '/libs.so'})
        self.assertIn('UCX_RM_URMA_SEG_SIZE', env)

    def test_real_config_text_requires_all_three_transport_defaults(self):
        parser = candidate.bytes_value
        self.assertEqual(policy.check_defaults(CONFIG, parser)['UCX_RM_URMA_SEG_SIZE'], 4096)
        for text in (CONFIG.replace('4K', '8K'), CONFIG.replace('RC_URMA_SEG_SIZE=8K',
                      'RC_URMA_SEG_SIZE=4K'), CONFIG.replace('UCX_UM_URMA_SEG_SIZE=8K\n', '')):
            with self.assertRaises(ValueError):
                policy.check_defaults(text, parser)

    def test_candidate_binding_restores_outer_runner_even_after_failure(self):
        original_env = lambda *a, **k: {'UCX_RM_URMA_SEG_SIZE': '4096', 'OTHER': 'ok'}
        runner = SimpleNamespace(DEFAULT_CASE=Path('/old'), PRODUCT_COMMIT='old',
                                 PRODUCT_MODULE='old-module', environment=original_env)
        with self.assertRaises(RuntimeError):
            with policy.bind_candidate(runner, Path('/new'),
                                       {'source_commit': 'new', 'module_sha256': 'new-module'}):
                self.assertEqual(runner.DEFAULT_CASE, Path('/new'))
                self.assertEqual(runner.environment(), {'OTHER': 'ok'})
                raise RuntimeError('injected failure')
        self.assertEqual(runner.DEFAULT_CASE, Path('/old'))
        self.assertIs(runner.environment, original_env)
        self.assertEqual(runner.PRODUCT_MODULE, 'old-module')

    def make_case(self, root):
        case = root / 'candidate'
        source = case / 'source'
        code = source / 'src/uct/urma/rm_urma/rm_urma_iface.c'
        code.parent.mkdir(parents=True)
        code.write_text('{"URMA_", "SEG_SIZE=4096", NULL,\n')
        manifest = source / 'contrib/rm_ctp/r1/candidate_manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'files': {str(code.relative_to(source)):
                                                  hashlib.sha256(code.read_bytes()).hexdigest()}}))
        module = case / 'install/lib/ucx/libuct_ub.so.0.0.0'
        module.parent.mkdir(parents=True)
        module.write_bytes(b'fake module for identity test')
        build = {'case_dir': str(case), 'source_commit': 'a' * 40,
                 'module_sha256': hashlib.sha256(module.read_bytes()).hexdigest()}
        (case / 'build_manifest.json').write_text(json.dumps(build))
        return case, build, code, module

    def test_build_is_bound_to_source_module_and_exact_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case, build, code, module = self.make_case(root)
            self.assertEqual(policy.validate_build(case, root), build)
            with self.assertRaises(ValueError):
                policy.validate_build(case, root / 'another')
            module.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                policy.validate_build(case, root)
            module.write_bytes(b'fake module for identity test')
            code.write_text('different source')
            with self.assertRaises(ValueError):
                policy.validate_build(case, root)

    def test_trial_checks_effective_defaults_and_absence_of_environment_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            run = out / 'trial'
            run.mkdir()
            for host in ('node89', 'node97'):
                (run / (host + '_ucx_config.txt')).write_text(CONFIG)
            row = {'name': 'trial', 'passed': True, 'configuration': {'environment': {}}}
            self.assertTrue(candidate.check_trial_default(row, out)['passed'])
            row['configuration']['environment']['UCX_RM_URMA_SEG_SIZE'] = '4096'
            self.assertFalse(candidate.check_trial_default(row, out)['passed'])
            row['configuration']['environment'] = {}
            row['passed'] = True
            (run / 'node97_ucx_config.txt').write_text(CONFIG.replace('4K', '8K'))
            self.assertFalse(candidate.check_trial_default(row, out)['passed'])

    def test_existing_wrong_peer_install_is_never_overwritten(self):
        with patch.object(candidate.osu.base, 'ssh', side_effect=['EXISTS', 'wrong-sha path']), \
                patch.object(candidate.osu.base, 'copy_remote') as copy:
            with self.assertRaises(ValueError):
                candidate.deploy(Path('/candidate'), {'module_sha256': 'expected'}, Path('/out'))
            copy.assert_not_called()

    def test_normal_regression_changes_case_root_without_moving_observer_dependencies(self):
        sys.path.insert(0, str(candidate.PROJECT / 'r1'))
        import accept_iface as legacy
        old_lab = legacy.base.LAB
        old_remote = legacy.remote
        old_argv = sys.argv
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            def run_regression():
                self.assertEqual(legacy.base.LAB, old_lab)
                self.assertIn('--experiment-root', sys.argv)
                self.assertEqual(sys.argv[sys.argv.index('--experiment-root') + 1],
                                 str(policy.B_ROOT))
                result = case / 'iface_accept_fixture'
                result.mkdir()
                (result / 'results.json').write_text(json.dumps(
                    {'cases': [{'passed': True} for _ in range(28)]}))
            with patch.object(legacy, 'main', side_effect=run_regression):
                self.assertEqual(candidate.normal_regression(case, case)['cases'], 28)
        self.assertEqual(legacy.base.LAB, old_lab)
        self.assertIs(legacy.remote, old_remote)
        self.assertIs(sys.argv, old_argv)

    def test_boundary_hook_does_not_force_segment_and_restores_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            receipt = {'controller_dir': str(case)}
            old_argv = sys.argv
            def fail():
                self.assertIsNot(candidate.osu.one_trial, trial)
                candidate.osu.one_trial(case, segment_size=4096)
                raise RuntimeError('stop after probe')
            def trial(out, **kwargs):
                self.assertIsNone(kwargs['segment_size'])
                return {'passed': False}
            with patch.object(candidate.osu, 'one_trial', side_effect=trial) as original, \
                    patch.object(candidate.wr, 'main', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    candidate.boundary_matrix(case, receipt)
                self.assertIs(candidate.osu.one_trial, original)
            self.assertIs(sys.argv, old_argv)

    def test_comparison_selects_correct_install_and_retains_negative_result(self):
        osu = candidate.osu
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'comparison'
            new_case = Path(temporary) / 'new-case'
            before_case = osu.DEFAULT_CASE
            targets = []

            def preflight(directory, commands):
                (directory / 'libosu_identity.so').write_bytes(b'identity fixture')
                return {'inventory': []}

            def trial(directory, commands, evidence, benchmark, mode, ppn, phase, repeat, args):
                label = 'old_rm' if osu.DEFAULT_CASE == candidate.OLD_CASE else (
                    'rc' if mode == 'rc' else 'new_rm')
                targets.append((label, phase, str(osu.DEFAULT_CASE)))
                return {'passed': True, 'configuration': {'environment': {'LD_PRELOAD': '/identity.so'}},
                        'output': {'latency_us': {'old_rm': 2.0, 'new_rm': 2.2, 'rc': 2.4}[label]},
                        'rank_evidence': {'before_finalize_rss_sum_mib': 100.0}}

            def ssh(script, *args, **kwargs):
                return 'fixture-sha file' if script.startswith('sha256sum') else ''

            with patch.object(osu, 'saved_spec', return_value=([], {})), \
                    patch.object(osu, 'preflight', side_effect=preflight), \
                    patch.object(osu.base, 'module_check', return_value={}), \
                    patch.object(osu.base, 'ssh', side_effect=ssh), \
                    patch.object(osu.base, 'copy_remote'), \
                    patch.object(osu, 'digest', return_value='fixture-sha'), \
                    patch.object(osu, 'one_trial', side_effect=trial):
                result = candidate.timing_comparison(out, new_case,
                    {'source_commit': 'a' * 40, 'module_sha256': 'b' * 64})
            self.assertEqual(len(targets), 16)
            self.assertEqual(targets[0][:2], ('old_rm', 'validation'))
            self.assertEqual(len(result['new_rm']['samples_us']), 5)
            self.assertLess(result['new_vs_old_latency_reduction_percent'], 0)
            self.assertEqual(result['new_rm']['rss_sum_mib_median'], 100.0)
            self.assertEqual(osu.DEFAULT_CASE, before_case)


if __name__ == '__main__':
    unittest.main()
