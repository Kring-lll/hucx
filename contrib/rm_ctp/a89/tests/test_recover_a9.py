"""Synthetic full-matrix file recovery, not actual MPI/URMA acceptance."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifacts
import recover_a9 as recovery
import run_scale
import support


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='a89_recovery_cpu_')
        cls.case = Path(cls.temporary.name)
        cls.source = cls.case / recovery.SOURCE_NAME
        cls.source.mkdir()
        cls.path = cls.source / 'results.json'
        toolkit = json.loads(Path(__file__).with_name('checkpoint_manifest_9c10557.json').read_text())
        cls.assert_fixture = recovery.manifest_digest(toolkit) == recovery.SOURCE_TOOLKIT
        cls.report = {'product_commit': support.PRODUCT_COMMIT,
            'module_sha256': support.PRODUCT_MODULE, 'toolkit': toolkit,
            'node_run_started': 'CPU_SYNTHETIC_FIXTURE', 'a8_functional': True,
            'a9_functional': False, 'error': 'RuntimeError: A9资源/回收失败，停止扩大：' + recovery.FAILED_NAME,
            'inventory': [{'hostname': 'node89', 'boot_id': 'a', 'allowed_cpus': list(range(192))},
                          {'hostname': 'node97', 'boot_id': 'b', 'allowed_cpus': list(range(192))}],
            'installations': {mode: {'install': mode, 'module_sha256': 'same',
                                    'base_libraries_sha256': {'ucp': 'same'}}
                              for mode in ('candidate', 'old_rm', 'old_minimal_control')},
            'saved_launcher_records_sha256': {'rm': 'same', 'rc': 'same'},
            'observer_identity': {'local': 'same', 'peer': 'same'},
            'trials': [], 'regressions': [], 'comparisons': [], 'a9_levels': []}
        binary = cls.source / 'mpi_scale'
        binary.write_bytes(b'CPU_SYNTHETIC_NOT_EXECUTABLE')
        cls.report['binary_sha256'] = support.digest(binary)
        for index, marker in enumerate(('功能矩阵=通过', 'DIRECT_UCT_FOUR_EP_PASS=YES',
                    'A7_NODE_LIFECYCLE_PASS=YES', 'MPI_ADDRESS_PHASE_PASS=YES')):
            log = cls.source / ('regression%d.log' % index)
            log.write_text(marker)
            cls.report['regressions'].append({'log': str(log), 'passed': True, 'exit': 0})
        for ppn in list(support.A8_LEVELS) + [32, 48, 64, 96]:
            group = []
            for mode in (('iface', 'old_rm', 'rc') if ppn <= 20 else ('iface',)):
                for repeat, probe in [(i, False) for i in range(3)] + [(0, True)]:
                    row = cls.make_trial(mode, ppn, repeat, probe)
                    group.append(row)
                    cls.report['trials'].append(row)
            if ppn <= 20:
                cls.report['comparisons'].append(run_scale.comparison(group, ppn))
            elif ppn < 96:
                timings = [r for r in group if not r['resource_probe']]
                cls.report['a9_levels'].append({'ppn': ppn,
                    'resource_trial': 'iface_p%d_r0_resources' % ppn,
                    'timings': {key: support.statistics_for([r['metrics'][key] for r in timings])
                                for key in timings[0]['metrics']}})
        cls.complete_peer = cls.case / 'peer_complete'
        old = cls.source / recovery.FAILED_NAME
        shutil.copytree(old, cls.complete_peer)
        # Reproduce interrupted scp: final remote files are absent locally only.
        for path in old.glob('rank191*'):
            path.unlink()
        failed = cls.report['trials'][-1]
        failed.pop('metrics')
        failed.pop('resources')
        failed['passed'] = False
        failed['error'] = "TimeoutExpired: Command '['scp', 'many files']' timed out after 119.999 seconds"
        (old / 'trial.json').write_text(json.dumps(failed))
        cls.path.write_text(json.dumps(cls.report))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def make_trial(cls, mode, ppn, repeat, probe):
        name = '%s_p%d_r%d_%s' % (mode, ppn, repeat, 'resources' if probe else 'timing')
        run = cls.source / name
        run.mkdir()
        config = {'run': str(run), 'nonce': 'a' * 32, 'budget': support.budget(ppn)}
        (run / 'rank_config.json').write_text(json.dumps(config))
        install = support.PRE_IFACE if mode == 'old_rm' else cls.case / 'install'
        maps = '\n'.join('0-1 rw ' + str(install / 'lib' / (lib + '.0')) for lib in
                       ('libucp.so', 'libuct.so', 'libucs.so', 'libucm.so', 'libuct_ub.so'))
        for rank in range(2 * ppn):
            row = {'rank': rank, 'pid': 1000 + rank, 'ppn': ppn, 'rounds': max(10, 2000 // ppn),
                   'hostname': cls.report['inventory'][rank // ppn]['hostname'],
                   'data_validation': True, 'finalized': True}
            row.update({key: .1 for key in ('init_s', 'first_all_peers_s', 'steady_s',
                        'burst64_s', 'burst1024_s', 'finalize_s')})
            row.update({key: 100 for key in ('init_rss_kib', 'first_rss_kib',
                        'steady_rss_kib', 'peak_rss_kib')})
            (run / ('rank%d.json' % rank)).write_text(json.dumps(row))
            (run / ('pid_rank%d.json' % rank)).write_text(json.dumps({
                'nonce': config['nonce'], 'cpu_affinity': [rank % ppn]}))
            for phase in ('after_init', 'after_first', 'after_steady'):
                (run / ('rank%d_%s.maps' % (rank, phase))).write_text(maps)
            if probe:
                events = [{'event': 'start', 'rank': rank},
                    {'event': 'acquire', 'kind': 'jetty', 'rc': 0, 'mode': 2 if mode == 'rc' else 1,
                     'ptr': 'p', 'ctx': 'c', 'depth': 64},
                    {'event': 'release', 'kind': 'jetty', 'rc': 0, 'ptr': 'p'}]
                (run / ('rank%d_pid%d.jsonl' % (rank, 1000 + rank))).write_text(
                    '\n'.join(map(json.dumps, events)))
        (run / 'launcher.log').write_text('\n'.join('A89_RANK_PASS rank=%d finalized=YES' % rank
                                                    for rank in range(2 * ppn)))
        checked = run_scale.validate_trial_artifacts(run, config, ppn, cls.report['inventory'],
                                                     install, mode, probe)
        row = {'name': name, 'mode': mode, 'ppn': ppn, 'repeat': repeat, 'configuration': config,
               'resource_probe': probe, 'passed': True, 'exit': 0, **checked,
               'cleanup': {'local': {'terminated_own_pids': []}, 'peer': {'terminated_own_pids': []}}}
        (run / 'trial.json').write_text(json.dumps(row))
        return row

    def test_full_matrix_revalidated_and_ppn96_recovers_without_modifying_original(self):
        self.assertTrue(self.assert_fixture)
        previous = recovery.source_identity(self.path, self.case, self.report, self.report['toolkit'])
        rows, evidence = recovery.validate_history(previous, self.path, self.case)
        self.assertEqual(len(rows), 100)
        self.assertEqual(len(evidence), 104)
        old_trial = self.source / recovery.FAILED_NAME / 'trial.json'
        before = old_trial.read_bytes()
        out = self.case / 'recovered'
        out.mkdir()

        def real_archive_fetch(run, ranks, probe, peer, destination):
            archive = out / 'peer.tar.gz'
            with archive.open('wb') as stream:
                subprocess.run([sys.executable, '-c', artifacts.REMOTE, str(self.complete_peer),
                                json.dumps(list(ranks)), '1'], stdout=stream, check=True, timeout=20)
            artifacts.unpack_checked(archive, destination, ranks, probe)

        with patch.object(recovery, 'fetch', side_effect=real_archive_fetch):
            row = recovery.recover_resource(self.path, out, rows[recovery.FAILED_NAME], previous, self.case)
        self.assertTrue(row['passed'])
        self.assertEqual(len(row['resources']), 192)
        self.assertEqual(old_trial.read_bytes(), before)
        self.assertFalse((self.source / recovery.FAILED_NAME / 'rank191.json').exists())

    def test_rejects_changed_identity_terminated_processes_and_non_collection_failure(self):
        for key in ('boot_id', 'allowed_cpus'):
            current = copy.deepcopy(self.report)
            current['inventory'][0][key] = 'changed'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'boot/CPU'):
                recovery.source_identity(self.path, self.case, current, self.report['toolkit'])
        row = copy.deepcopy(self.report['trials'][-1])
        row['cleanup']['peer']['terminated_own_pids'] = [123]
        with self.assertRaisesRegex(ValueError, 'left processes'):
            recovery.require_clean(row)
        previous = copy.deepcopy(self.report)
        previous['trials'][-1]['error'] = 'RuntimeError: MPI timeout'
        # trial/report equality is also a gate; failure cannot be relabelled.
        with self.assertRaisesRegex(ValueError, 'differs|post-launch'):
            recovery.validate_history(previous, self.path, self.case)

    def test_rejects_modified_payload_map_or_summary_in_historical_results(self):
        path = self.source / 'iface_p1_r0_timing/rank0.json'
        before = path.read_bytes()
        try:
            value = json.loads(before)
            value['data_validation'] = False
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'payload'):
                recovery.validate_history(self.report, self.path, self.case)
        finally:
            path.write_bytes(before)
        previous = copy.deepcopy(self.report)
        previous['comparisons'][0]['ppn'] = 999
        with self.assertRaisesRegex(ValueError, 'summary'):
            recovery.validate_history(previous, self.path, self.case)
