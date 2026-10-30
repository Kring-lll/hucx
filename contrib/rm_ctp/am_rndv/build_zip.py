#!/usr/bin/env python3
"""Deterministic node zipapp, reusing unchanged OSU/observer infrastructure."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
ENTRY = '''import hashlib
import json
from pathlib import Path
import runpy
import sys
import tempfile
import zipfile

case = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_80b28a25b_dvKqlu')
node_action = '--run' in sys.argv or '--preflight-only' in sys.argv
if node_action and not case.is_dir():
    raise SystemExit('固定B已测CASE不存在；本包不创建或覆盖传输库')
root = Path(tempfile.mkdtemp(prefix='toolkit_b_am_rndv_', dir=case if node_action else None))
with zipfile.ZipFile(sys.argv[0]) as archive:
    members = archive.namelist()
    if len(members) != len(set(members)) or any(
            name.startswith('/') or '..' in Path(name).parts or '\\\\' in name for name in members):
        raise SystemExit('工具包条目异常')
    archive.extractall(root)
manifest = json.loads((root / 'am_rndv_manifest.json').read_text())
for name, expected in manifest['files'].items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('工具文件哈希不符：' + name)
sys.path.insert(0, str(root / 'am_rndv'))
runpy.run_path(str(root / 'am_rndv/run_rndv.py'), run_name='__main__')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('output already exists; preserve it')
    paths = {}
    for folder in ('am_rndv', 'window_matrix', 'segment_default', 'wr_probe', 'boundary', 'osu_compare'):
        for path in sorted((PROJECT / folder).rglob('*')):
            if (path.is_file() and '__pycache__' not in path.parts
                    and path.suffix not in ('.pyc', '.pyo', '.so')):
                paths[str(path.relative_to(PROJECT))] = path.read_bytes()
    shared = json.loads((PROJECT / 'osu_compare/manifest.json').read_text())
    for name, expected in shared['a89_dependencies'].items():
        data = (PROJECT / 'a89' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('reused A89 file differs: ' + name)
        paths['a89/' + name] = data
    manifest = {'schema': 1, 'tool_source_base': '9e1a3a6b5ed4074ac162258397065fd0f54831e0',
                'node_acceptance': 'PENDING', 'product_unchanged': True,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(paths.items())}}
    paths['am_rndv_manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    paths['__main__.py'] = ENTRY.encode()
    with zipfile.ZipFile(args.output, 'w') as archive:
        for name, data in sorted(paths.items()):
            if b'\r\n' in data:
                raise ValueError('LF required: ' + name)
            info = zipfile.ZipInfo(name, (2026, 10, 30, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    print('NODE_PACKAGE=' + str(args.output))
    print('SHA256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
