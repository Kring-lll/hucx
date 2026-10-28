"""Real archive producer/consumer for the OSU file schema, with no SSH server."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'a89'))
import artifacts


class TransferTests(unittest.TestCase):
    def test_osu_schema_transfers_real_two_phase_files_and_never_invents_a89_snapshots(self):
        with tempfile.TemporaryDirectory(prefix='osu_archive_cpu_') as directory:
            root = Path(directory)
            peer = root / 'peer'
            target = root / 'local'
            peer.mkdir()
            target.mkdir()
            ranks = [20, 21]
            for name in artifacts.required_names(ranks, schema='osu'):
                (peer / name).write_text('CPU_OSU_SCHEMA_FIXTURE ' + name)
            for rank in ranks:
                (peer / ('rank%d_pid%d.jsonl' % (rank, rank + 100))).write_text('{}\n')
            archive = root / 'stream.tar.gz'
            with archive.open('wb') as output:
                subprocess.run([sys.executable, '-c', artifacts.REMOTE, str(peer),
                                json.dumps(ranks), '1', 'osu'], stdout=output, check=True, timeout=10)
            files = artifacts.unpack_checked(archive, target, ranks, True, schema='osu')
            self.assertEqual(len(files), 10)
            self.assertFalse(any('after_first' in name or 'after_steady' in name for name in files))
            self.assertTrue((target / 'rank20_before_finalize.maps').exists())
            with self.assertRaisesRegex(ValueError, 'unexpected|coverage'):
                artifacts.unpack_checked(archive, target, ranks, True, schema='a89')

    def test_unknown_schema_cannot_silently_use_old_file_contract(self):
        with self.assertRaisesRegex(ValueError, 'schema'):
            artifacts.required_names([0], schema='unknown')
