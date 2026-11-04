"""Real --run validation/dispatch; stop before SSH, SDK and MPI execution."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
spec = importlib.util.spec_from_file_location('cleanup_resume_test', ROOT/'resume_entry.py')
resume = importlib.util.module_from_spec(spec);spec.loader.exec_module(resume)

class ReachedDispatch(BaseException):
    pass

class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.case = Path(cls.directory.name)/'case'
        source = cls.case/'source';source.mkdir(parents=True)
        data = subprocess.check_output(['git','-C',str(REPO),'archive',resume.COMMIT])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(source, filter='data')
        module = cls.case/'install/lib/ucx/libuct_ub.so.0.0.0'
        module.parent.mkdir(parents=True);module.write_bytes(b'private CPU module fixture')
        cls.module = hashlib.sha256(module.read_bytes()).hexdigest()
        (cls.case/'build_manifest.json').write_text(json.dumps(dict(
            case_dir=str(cls.case),source_commit=resume.COMMIT,module_sha256=cls.module)))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_patch_is_identical_to_permanent_engine_fix(self):
        old = subprocess.check_output(['git','-C',str(REPO),'show',resume.COMMIT+
            ':contrib/rm_ctp/send_pool/run_candidate.py']).decode()
        current = (ROOT.parent/'send_pool/run_candidate.py').read_text()
        self.assertEqual(old.replace(resume.OLD,resume.NEW,1),current)

    def test_real_run_validates_build_manifest_sources_and_reaches_dispatch(self):
        case = self.case
        execute_module = importlib.machinery.SourceFileLoader.exec_module
        reached = []
        def load_and_hook(loader,module):
            execute_module(loader,module)
            if module.__name__ != 'cleanup_verified_entry':
                return
            configure = module.configure
            def configure_and_stop_before_devices():
                configure()
                engine = module.engine
                engine.EXPERIMENT = case.parent
                # Reproduce the rejected old guard with this same legitimate path.
                text = (case/'source/contrib/rm_ctp/send_pool/run_candidate.py').read_text()
                first = text.index('def validate_candidate(')
                last = text.index('\n\n@contextmanager',first)
                namespace = dict(engine.__dict__)
                exec(text[first:last], namespace)
                with self.assertRaisesRegex(ValueError,'isolated candidate source'):
                    namespace['validate_candidate'](case)
                def dispatch(actual,build,out,timing):
                    self.assertEqual(actual,case)
                    self.assertEqual(build['source_commit'],resume.COMMIT)
                    self.assertEqual(engine.ROOT,case/'source/contrib/rm_ctp/send_cleanup')
                    self.assertEqual(out.parent,case)
                    self.assertFalse(timing)
                    reached.append(True)
                    raise ReachedDispatch()
                engine.execute = dispatch
            module.configure = configure_and_stop_before_devices
        old_argv = sys.argv[:]
        try:
            with patch.object(resume,'MODULE',self.module), \
                 patch.object(os,'geteuid',return_value=0), \
                 patch('socket.gethostname',return_value='node89'), \
                 patch.object(importlib.machinery.SourceFileLoader,'exec_module',load_and_hook), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ReachedDispatch):
                    resume.run(case,'test-tool-commit')
                # Revalidate every original source and module after the memory patch.
                resume.checked_source(case)
        finally:
            sys.argv = old_argv
        self.assertEqual(reached,[True])

    def test_changed_source_rejected_before_import_or_dispatch(self):
        path = self.case/'source/src/uct/urma/rm_urma/rm_urma_ep.c'
        original = path.read_bytes()
        try:
            path.write_bytes(original+b'\n/* private test mutation */\n')
            with patch.object(resume,'MODULE',self.module), self.assertRaisesRegex(ValueError,'source changed'):
                resume.checked_source(self.case)
        finally:
            path.write_bytes(original)

    def test_changed_module_rejected_before_import_or_dispatch(self):
        path = self.case/'install/lib/ucx/libuct_ub.so.0.0.0'
        original = path.read_bytes()
        try:
            path.write_bytes(b'changed private fixture')
            with patch.object(resume,'MODULE',self.module), self.assertRaisesRegex(ValueError,'module changed'):
                resume.checked_source(self.case)
        finally:
            path.write_bytes(original)

    def test_other_tool_names_and_external_directories_stay_rejected(self):
        text = (ROOT.parent/'send_pool/run_candidate.py').read_text()
        start = text.index('def validate_candidate(')
        end = text.index('\n\n@contextmanager',start)
        for tool in (self.case/'source/contrib/rm_ctp/other_tool',
                     self.case/'external/send_cleanup'):
            namespace = {'ROOT':tool}
            exec(text[start:end], namespace)
            with self.assertRaisesRegex(ValueError,'isolated candidate source'):
                namespace['validate_candidate'](self.case)

if __name__ == '__main__':
    unittest.main()
