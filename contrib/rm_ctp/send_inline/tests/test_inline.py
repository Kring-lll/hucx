import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[2]


def function(text):
    start=text.index('ucs_status_t uct_rm_urma_ep_reliable_post(')
    opening=text.index('{',start)
    depth=1
    for pos in range(opening+1,len(text)):
        depth+=(text[pos]=='{')-(text[pos]=='}')
        if not depth:
            return text[start:pos+1]+'\n'
    raise ValueError('production function has unbalanced braces')


class ProductTests(unittest.TestCase):
    def test_actual_post_boundaries_replay_and_errors_with_sanitizers(self):
        source=(REPO/'src/uct/urma/rm_urma/rm_urma_ep.c').read_text()
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            (out/'actual_reliable_post.inc').write_text(function(source))
            command=['gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1','-g',
                     '-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(out),
                     str(ROOT/'tests/post_fixture.c'),'-o',str(out/'test')]
            subprocess.run(command,check=True,capture_output=True)
            import os
            env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0')
            result=subprocess.run([str(out/'test')],env=env,text=True,capture_output=True,check=True)
            self.assertIn('CPU_MODEL_CASES=104',result.stdout)

    def test_only_the_reviewed_post_function_changes_product(self):
        path='src/uct/urma/rm_urma/rm_urma_ep.c'
        before=subprocess.check_output(['git','-C',str(REPO),'show','5ff9635e4d1b9479fbb60832889067de9771238e:'+path]).decode()
        after=(REPO/path).read_text()
        self.assertEqual(before.replace(function(before),''),after.replace(function(after),''))
        self.assertIn('use_inline ? NULL : entry->desc->tseg',function(after))
        self.assertIn('use_inline ? URMA_INLINE_ENABLE : URMA_INLINE_DISABLE',function(after))


if __name__=='__main__':
    unittest.main()
