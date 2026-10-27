"""Deterministic /proc fixtures; never signal real system processes."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import osu_rank


class CleanupTests(unittest.TestCase):
    def test_only_exact_nonce_and_directory_match_even_without_path_in_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / 'proc'
            proc.mkdir()
            run = root / 'trial'
            nonce = 'b' * 32
            for pid, token, path in ((123451, nonce, run),
                                     (123452, 'c' * 32, run),
                                     (123453, nonce, root / 'trial_extra')):
                folder = proc / str(pid)
                folder.mkdir()
                # start_ticks reads field 22; a realistic comm can contain spaces.
                (folder / 'stat').write_text(str(pid) + ' (OSU fixture) ' + ' '.join(['0'] * 19 + ['456']))
                (folder / 'environ').write_bytes(('A89_RUN_NONCE=' + token + '\0HUCX_OSU_PROBE_DIR=' + str(path)).encode())
                (folder / 'cmdline').write_bytes(b'/original/osu_multi_lat\0--message-size\08:8\0')
            calls = []
            with patch.object(osu_rank.os, 'kill', side_effect=lambda pid, sig: calls.append((pid, sig))), \
                 patch.object(osu_rank.time, 'sleep'):
                result = osu_rank.cleanup(nonce, run, proc)
            self.assertEqual(result, [123451])
            self.assertEqual(calls, [(123451, signal.SIGTERM), (123451, signal.SIGKILL)])

    def test_rejects_invalid_token_before_inspecting_processes(self):
        with self.assertRaisesRegex(ValueError, 'nonce'):
            osu_rank.cleanup('not-a-token', '/trial', Path('/missing'))
