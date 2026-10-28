#!/usr/bin/env python3
"""Deterministic tool-only zipapp. Original OSU/product binaries are not bundled."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
DEPENDENCIES = ('artifacts.py', 'support.py', 'rank_exec.py', 'run_scale.py', 'resume.py')
ENTRY = r'''
import pathlib
import runpy
import sys
import tempfile
import zipfile

case = pathlib.Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz')
if '--plan' in sys.argv or '--help' in sys.argv:
    target = pathlib.Path(tempfile.mkdtemp(prefix='hucx_osu_plan_'))
else:
    if not case.is_dir():
        raise SystemExit('固定已测CASE不存在；不会替换成其他安装')
    target = pathlib.Path(tempfile.mkdtemp(prefix='toolkit_osu_', dir=case))
with zipfile.ZipFile(sys.argv[0]) as archive:
    for name in archive.namelist():
        if name.startswith('/') or '..' in pathlib.PurePosixPath(name).parts:
            raise SystemExit('工具包路径越界')
    archive.extractall(target)
sys.path.insert(0, str(target / 'osu_compare'))
entry = 'scale_rm.py' if '--rm-scale' in sys.argv else 'run.py'
if '--rm-scale' in sys.argv:
    sys.argv.remove('--rm-scale')
runpy.run_path(str(target / 'osu_compare' / entry), run_name='__main__')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    files = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(ROOT.rglob('*')) if p.is_file() and '__pycache__' not in p.parts
             and p.name != 'manifest.json'}
    dependencies = {name: hashlib.sha256((ROOT.parent / 'a89' / name).read_bytes()).hexdigest()
                    for name in DEPENDENCIES}
    manifest = {'date': '2026-10-27', 'source_base': '2d04999fde719d54b769b43b6839f30aff09aae8',
                'product_commit': '2a8e3b771211e5af491c8ca483ebcc01a03c2f01',
                'node_acceptance': 'PENDING', 'files': files, 'a89_dependencies': dependencies}
    path = ROOT / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    data = {'__main__.py': ENTRY.encode(), 'osu_compare/manifest.json': path.read_bytes()}
    data.update({'osu_compare/' + name: (ROOT / name).read_bytes() for name in files})
    data.update({'a89/' + name: (ROOT.parent / 'a89' / name).read_bytes() for name in dependencies})
    with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in sorted(data.items()):
            if b'\r\n' in contents:
                raise ValueError('CRLF tool source rejected: ' + name)
            info = zipfile.ZipInfo(name, (2026, 10, 27, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, contents)
    print('NODE_PACKAGE=' + str(args.output))
    print('SHA256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
