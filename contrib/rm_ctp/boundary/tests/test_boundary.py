"""Pure format, boundary, and fail-closed validation tests (no node access)."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[0] / 'osu_compare'))
from boundary_config import SEGMENT_KEY, bytes_value, check_outputs, parse_capability, parse_config
from run_boundary import SIZES
import run as osu
from analysis import parse_output


class BoundaryTests(unittest.TestCase):
    def test_effective_config_and_exact_target(self):
        text = ('UCX_RM_URMA_SEG_SIZE=4K\nUCX_RM_URMA_LINK_MODE=iface_v2\n'
                'UCX_RM_URMA_FC_SOFT_THRESH=0.5\n')
        env = {SEGMENT_KEY: '4096', 'UCX_RM_URMA_LINK_MODE': 'iface_v2',
               'UCX_RM_URMA_FC_SOFT_THRESH': '0.5'}
        capability = ('# Device: bonding_dev_0\n# am_bcopy: <= 8189\n'
                      '# Device: udmac0d1e2:1\n# am_short: <= 125\n'
                      '# am_bcopy: <= 4093\n# Device: other\n# am_bcopy: <= 8190\n')
        self.assertEqual(check_outputs({'config': text, 'device': capability}, env, 4096)
                         ['am_bcopy_bytes'], 4093)
        self.assertEqual(parse_capability(capability), 4093)
        self.assertEqual(bytes_value('8K'), 8192)
        self.assertEqual(bytes_value('4096'), 4096)

    def test_reject_mismatched_or_unrecognized_config(self):
        with self.assertRaisesRegex(ValueError, 'differs'):
            parse_config('UCX_RM_URMA_SEG_SIZE=8K\n', {SEGMENT_KEY: '4096'})
        with self.assertRaisesRegex(ValueError, 'not parsed'):
            parse_config('', {SEGMENT_KEY: '4096'})
        with self.assertRaisesRegex(ValueError, 'unit'):
            bytes_value('unlimited')
        with self.assertRaisesRegex(ValueError, 'inconsistent'):
            check_outputs({'config': 'UCX_RM_URMA_SEG_SIZE=4K\n',
                           'device': '# Device: udmac0d1e2:1\n# am_bcopy: <= 8189\n'},
                          {SEGMENT_KEY: '4096'}, 4096)

    def test_standard_osu_size_and_validation_are_checked(self):
        self.assertEqual(osu.arguments('osu_multi_lat', 1000, 100, True, 4093)[:2],
                         ['--message-size', '4093:4093'])
        text = '# OSU MPI Multi Latency Test v7.5.2\n# Validation\n4093 3.21 Pass\n'
        self.assertTrue(parse_output(text, 'osu_multi_lat', True, 1000, 4093)
                        ['validation_pass'])
        with self.assertRaisesRegex(ValueError, '4094B result'):
            parse_output(text, 'osu_multi_lat', True, 1000, 4094)
        with self.assertRaisesRegex(ValueError, 'error'):
            parse_output(text.replace('Pass', 'Fail'), 'osu_multi_lat', True, 1000, 4093)
        self.assertEqual(SIZES[-1], 16384)


if __name__ == '__main__':
    unittest.main()
