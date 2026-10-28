"""CPU-only synthetic checkpoint fixtures, never node-performance evidence."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import resume
import support


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='a89_resume_fixture_')
        self.case = Path(self.temporary.name)
        self.directory = self.case / resume.SOURCE_DIRECTORY
        self.directory.mkdir()
        self.path = self.directory / 'results.json'
        self.manifest = json.loads(Path(__file__).with_name('checkpoint_manifest_170.json').read_text())
        self.assertEqual(resume.manifest_digest(self.manifest), resume.SOURCE_MANIFEST)
        self.baseline = self.case / 'baseline'
        self.baseline.mkdir()
        for kind in ('rm', 'rc'):
            (self.baseline / (kind + '_run.json')).write_text('{}\n')
        self.current = {'inventory': [{'hostname': 'a', 'boot_id': 'a', 'allowed_cpus': [0]},
                                      {'hostname': 'b', 'boot_id': 'b', 'allowed_cpus': [0]}],
                        'installations': {mode: {'install': mode, 'module_sha256': 'same',
                                               'base_libraries_sha256': {'ucp': 'same'}}
                                          for mode in ('candidate', 'old_rm', 'old_minimal_control')}}
        self.report = {**copy.deepcopy(self.current), 'product_commit': support.PRODUCT_COMMIT,
                       'module_sha256': support.PRODUCT_MODULE, 'toolkit': self.manifest,
                       'error': 'RuntimeError: A8资源门禁失败：iface_p1_r0_resources',
                       'compile_command': ['mpicc', 'original.c'], 'regressions': [], 'trials': [],
                       'saved_launcher_records_sha256': {kind: support.digest(self.baseline / (kind + '_run.json'))
                                                        for kind in ('rm', 'rc')}}
        names = ('28项正常矩阵', '四EP无反向业务EP', '160EP生命周期', 'MPI地址阶段')
        markers = ('功能矩阵=通过', 'DIRECT_UCT_FOUR_EP_PASS=YES',
                   'A7_NODE_LIFECYCLE_PASS=YES', 'MPI_ADDRESS_PHASE_PASS=YES')
        for index, (name, marker) in enumerate(zip(names, markers)):
            log = self.directory / ('regression%d.log' % index)
            log.write_text(marker + '\n')
            self.report['regressions'].append({'name': name, 'passed': True, 'exit': 0, 'log': str(log)})
        binary = self.directory / 'mpi_scale'
        binary.write_bytes(b'CPU_FIXTURE_ONLY_NOT_AN_EXECUTABLE')
        self.report['binary_sha256'] = support.digest(binary)
        for mode in ('iface', 'old_rm', 'rc'):
            for repeat in range(3):
                name = '%s_p1_r%d_timing' % (mode, repeat)
                directory = self.directory / name
                directory.mkdir()
                ranks = []
                for rank in (0, 1):
                    data = {'rank': rank, 'ppn': 1, 'rounds': 10,
                            'data_validation': True, 'finalized': True}
                    data.update({key: 0.1 for key in ('init_s', 'first_all_peers_s', 'steady_s',
                                                      'burst64_s', 'burst1024_s', 'finalize_s')})
                    data.update({key: 100 for key in ('init_rss_kib', 'first_rss_kib',
                                                      'steady_rss_kib', 'peak_rss_kib')})
                    (directory / ('rank%d.json' % rank)).write_text(json.dumps(data))
                    ranks.append(data)
                    install = support.PRE_IFACE if mode == 'old_rm' else self.case / 'install'
                    maps = '\n'.join('0-1 rw ' + str(install / 'lib' / (library + '.0')) for library in
                                     ('libucp.so', 'libuct.so', 'libucs.so', 'libucm.so', 'libuct_ub.so'))
                    for phase in ('after_init', 'after_first', 'after_steady'):
                        (directory / ('rank%d_%s.maps' % (rank, phase))).write_text(maps)
                row = {'name': name, 'mode': mode, 'ppn': 1, 'repeat': repeat,
                       'resource_probe': False, 'passed': True,
                       'metrics': support.summarize(ranks, 1),
                       'cleanup': {'local': {'terminated_own_pids': []},
                                   'peer': {'terminated_own_pids': []}}}
                (directory / 'trial.json').write_text(json.dumps(row))
                self.report['trials'].append(row)
        self.report['trials'].append({'name': 'iface_p1_r0_resources', 'resource_probe': True,
            'passed': False, 'error': 'ValueError: missing Jetty/wrong depth/leak: rank 0',
            'cleanup': {'local': {'terminated_own_pids': []}, 'peer': {'terminated_own_pids': []}}})
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report))

    def tearDown(self):
        self.temporary.cleanup()

    def load(self):
        return resume.load_checkpoint(self.path, self.case, self.current,
                                      self.manifest, self.baseline, 3)

    def test_reuses_only_nine_unobserved_timings_and_preserves_failure(self):
        before = self.path.read_bytes()
        result = self.load()
        self.assertEqual(len(result['timings']), 9)
        self.assertEqual(len(result['regressions']), 4)
        self.assertEqual(len(result['provenance']['failed_trials_preserved']), 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(all(not row['resource_probe'] for row in result['timings'].values()))

    def test_rejects_changed_boot_library_or_launcher(self):
        self.current['inventory'][0]['boot_id'] = 'changed'
        with self.assertRaisesRegex(ValueError, '节点重启'):
            self.load()
        self.current['inventory'][0]['boot_id'] = 'a'
        self.current['installations']['candidate']['module_sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, '库身份'):
            self.load()
        self.current['installations']['candidate']['module_sha256'] = 'same'
        (self.baseline / 'rm_run.json').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'MPI命令'):
            self.load()

    def test_rejects_missing_regression_and_changed_rank_data(self):
        (self.directory / 'regression2.log').write_text('missing pass marker')
        with self.assertRaisesRegex(ValueError, '回归日志'):
            self.load()
        (self.directory / 'regression2.log').write_text('A7_NODE_LIFECYCLE_PASS=YES')
        path = self.directory / 'iface_p1_r0_timing/rank0.json'
        data = json.loads(path.read_text())
        data['data_validation'] = False
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'payload'):
            self.load()

    def test_rejects_expired_checkpoint_and_unapproved_toolkit(self):
        os.utime(self.path, (1, 1))
        with self.assertRaisesRegex(resume.CheckpointExpired, '一小时'):
            self.load()
        self.save()
        self.report['toolkit']['date'] = 'changed'
        self.save()
        with self.assertRaisesRegex(ValueError, '工具来源'):
            self.load()
