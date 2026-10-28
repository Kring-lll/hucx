"""CPU fixtures: target identity, no attach to others, own-job early stop."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import proc_guard
import supervisor


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p192_guard_cpu_')
        self.root = Path(self.temp.name)
        self.proc = self.root/'proc'
        self.run = self.root/'run'
        self.run.mkdir()
        self.binary = self.root/'osu_fixture'
        self.binary.write_text('CPU fixture; never executed or attached')
        self.config = {'run':str(self.run),'nonce':'a'*32,'binary':str(self.binary)}
        markers = ('A89_RUN_NONCE='+self.config['nonce']+'\0HUCX_OSU_PROBE_DIR='+str(self.run)+
                   '\0OMPI_COMM_WORLD_RANK=0\0').encode()
        for pid,tick in [(10,'77'),(20,'88')]:
            directory = self.proc/str(pid)
            directory.mkdir(parents=True)
            (directory/'stat').write_text(str(pid)+' (process with space) '+' '.join(['R']+['0']*18+[tick]))
            (directory/'environ').write_bytes(markers)
            (directory/'status').write_text('State:\tR (running)\nThreads:\t2\n')
        childdir = self.proc/'10/task/10'
        childdir.mkdir(parents=True)
        (childdir/'children').write_text('20')
        (self.proc/'20/exe').symlink_to(self.binary)
        (self.run/'pid_rank0.json').write_text(json.dumps({'rank':0,'pid':10,
                'start_ticks':'77','nonce':self.config['nonce']}))
        (self.run/'rank0_after_init.maps').write_text('CPU stage fixture')

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_marker_parent_start_time_binary_and_actual_child(self):
        row = proc_guard.find_app(self.config,0,self.proc)
        self.assertEqual((row['rank'],row['pid'],row['start_ticks']),(0,20,'88'))
        snapshot = proc_guard.snapshot(self.config,self.proc)
        self.assertEqual((snapshot['started'],snapshot['after_init'],snapshot['before_finalize']),(1,1,0))

    def test_parent_pid_reuse_rejected(self):
        p=self.run/'pid_rank0.json'
        row=json.loads(p.read_text())
        row['start_ticks']='different'
        p.write_text(json.dumps(row))
        with self.assertRaises(ValueError):
            proc_guard.find_app(self.config,0,self.proc)

    def test_other_job_child_marker_and_other_binary_both_rejected(self):
        p=self.proc/'20/environ'
        original=p.read_bytes()
        p.write_bytes(original.replace(b'a'*32,b'b'*32))
        with self.assertRaises(ProcessLookupError):
            proc_guard.find_app(self.config,0,self.proc)
        p.write_bytes(original)
        (self.proc/'20/exe').unlink()
        other=self.root/'different_app'
        other.write_text('not our executable')
        (self.proc/'20/exe').symlink_to(other)
        with self.assertRaises(ProcessLookupError):
            proc_guard.find_app(self.config,0,self.proc)

    def test_gdb_never_called_when_identity_not_proven(self):
        (self.proc/'20/environ').write_bytes(b'unrelated process')
        calls=[]
        def execute(*args,**kwargs):
            calls.append(args)
            raise AssertionError('must not attach')
        row=supervisor.capture_one(self.config,0,self.root,1,self.proc,execute)
        self.assertFalse(row['passed'])
        self.assertEqual(calls,[])

    def test_backtrace_saved_and_phase_extracted_without_real_attach(self):
        def execute(command,**kwargs):
            self.assertIn('20',command)
            kwargs['stdout'].write('#0 ucp_worker_progress ()\n#8 PMPI_Barrier ()\n')
            return types.SimpleNamespace(returncode=0)
        row=supervisor.capture_one(self.config,0,self.root,1,self.proc,execute)
        self.assertTrue(row['passed'])
        self.assertEqual(row['wait_frames'],'PMPI_Barrier')
        self.assertIn('#0',(self.root/'round1_rank0.gdb.log').read_text())

    def test_debugger_timeout_is_failed_evidence(self):
        def execute(command,**kwargs):
            raise subprocess.TimeoutExpired(command,12)
        row=supervisor.capture_one(self.config,0,self.root,1,self.proc,execute)
        self.assertFalse(row['passed'])
        self.assertIn('TimeoutExpired',row['error'])


class SupervisorTests(unittest.TestCase):
    def build(self,out):
        def write_json(path,data):
            Path(path).write_text(json.dumps(data))
        base=types.SimpleNamespace(write_json=write_json,run_logged=lambda *a:'UNCHANGED_FALLBACK')
        report={'stacks':[],'intentional_early_stop':False}
        return supervisor.Supervisor(types.SimpleNamespace(base=base),out,report)

    def test_sampling_stops_only_spawned_launcher_group_and_does_not_dump_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)
            control=self.build(out)
            calls=[]
            control.sample=lambda log,number:calls.append(number)
            with patch.object(supervisor,'CAPTURE_AT',(0,0)):
                with self.assertRaises(supervisor.DiagnosticStop):
                    control([sys.executable,'-c','import time; time.sleep(60)'],
                            dict(os.environ,DO_NOT_SAVE_SECRET='private'),out/'launcher.log',620)
            self.assertEqual(calls,[1,2])
            self.assertTrue(control.report['intentional_early_stop'])
            self.assertLess(control.report['supervisor_wall_s'],5)
            saved=json.loads((out/'launcher.command.json').read_text())
            self.assertNotIn('DO_NOT_SAVE_SECRET',saved['environment'])

    def test_fast_success_is_not_mislabelled_as_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            control=self.build(Path(folder))
            with patch.object(supervisor,'CAPTURE_AT',(1,2)):
                result=control([sys.executable,'-c','print("CPU_FIXTURE_NOT_MPI")'],
                               dict(os.environ),Path(folder)/'launcher.log',620)
            self.assertEqual(result['exit'],0)
            self.assertFalse(control.report['intentional_early_stop'])
            self.assertEqual(control(['unused'],{},Path(folder)/'compile.log',10),'UNCHANGED_FALLBACK')
