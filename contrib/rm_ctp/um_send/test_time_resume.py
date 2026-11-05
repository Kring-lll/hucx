"""Exercise the real isolated-source validator before stopping at the SSH boundary."""
import hashlib
import importlib.util
import json
from pathlib import Path
import io
import subprocess
import tarfile
import socket
import sys
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('um_send_time_resume',HERE/'resume_timefix.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.case=Path(cls.tmp.name)/'r1_shared_a4d_fixture'
        archive=subprocess.check_output(['git','-C',str(HERE.parents[2]),'-c','core.autocrlf=false','-c','core.eol=lf','archive',r.COMMIT])
        (cls.case/'source').mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as t:t.extractall(cls.case/'source')
        cls.module=cls.case/'install/lib/ucx/libuct_ub.so.0.0.0';cls.module.parent.mkdir(parents=True)
        cls.module.write_bytes(b'CPU_MODULE_FIXTURE_NOT_ARM')
        cls.module_sha=r.digest(cls.module)
        (cls.case/'build_manifest.json').write_text(json.dumps({'source_commit':r.COMMIT,
            'archive_sha256':r.ARCHIVE_SHA,'module_sha256':cls.module_sha}))
        cls.payload=(HERE/'run_candidate.py').read_bytes()
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def test_real_validate_source_and_effective_microseconds(self):
        with mock.patch.object(r,'CASE',self.case):ns=r.load_controller(self.case,self.payload,self.module_sha)
        ns['LAB']=self.case.parent
        self.assertEqual(ns['validate_source'](self.case)['module_sha256'],self.module_sha)
        self.assertEqual(ns['time_seconds']('10000000.00us'),ns['time_seconds']('10s'))
        self.assertEqual(r.digest(self.module),self.module_sha)
    def test_actual_main_reaches_network_boundary(self):
        with mock.patch.object(r,'CASE',self.case):ns=r.load_controller(self.case,self.payload,self.module_sha)
        ns['LAB']=self.case.parent
        def stop(*args):raise RuntimeError('CPU_STOP_BEFORE_SSH')
        ns['deploy']=stop
        with mock.patch.object(socket,'gethostname',return_value='node89'),mock.patch.object(ns['os'],'geteuid',return_value=0),mock.patch.object(sys,'argv',['runner',str(self.case),'--run']):
            with self.assertRaisesRegex(RuntimeError,'CPU_STOP_BEFORE_SSH'):ns['main']()
        self.assertEqual(r.digest(self.case/'source'/r.RELATIVE_ENTRY),r.ENTRY_SHA)
    def test_corrupt_fixed_code_rejected(self):
        with mock.patch.object(r,'CASE',self.case),self.assertRaisesRegex(ValueError,'fixed runner hash'):
            r.load_controller(self.case,self.payload+b'\n',self.module_sha)
    def test_wrong_module_rejected(self):
        with mock.patch.object(r,'CASE',self.case),self.assertRaisesRegex(ValueError,'build identity'):
            r.load_controller(self.case,self.payload,'0'*64)
    def test_changed_source_rejected(self):
        entry=self.case/'source'/r.RELATIVE_ENTRY;before=entry.read_bytes()
        try:
            entry.write_bytes(before+b'\n')
            with mock.patch.object(r,'CASE',self.case),self.assertRaisesRegex(ValueError,'source changed'):
                r.load_controller(self.case,self.payload,self.module_sha)
        finally:entry.write_bytes(before)
    def test_outside_case_rejected(self):
        with self.assertRaisesRegex(ValueError,'unexpected candidate'):r.load_controller(Path('/tmp/unrelated'),self.payload)

if __name__=='__main__':unittest.main()
