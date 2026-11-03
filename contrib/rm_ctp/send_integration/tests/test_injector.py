"""Actual local dlopen/receive interposition; no URMA hardware or MPI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('gcc'), 'gcc required')
class InjectorTests(unittest.TestCase):
    def test_pinned_wire_and_ucp_layout(self):
        source=ROOT.parents[2]/'src'
        # An installed test-only package deliberately contains no product source.
        if not source.is_dir():
            self.skipTest('product headers absent from tool-only package')
        types=(source/'ucp/core/ucp_types.h').read_text()
        self.assertRegex(types,r'UCP_AM_ID_EAGER_FIRST\s*=\s*3\b')
        self.assertRegex(types,r'UCP_AM_ID_EAGER_MIDDLE\s*=\s*4\b')
        wire=(source/'uct/urma/rm_urma/rm_urma_reliable.c').read_text()
        self.assertIn('wire[6] = id;',wire)
        self.assertIn('rel_put32(wire, 32, length);',wire)

    def exercise(self, profile='eager_middle'):
        with tempfile.TemporaryDirectory(prefix='injector_dlopen_') as temp:
            out=Path(temp)
            fake=out/'fake.c'
            fake.write_text('''#include <stddef.h>
static int count;
int uct_rm_urma_reliable_receive(void *p,const void *b,size_t n) {
 (void)p; (void)b; (void)n; ++count; return 0;
}
int forwarded(void) { return count; }
''')
            provider=out/'provider.so'
            inject=out/'drop.so'
            subprocess.run(['gcc','-shared','-fPIC','-Wall','-Wextra','-Werror',str(fake),'-o',str(provider)],check=True)
            subprocess.run(['gcc','-std=gnu99','-shared','-fPIC','-Wall','-Wextra','-Werror',
                            str(ROOT/'drop_receive_mpi.c'),'-ldl','-o',str(inject)],check=True)
            code=r'''
import ctypes,json,os,struct
from pathlib import Path
provider=ctypes.CDLL(os.environ['HUCX_SEND_TEST_MODULE'], mode=os.RTLD_LOCAL|os.RTLD_NOW)
shim=ctypes.CDLL(os.environ['TEST_SHIM'], mode=os.RTLD_LOCAL|os.RTLD_NOW)
receive=shim.uct_rm_urma_reliable_receive
receive.argtypes=(ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t)
def call(kind,am,psn,payload):
    w=bytearray((104 if kind==1 else 36)+len(payload))
    struct.pack_into('<I',w,0,0x33524d53)
    w[4]=3; w[5]=kind; w[6]=am
    struct.pack_into('<QQQI',w,8,11,22,psn,len(payload))
    w[(104 if kind==1 else 36):]=payload
    b=ctypes.create_string_buffer(bytes(w))
    assert receive(None,b,len(w))==0
# A large single-packet payload must not consume an interior-fragment fault.
call(1,2,1,bytes(3500))
call(1,3,2,struct.pack('<QQQ',113,65536,71)+bytes(100))
call(1,4,3,struct.pack('<QQ',71,3965)+bytes(3973))
call(1,4,4,struct.pack('<QQ',71,7938)+bytes(3973))
call(1,4,4,struct.pack('<QQ',71,7938)+bytes(3973))
for psn in (1,2,4): call(2,0,psn,b'')
assert provider.forwarded()==6, provider.forwarded()
'''
            env=dict(os.environ, HUCX_SEND_TEST_MODULE=str(provider),HUCX_SEND_INJECT_PROFILE=profile,
                     TEST_SHIM=str(inject),HUCX_OSU_PROBE_DIR=temp,A89_RUN_NONCE='a'*32,OMPI_COMM_WORLD_RANK='0')
            result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=15)
            if profile!='eager_middle':
                self.assertNotEqual(result.returncode,0)
                self.assertIn('INVALID_PROFILE',result.stderr)
                return
            self.assertEqual(result.returncode,0,result.stderr)
            files=list(out.glob('inject_rank0_pid*.json'))
            self.assertEqual(len(files),1)
            row=json.loads(files[0].read_text())
            self.assertEqual((row['data_drops'],row['ack_drops'],row['duplicates']),(1,2,1))
            self.assertEqual((row['duplicate_offset'],row['drop_offset']),(3965,7938))

    def test_interior_fragment_faults_actual_dlopen(self): self.exercise()
    def test_unknown_profile_fails_closed(self): self.exercise('unknown')


if __name__=='__main__':
    unittest.main()
