import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import node_entry


class NodeEntryTests(unittest.TestCase):
    def package(self, root, extra=False):
        payload={'contrib/rm_ctp/send_integration/run_integration.py':b'# CPU fixture only\n'}
        manifest={'tool_commit':'a'*40,'source_commit':'b'*40,'module_sha256':hashlib.sha256(b'module').hexdigest(),
                  'plan':{'node_acceptance':'PENDING'},
                  'files':{k:hashlib.sha256(v).hexdigest() for k,v in payload.items()}}
        path=root/'node.zip'
        with zipfile.ZipFile(path,'x') as archive:
            archive.writestr('__main__.py',Path(node_entry.__file__).read_bytes())
            archive.writestr('package_manifest.json',json.dumps(manifest))
            for name,data in payload.items(): archive.writestr('payload/'+name,data)
            if extra: archive.writestr('payload/../outside',b'bad')
        return path,manifest

    def test_default_zip_plan_does_not_run_or_install(self):
        with tempfile.TemporaryDirectory() as temp:
            path,_=self.package(Path(temp))
            result=subprocess.run([sys.executable,str(path)],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['node_acceptance'],'PENDING')
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()),['node.zip'])

    def test_unlisted_or_traversal_payload_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path,manifest=self.package(Path(temp),True)
            with zipfile.ZipFile(path) as archive, self.assertRaises(ValueError):
                node_entry.validate_payload(archive,manifest)

    def test_staging_preserves_existing_source_and_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path,manifest=self.package(root)
            case=root/'accepted'
            (case/'source').mkdir(parents=True)
            (case/'source/keep').write_bytes(b'accepted-source')
            module=case/'install/lib/ucx/libuct_ub.so.0.0.0'
            module.parent.mkdir(parents=True)
            module.write_bytes(b'module')
            (case/'build_manifest.json').write_text(json.dumps({'source_commit':'b'*40,'module_sha256':manifest['module_sha256']}))
            with mock.patch.object(node_entry,'DEFAULT_CASE',case), \
                 mock.patch.object(node_entry.os,'geteuid',return_value=0), \
                 mock.patch.object(node_entry.socket,'gethostname',return_value='node89'), \
                 mock.patch.object(sys,'argv',[str(path),'--preflight-only']), \
                 mock.patch.object(node_entry.subprocess,'call',return_value=0) as execute, \
                 self.assertRaises(SystemExit) as exit_value:
                node_entry.main()
            self.assertEqual(exit_value.exception.code,0)
            self.assertEqual((case/'source/keep').read_bytes(),b'accepted-source')
            self.assertEqual(module.read_bytes(),b'module')
            staged=list(case.glob('send_tools_fixed_*'))
            self.assertEqual(len(staged),1)
            self.assertTrue((staged[0]/'tool_receipt.json').is_file())
            self.assertIn('--preflight-only',execute.call_args.args[0])
