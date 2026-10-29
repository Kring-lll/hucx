"""Real UCX table parsing with a no-device stub; not URMA communication."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('CANDIDATE_UCX_PREFIX'), 'requires the isolated CPU build')
class ConfigParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prefix = Path(os.environ['CANDIDATE_UCX_PREFIX']).resolve()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.stub = Path(cls.temporary.name) / 'libno_devices.so'
        include = os.environ.get('URMA_TEST_INCLUDE', '/usr/include/ub/umdk/urma')
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-I' + include, str(Path(__file__).with_name('no_devices.c')),
                        '-o', str(cls.stub)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def query(self, override):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('UCX_', 'LD_'))}
        library = str(self.prefix / 'lib') + ':' + os.environ.get('URMA_TEST_LIB', '')
        env.update(LD_LIBRARY_PATH=library, LD_PRELOAD=str(self.stub),
                   UCX_MODULE_DIR=str(self.prefix / 'lib/ucx'))
        if override is not None:
            env['UCX_RM_URMA_SEG_SIZE'] = override
        result = subprocess.run([str(self.prefix / 'bin/ucx_info'), '-c'], env=env,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(re.findall(r'^UCX_(RM|RC|UM)_URMA_SEG_SIZE=(.*)$', result.stdout, re.M))
        return values

    def test_default_is_rm4k_and_preserves_rc_um8k(self):
        self.assertEqual(self.query(None), {'RM': '4K', 'RC': '8K', 'UM': '8K'})

    def test_explicit_smaller_override_takes_precedence(self):
        self.assertEqual(self.query('2048'), {'RM': '2K', 'RC': '8K', 'UM': '8K'})

    def test_explicit_larger_override_is_not_silently_clamped(self):
        self.assertEqual(self.query('8192'), {'RM': '8K', 'RC': '8K', 'UM': '8K'})


if __name__ == '__main__':
    unittest.main()
