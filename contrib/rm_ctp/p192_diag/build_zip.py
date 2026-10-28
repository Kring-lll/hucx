#!/usr/bin/env python3
"""Package the diagnostic plus exact tested runner, without product binaries."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
ENTRY = '''
import pathlib,runpy,sys,tempfile,zipfile
case=pathlib.Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz')
target=pathlib.Path(tempfile.mkdtemp(prefix='toolkit_p192_',dir=case if '--run' in sys.argv else None))
with zipfile.ZipFile(sys.argv[0]) as archive:
    for name in archive.namelist():
        if name.startswith('/') or '..' in pathlib.PurePosixPath(name).parts:
            raise SystemExit('diagnostic package path traversal')
    archive.extractall(target)
runpy.run_path(str(target/'p192_diag/diagnose.py'),run_name='__main__')
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    files = {str(p.relative_to(ROOT)):digest(p) for p in sorted(ROOT.rglob('*'))
             if p.is_file() and '__pycache__' not in p.parts and p.name != 'manifest.json'}
    manifest = {'source_base':'d0dc384b8045b6e88c4b081120b74a3b4b665157',
                'source_tree':'a90f15befa4bef2ffc04297c2bf4278b555ba196',
                'product_unchanged':True,'node_diagnostic':'PENDING','files':files}
    (ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    data = {'__main__.py':ENTRY.encode(),'p192_diag/manifest.json':(ROOT/'manifest.json').read_bytes()}
    data.update({'p192_diag/'+name:(ROOT/name).read_bytes() for name in files})
    core = ROOT.parent/'osu_compare'
    old = json.loads((core/'manifest.json').read_text())
    data['osu_compare/manifest.json'] = (core/'manifest.json').read_bytes()
    for name, expected in old['files'].items():
        if digest(core/name) != expected:
            raise ValueError('measured OSU source changed: '+name)
        data['osu_compare/'+name] = (core/name).read_bytes()
    for name, expected in old['a89_dependencies'].items():
        path = ROOT.parent/'a89'/name
        if digest(path) != expected:
            raise ValueError('measured A89 dependency changed: '+name)
        data['a89/'+name] = path.read_bytes()
    output = Path(sys.argv[1])
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(data.items()):
            if b'\r\n' in content:
                raise ValueError('CRLF source rejected: '+name)
            info = zipfile.ZipInfo(name,(2026,10,28,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info,content)
    print('NODE_PACKAGE='+str(output))
    print('SHA256='+digest(output))


if __name__ == '__main__':
    main()
