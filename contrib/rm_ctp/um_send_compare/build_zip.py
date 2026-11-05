"""Build the node-only tool ZIP; product source and install are not bundled."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent


def build(destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('output exists; preserve previous package')
    runner = (ROOT.parent/'um_send/run_candidate.py').read_bytes()
    entries = {name:(ROOT/name).read_bytes() for name in ('policy.py','run_compare.py','bootstrap.py')}
    entries['pinned_guard.py'] = (ROOT.parent/'um_send/resume_timefix.py').read_bytes()
    entries['pinned_runner.py'] = runner
    entries['__main__.py'] = b'import sys\nsys.dont_write_bytecode=True\nfrom bootstrap import entry\nentry()\n'
    commit = subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    entries['metadata.json'] = (json.dumps({'tool_commit':commit,
        'product_commit':'a4d07599bd5b66b84c5de8257e5f7fe15e3a6f63',
        'product_module':'48dcd30a30f29a39d8f9b63e0c2afd7279bdd3da23b1757a610c828b77b04c2f',
        'runner_sha256':hashlib.sha256(runner).hexdigest(),
        'product_rebuild':False},sort_keys=True,indent=2)+'\n').encode()
    entries['files.sha256.json'] = (json.dumps({name:hashlib.sha256(value).hexdigest()
        for name,value in entries.items()},sort_keys=True,indent=2)+'\n').encode()
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        for name,value in sorted(entries.items()):
            info = zipfile.ZipInfo(name,(2026,11,5,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16;z.writestr(info,value)
    print('NODE_PACKAGE='+str(destination))
    print('NODE_SHA256='+hashlib.sha256(destination.read_bytes()).hexdigest())


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('destination',type=Path)
    build(parser.parse_args().destination)
