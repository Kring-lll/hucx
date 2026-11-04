import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1];REPO=ROOT.parents[2]
def extract(text,signature):
 start=text.index(signature);begin=text.index('{',start);depth=1
 for at in range(begin+1,len(text)):
  depth+=(text[at]=='{')-(text[at]=='}')
  if not depth:return text[start:at+1]+'\n'
 raise ValueError('unbalanced production function')
class PoolTests(unittest.TestCase):
 def test_verbatim_production_ownership_with_sanitizers_assert_on_off(self):
  s=(REPO/'src/uct/urma/rm_urma/rm_urma_reliable.c').read_text()
  signatures=['static void rel_release(', 'void uct_rm_urma_reliable_complete(',
              'static uct_rm_urma_replay_t *rel_alloc(', 'static ucs_status_t rel_accept(',
              'void uct_rm_urma_reliable_cleanup(']
  part=extract((REPO/'src/uct/urma/rm_urma/rm_urma_iface.c').read_text(),
               'static ucs_status_t uct_rm_urma_iface_pending_mpool_init(')
  part+='\n'.join(extract(s,x) for x in signatures)
  with tempfile.TemporaryDirectory() as directory:
   out=Path(directory);(out/'production.inc').write_text(part)
   for flag in (0,1):
    cmd=['gcc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-but-set-variable','-Wno-unused-parameter',
         '-fsanitize=address,undefined','-fno-omit-frame-pointer','-DPRODUCT_ASSERT='+str(flag),
         '-I'+str(out),str(ROOT/'tests/pool_fixture.c'),'-o',str(out/'test')]
    p=subprocess.run(cmd,text=True,capture_output=True);self.assertEqual(p.returncode,0,p.stderr)
    p=subprocess.run([str(out/'test')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),text=True,capture_output=True)
    self.assertEqual(p.returncode,0,p.stderr);self.assertIn('PRODUCTION_POOL_',p.stdout)
 def test_existing_pool_ownership_functions_are_unchanged(self):
  path='src/uct/urma/rm_urma/rm_urma_reliable.c'
  old=subprocess.check_output(['git','-C',str(REPO),'show','bdc1e36e9ee75d97dee2d3e93b9e83b6bd10b49f:'+path]).decode()
  new=(REPO/path).read_text()
  for sig in ('static void rel_release(', 'void uct_rm_urma_reliable_complete(',
              'static uct_rm_urma_replay_t *rel_alloc(', 'static ucs_status_t rel_accept(',
              'void uct_rm_urma_reliable_cleanup('):
   self.assertEqual(extract(old,sig),extract(new,sig))
 def test_old_comparison_requests_inline_validation(self):
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('pool_candidate_one',ROOT/'run_candidate.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as directory:
   out=Path(directory);(out/'fixture').mkdir()
   item=dict(benchmark='osu_multi_lat',ppn=1,size=8,path='eager',expected_wr=123)
   with patch.object(m.previous,'run_trial',return_value=dict(name='fixture',passed=True)),patch.object(m,'add_inline_evidence') as check:
    row=m.one(Path('/candidate'),out,dict(source_commit='a'*40,module_sha256='b'*64),{}, {}, {},item,'validation',0,'old_reliable')
    self.assertTrue(row['passed'])
    self.assertEqual(check.call_args.args[-1],False)
 def test_pool_runner_uses_inline_baseline_not_diagnostic(self):
  spec=importlib.util.spec_from_file_location('pool_candidate',ROOT/'run_candidate.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  self.assertEqual(m.OLD_CASE.name,'r1_shared_a2b4b07c1_Z1tIOO')
  self.assertEqual(m.plan()['timing_repeats'],5)
  self.assertNotIn('send_cost_build',str(m.OLD_CASE))
  self.assertFalse(m.plan()['hardware_fault_acceptance'])
if __name__=='__main__':unittest.main()
