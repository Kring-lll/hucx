import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class EntryTests(unittest.TestCase):
    def test_plan_uses_pool_checkpoint_and_does_not_run_by_default(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT/'run_candidate.py'),
                                 '/not-a-node-case'], check=True, text=True, capture_output=True)
        value = json.loads(result.stdout)
        self.assertEqual(value['default_action'], 'PLAN_ONLY')
        self.assertFalse(value['performance_requested_by_default'])
        self.assertEqual(value['prior_product'], 'bdc1e36e9ee75d97dee2d3e93b9e83b6bd10b49f')
        self.assertEqual(value['original_osu_functional_tasks']+
                         value['strict_alternating_tasks']+
                         value['original_osu_software_injection_tasks'], 14)

    def test_changed_checkpoint_is_rejected(self):
        spec = importlib.util.spec_from_file_location('cleanup_entry', ROOT/'run_candidate.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            path = case/'send_pool_accept_8vruomlc/results.json'
            path.parent.mkdir();path.write_text('{}')
            with patch.object(module.engine, 'OLD_CASE', case), self.assertRaises(ValueError):
                module.checkpoint()

if __name__ == '__main__':
    unittest.main()
