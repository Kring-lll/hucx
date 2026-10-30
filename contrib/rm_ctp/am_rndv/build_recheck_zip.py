#!/usr/bin/env python3
"""Build a deterministic, one-task node package using the existing c7ef install."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

import build_zip as shared

PROJECT = shared.PROJECT
ENTRY = shared.ENTRY.replace('r1_shared_80b28a25b_dvKqlu', 'r1_shared_c7ef02b51_vkSZ69')
ENTRY = ENTRY.replace("prefix='toolkit_b_am_rndv_'", "prefix='toolkit_am_log_recheck_'")
ENTRY = ENTRY.replace("str(root / 'am_rndv/run_rndv.py')", "str(root / 'am_rndv/recheck_last.py')")


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
    dependencies = json.loads((PROJECT / 'osu_compare/manifest.json').read_text())
    for name, expected in dependencies['a89_dependencies'].items():
        data = (PROJECT / 'a89' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('reused A89 source differs: ' + name)
        paths['a89/' + name] = data
    manifest = {'schema': 1, 'tool_source_base': 'c7ef02b51d274e4ff3151457b82cc34f493419d5',
                'node_acceptance': 'PENDING', 'product_unchanged': True,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(paths.items())}}
    paths['am_rndv_manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    paths['__main__.py'] = ENTRY.encode()
    with zipfile.ZipFile(args.output, 'w') as archive:
        for name, data in sorted(paths.items()):
            if b'\r\n' in data:
                raise ValueError('LF source required: ' + name)
            info = zipfile.ZipInfo(name, (2026, 10, 30, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    print('NODE_PACKAGE=' + str(args.output))
    print('SHA256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
