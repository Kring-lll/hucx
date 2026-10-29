#!/usr/bin/env python3
"""Build a pinned standard-OSU boundary tool; no product or OSU binary embedded."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
ENTRY = r'''
import hashlib
import json
from pathlib import Path
import runpy
import sys
import tempfile
import zipfile

case = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz')
if '--run' in sys.argv:
    if not case.is_dir():
        raise SystemExit('固定已测CASE不存在')
    root = Path(tempfile.mkdtemp(prefix='toolkit_wr_probe_', dir=case))
else:
    root = Path(tempfile.mkdtemp(prefix='toolkit_wr_probe_plan_'))
with zipfile.ZipFile(sys.argv[0]) as archive:
    names = archive.namelist()
    if any(name.startswith('/') or '..' in Path(name).parts for name in names):
        raise SystemExit('压缩包有越界路径')
    archive.extractall(root)
manifest = json.loads((root / 'boundary_manifest.json').read_text())
for name, expected in manifest['files'].items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('工具文件哈希不符：' + name)
sys.path.insert(0, str(root / 'boundary'))
sys.path.insert(0, str(root / 'osu_compare'))
runpy.run_path(str(root / 'wr_probe/run_probe.py'), run_name='__main__')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    paths = {}
    for folder in ('wr_probe', 'boundary', 'osu_compare'):
        for path in sorted((PROJECT / folder).rglob('*')):
            if (path.is_file() and '__pycache__' not in path.parts
                    and path.suffix not in ('.pyc', '.pyo', '.so')):
                paths[str(path.relative_to(PROJECT))] = path.read_bytes()
    osu = json.loads((PROJECT / 'osu_compare/manifest.json').read_text())
    for name in osu['a89_dependencies']:
        path = PROJECT / 'a89' / name
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != osu['a89_dependencies'][name]:
            raise ValueError('A89 reused dependency differs: ' + name)
        paths['a89/' + name] = data
    manifest = {'product_commit': '2a8e3b771211e5af491c8ca483ebcc01a03c2f01',
                'standard_osu': '7.5.2', 'node_acceptance': 'PENDING',
                'files': {k: hashlib.sha256(v).hexdigest() for k, v in paths.items()}}
    paths['boundary_manifest.json'] = (json.dumps(manifest, ensure_ascii=False,
                                               sort_keys=True, indent=2) + '\n').encode()
    paths['__main__.py'] = ENTRY.encode()
    with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(paths.items()):
            if b'\r\n' in data:
                raise ValueError('toolkit must use LF: ' + name)
            info = zipfile.ZipInfo(name, (2026, 10, 28, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    print('NODE_ZIP=' + str(args.output))
    print('SHA256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
