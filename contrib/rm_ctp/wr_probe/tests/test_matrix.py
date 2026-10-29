import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_probe as probe


class MatrixTests(unittest.TestCase):
    def arguments(self, values):
        parser = probe.make_parser()
        return probe.validate_arguments(parser, parser.parse_args(values))

    def test_profile_is_bounded_and_keeps_original_default(self):
        old = probe.planned_matrix(self.arguments([]))
        self.assertEqual((old['ppn'], old['rm_validation_trials']), (1, 5))
        new = probe.planned_matrix(self.arguments(['--boundary-matrix']))
        self.assertEqual((new['ppn'], new['rm_validation_trials'], new['rc_8b_gates']), (4, 16, 2))
        self.assertEqual(new['sizes'][1:4], [4084, 4085, 4086])
        self.assertEqual(new['sizes'][4:7], [8145, 8146, 8147])

    def test_invalid_plan_is_rejected_before_node_access(self):
        for values in (['--sizes', '8', '8'], ['--sizes', '16385'],
                       ['--ppn', '192'], ['--benchmarks', 'osu_alltoall', 'osu_alltoall']):
            with self.subTest(values=values), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.arguments(values)

    def populate(self, run):
        row = {'ppn': 4, 'rank_evidence': {'pids': list(range(100, 108))},
               'configuration': {'nonce': 'a' * 32}}
        for rank, pid in enumerate(row['rank_evidence']['pids']):
            summary = {'schema': 1, 'rank': rank, 'pid': pid, 'nonce': 'a' * 32,
                       'calls': 1, 'error_calls': 0, 'incomplete': 0,
                       'provider_is_liburma': True,
                       'buckets': [{'bytes': 4096, 'submitted': 1, 'success': 1,
                                    'num_sge': 1, 'inline': 0}]}
            (run / ('wr_rank%d_pid%d.json' % (rank, pid))).write_text(json.dumps(summary))
            for phase in ('after_init', 'before_finalize'):
                (run / ('rank%d_%s.maps' % (rank, phase))).write_text('0000 0000 /fixed/libprobe.so\n')
        return row

    def test_checks_every_actual_application_rank(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            row = self.populate(run)
            observations = probe.inspect_trial(run, row, '/fixed/libprobe.so')
            self.assertEqual([item['rank'] for item in observations], list(range(8)))
            (run / 'wr_rank7_pid107.json').unlink()
            with self.assertRaises(FileNotFoundError):
                probe.inspect_trial(run, row, '/fixed/libprobe.so')

    def test_wrong_pid_identity_and_missing_late_maps_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            row = self.populate(run)
            path = run / 'wr_rank7_pid107.json'
            data = json.loads(path.read_text())
            data['pid'] = 108
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                probe.inspect_trial(run, row, '/fixed/libprobe.so')
            self.populate(run)
            (run / 'rank7_before_finalize.maps').write_text('0000 0000 /wrong/libprobe.so\n')
            with self.assertRaises(ValueError):
                probe.inspect_trial(run, row, '/fixed/libprobe.so')

    def test_peer_summary_batch_has_complete_coverage_and_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            row = self.populate(run)
            names = ['wr_rank%d_pid%d.json' % (rank, 100 + rank) for rank in range(4, 8)]
            payload = {name: (run / name).read_text() for name in names}
            for name in names:
                (run / name).unlink()
            with patch.object(probe.osu.base, 'ssh', return_value=json.dumps(payload)) as ssh:
                receipt = probe.fetch_peer_summaries(run, row)
            self.assertEqual(ssh.call_count, 1)
            self.assertEqual(set(receipt), set(names))
            self.assertEqual(len(probe.inspect_trial(run, row, '/fixed/libprobe.so')), 8)
            payload.pop(names[-1])
            with patch.object(probe.osu.base, 'ssh', return_value=json.dumps(payload)):
                with self.assertRaises(ValueError):
                    probe.fetch_peer_summaries(run, row)

    def test_existing_summary_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            row = self.populate(run)
            payload = {'wr_rank%d_pid%d.json' % (rank, 100 + rank): '{}'
                       for rank in range(4, 8)}
            with patch.object(probe.osu.base, 'ssh', return_value=json.dumps(payload)):
                with self.assertRaises(ValueError):
                    probe.fetch_peer_summaries(run, row)


if __name__ == '__main__':
    unittest.main()
