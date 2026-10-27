"""CPU file/SSH-stream contracts only; no MPI/URMA performance evidence."""
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifacts
import run_scale


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='a89_transfer_cpu_')
        self.root = Path(self.temporary.name)
        self.peer = self.root / 'peer'
        self.local = self.root / 'local'
        self.peer.mkdir()
        self.local.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def files(self, ranks, probe):
        for name in artifacts.required_names(ranks):
            (self.peer / name).write_text('CPU_FIXTURE_ONLY ' + name + '\n')
        if probe:
            for rank in ranks:
                (self.peer / ('rank%d_pid%d.jsonl' % (rank, rank + 1000))).write_text('{}\n')

    def stream(self, ranks, probe, archive=None):
        archive = archive or self.root / 'stream.tar.gz'
        with archive.open('wb') as output:
            subprocess.run([sys.executable, '-c', artifacts.REMOTE, str(self.peer),
                            json.dumps(list(ranks)), '1' if probe else '0'],
                           stdout=output, check=True, timeout=15)
        return archive

    def test_large_stream_is_one_connection_and_accepts_existing_identical_partial_files(self):
        ranks = range(192, 384)
        self.files(ranks, True)
        partial = 'rank192.json'
        (self.local / partial).write_bytes((self.peer / partial).read_bytes())
        real_run = subprocess.run
        calls = []

        def local_ssh(command, **kwargs):
            calls.append(command)
            remote = shlex.split(command[-1])
            self.assertEqual(remote[:2], ['python3', '-c'])
            # Run the exact remote archive producer locally: no SSH/MPI hardware.
            return real_run([sys.executable] + remote[1:], **kwargs)

        with patch.object(artifacts.subprocess, 'run', side_effect=local_ssh):
            receipt = artifacts.fetch(self.peer, ranks, True, destination=self.local)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'ssh')
        self.assertEqual(len(receipt['files']), 192 * 6)
        self.assertTrue(receipt['validated'])
        for name in receipt['files']:
            self.assertEqual((self.peer / name).read_bytes(), (self.local / name).read_bytes())

    def modified(self, archive, kind):
        members = []
        with tarfile.open(archive) as source:
            for member in source:
                members.append((member.name, source.extractfile(member).read()))
        if kind == 'corrupt':
            members[-1] = (members[-1][0], b'altered payload')
        elif kind == 'path':
            manifest = json.loads(members[0][1])
            manifest['../outside'] = manifest.pop(members[-1][0])
            members[0] = (members[0][0], json.dumps(manifest).encode())
            members[-1] = ('../outside', members[-1][1])
        elif kind == 'duplicate':
            members.append(members[-1])
        output = self.root / (kind + '.tar.gz')
        with tarfile.open(output, 'w:gz') as destination:
            for name, data in members:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                destination.addfile(member, io.BytesIO(data))
        return output

    def test_rejects_corrupt_path_and_duplicate_without_publishing(self):
        self.files([1], True)
        archive = self.stream([1], True)
        for kind in ('corrupt', 'path', 'duplicate'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                artifacts.unpack_checked(self.modified(archive, kind), self.local, [1], True)
            self.assertEqual(list(self.local.iterdir()), [])

    def test_missing_rank_or_observer_and_mismatched_existing_file_stop(self):
        self.files([1], False)
        archive = self.stream([1], False)
        with self.assertRaisesRegex(ValueError, 'observer'):
            artifacts.unpack_checked(archive, self.local, [1], True)
        with self.assertRaisesRegex(ValueError, 'unexpected|coverage'):
            artifacts.unpack_checked(archive, self.local, [1, 2], False)
        (self.local / 'rank1.json').write_text('old differs')
        with self.assertRaisesRegex(ValueError, 'differs'):
            artifacts.unpack_checked(archive, self.local, [1], False)
        self.assertEqual((self.local / 'rank1.json').read_text(), 'old differs')

    def test_timeout_error_is_short_and_retains_partial(self):
        with patch.object(artifacts.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['ssh'], 900)):
            with self.assertRaisesRegex(RuntimeError, 'timeout=900s') as caught:
                artifacts.fetch(self.peer, [1], False, destination=self.local)
        self.assertLess(len(str(caught.exception)), 600)
        self.assertEqual(len(list((self.local / 'artifact_fetch').glob('*.partial'))), 1)

    def test_recovered_validation_rejects_data_errors_and_leaks(self):
        config = {'nonce': 'a' * 32, 'budget': {'tx_depth': 64}}
        inventories = [{'hostname': 'node89', 'allowed_cpus': [0]},
                       {'hostname': 'node97', 'allowed_cpus': [0]}]
        for rank in (0, 1):
            row = {'rank': rank, 'ppn': 1, 'pid': 100 + rank, 'rounds': 10,
                   'hostname': inventories[rank]['hostname'],
                   'data_validation': True, 'finalized': True}
            row.update({key: .1 for key in ('init_s', 'first_all_peers_s', 'steady_s',
                       'burst64_s', 'burst1024_s', 'finalize_s')})
            row.update({key: 100 for key in ('init_rss_kib', 'first_rss_kib',
                        'steady_rss_kib', 'peak_rss_kib')})
            (self.local / ('rank%d.json' % rank)).write_text(json.dumps(row))
            (self.local / ('pid_rank%d.json' % rank)).write_text(json.dumps({
                'nonce': config['nonce'], 'cpu_affinity': [0]}))
            maps = '\n'.join('0-1 rw /candidate/lib/' + lib + '.0' for lib in
                   ('libucp.so', 'libuct.so', 'libucs.so', 'libucm.so', 'libuct_ub.so'))
            for phase in ('after_init', 'after_first', 'after_steady'):
                (self.local / ('rank%d_%s.maps' % (rank, phase))).write_text(maps)
            events = [{'event': 'start', 'rank': rank},
                {'event': 'acquire', 'kind': 'jetty', 'mode': 1, 'rc': 0,
                 'ptr': 'p', 'ctx': 'c', 'depth': 64},
                {'event': 'release', 'kind': 'jetty', 'rc': 0, 'ptr': 'p'}]
            (self.local / ('rank%d_pid%d.jsonl' % (rank, 100 + rank))).write_text(
                '\n'.join(map(json.dumps, events)))
        (self.local / 'launcher.log').write_text(
            '\n'.join('A89_RANK_PASS rank=%d finalized=YES' % rank for rank in (0, 1)))
        checked = run_scale.validate_trial_artifacts(self.local, config, 1, inventories,
                                                   '/candidate', 'iface', True)
        self.assertEqual(checked['resources']['0']['live_at_exit'], 0)
        observer = self.local / 'rank0_pid100.jsonl'
        observer.write_text('\n'.join(observer.read_text().splitlines()[:-1]))
        with self.assertRaisesRegex(ValueError, 'leak'):
            run_scale.validate_trial_artifacts(self.local, config, 1, inventories,
                                              '/candidate', 'iface', True)
        row = json.loads((self.local / 'rank0.json').read_text())
        row['data_validation'] = False
        (self.local / 'rank0.json').write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, 'payload'):
            run_scale.validate_trial_artifacts(self.local, config, 1, inventories,
                                              '/candidate', 'iface', True)
