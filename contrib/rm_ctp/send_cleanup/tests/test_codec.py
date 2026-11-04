"""Differential CPU checks of actual old/new codecs; no device simulation."""
import ctypes as C
import random
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
BASE = 'bdc1e36e9ee75d97dee2d3e93b9e83b6bd10b49f'
PREFIX = 'src/uct/urma/rm_urma/'

class Address(C.Structure):
    _fields_ = [('va', C.c_uint64), ('length', C.c_uint64),
                ('jetty', C.c_uint32), ('flags', C.c_uint32),
                ('token', C.c_uint32), ('depth', C.c_uint32),
                ('peers', C.c_uint16), ('credits', C.c_uint16),
                ('instance', C.c_uint64)]

class CodecTests(unittest.TestCase):
    def test_original_golden_cases_with_sanitizers(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / 'test')
            subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra',
                            '-Werror', '-fsanitize=address,undefined',
                            '-fno-omit-frame-pointer', '-I'+str(ROOT/'src'),
                            str(ROOT/PREFIX/'rm_urma_addr.c'),
                            str(ROOT/'contrib/rm_ctp/r1/test_link_addr.c'),
                            '-o', binary], check=True, capture_output=True)
            subprocess.run([binary], env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'),
                           check=True, capture_output=True)

    def test_byte_exact_and_failure_atomic_against_accepted_codec(self):
        def original(name):
            return subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE+':'+PREFIX+name]).decode()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out/'rm_urma_addr.h').write_text(original('rm_urma_addr.h'))
            reliable = original('rm_urma_reliable.c')
            first = reliable.index('ucs_status_t uct_rm_urma_reliable_address_pack(')
            last = reliable.index('void uct_rm_urma_reliable_peer_init(', first)
            (out/'old.c').write_text(original('rm_urma_addr.c')+'\n'+reliable[first:last])
            libs = []
            for source, name in ((out/'old.c', 'old.so'),
                                 (ROOT/PREFIX/'rm_urma_addr.c', 'new.so')):
                subprocess.run(['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                                '-O2', '-shared', '-fPIC', '-I'+str(ROOT/'src'),
                                str(source), '-o', str(out/name)], check=True, capture_output=True)
                libs.append(C.CDLL(str(out/name)))
            rng = random.Random(1104)
            cases = [Address(0x0102030405060708, 64, 7, 0, 1, 64, 4, 8, 11)]
            for _ in range(200):
                cases.append(Address(rng.getrandbits(64), rng.getrandbits(64),
                                     rng.getrandbits(32), rng.getrandbits(32),
                                     rng.getrandbits(32), rng.getrandbits(32),
                                     rng.randrange(65536), rng.randrange(65536),
                                     rng.getrandbits(64)))
            for version in ('iface_addr_v2', 'reliable_address'):
                pack = [getattr(lib, 'uct_rm_urma_'+version+'_pack') for lib in libs]
                unpack = [getattr(lib, 'uct_rm_urma_'+version+'_unpack') for lib in libs]
                for address in cases:
                    for offset in (0, 1, 3, 7):
                        buffers = [C.create_string_buffer(b'\xa5'*64) for _ in libs]
                        codes = [f(C.byref(b, offset), 48, C.byref(address))
                                 for f, b in zip(pack, buffers)]
                        self.assertEqual(codes[0], codes[1])
                        self.assertEqual(buffers[0].raw, buffers[1].raw)
                        if codes[0] == 0:
                            decoded = [Address() for _ in libs]
                            self.assertEqual([f(C.byref(b, offset), 48, C.byref(a))
                                              for f,b,a in zip(unpack,buffers,decoded)], [0,0])
                            self.assertEqual(bytes(decoded[0]), bytes(decoded[1]))
                # Every wire byte mutation and every neighbouring length.
                valid = C.create_string_buffer(48)
                self.assertEqual(pack[0](valid, 48, C.byref(cases[0])), 0)
                mutations = [valid.raw]
                for byte in range(48):
                    for value in (0, 0xff):
                        changed = bytearray(valid.raw);changed[byte] = value
                        mutations.append(bytes(changed))
                for wire in mutations:
                    for length in (0, 35, 47, 48, 49):
                        data = C.create_string_buffer(wire+b'\0')
                        outputs = [Address.from_buffer_copy(b'\xa5'*48) for _ in libs]
                        codes = [f(data, length, C.byref(a)) for f,a in zip(unpack,outputs)]
                        self.assertEqual(codes[0], codes[1])
                        self.assertEqual(bytes(outputs[0]), bytes(outputs[1]))
                        if codes[0] != 0:
                            self.assertEqual(bytes(outputs[0]), b'\xa5'*48)

if __name__ == '__main__':
    unittest.main()
