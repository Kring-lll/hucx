#!/usr/bin/env python3
"""Build a deterministic zipapp from tracked source; no transport binaries."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
REGRESSION = ROOT.parent / 'r1'
REGRESSION_FILES = ('a7_iface_four_ep_20261026.py', 'a7_iface_lifecycle_20261026.py',
                    'a7_iface_lifecycle.c', 'a5_mpi_phase_20261026.py', 'a5_mpi_phase.c')

ENTRY = r'''
import pathlib
import runpy
import sys
import tempfile
import zipfile

case = pathlib.Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz')
if '--plan' in sys.argv:
    target = pathlib.Path(tempfile.mkdtemp(prefix='hucx_a89_plan_'))
else:
    if not case.is_dir():
        raise SystemExit('固定已测CASE不存在；不会猜测其他安装')
    target = pathlib.Path(tempfile.mkdtemp(prefix='toolkit_a89_', dir=str(case)))
with zipfile.ZipFile(sys.argv[0]) as archive:
    for name in archive.namelist():
        if name.startswith('/') or '..' in pathlib.PurePosixPath(name).parts:
            raise SystemExit('工具包路径越界')
    archive.extractall(target)
sys.path.insert(0, str(target / 'a89'))
runpy.run_path(str(target / 'a89/run_scale.py'), run_name='__main__')
'''


def manifest():
    files = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(ROOT.rglob('*')) if path.is_file() and
             '__pycache__' not in path.parts and path.name != 'toolkit_manifest.json'}
    regressions = {name: hashlib.sha256((REGRESSION / name).read_bytes()).hexdigest()
                   for name in REGRESSION_FILES}
    value = {'date': '2026-10-27', 'development_base': '170d43cfa5cef3bf8b36367c220caf3e53d19a88',
             'product_commit': '2a8e3b771211e5af491c8ca483ebcc01a03c2f01',
             'node_acceptance': 'PENDING', 'files': files, 'regression_files': regressions}
    (ROOT / 'toolkit_manifest.json').write_text(json.dumps(value, indent=2) + '\n')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    value = manifest()
    contents = {'__main__.py': ENTRY.encode(),
                'a89/toolkit_manifest.json': (ROOT / 'toolkit_manifest.json').read_bytes()}
    contents.update({'a89/' + name: (ROOT / name).read_bytes() for name in value['files']})
    contents.update({'a89/regression/' + name: (REGRESSION / name).read_bytes()
                     for name in REGRESSION_FILES})
    with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(contents.items()):
            if b'\r\n' in data:
                raise ValueError('CRLF source is not accepted: ' + name)
            info = zipfile.ZipInfo(name, (2026, 10, 27, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, data)
    print('TOOLKIT=' + str(args.output))
    print('SHA256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
