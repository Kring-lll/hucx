#!/usr/bin/env python3
"""Build a deterministic tool-only node zip; preserve the tested installation."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

from coll_matrix import SOURCE_BASE

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('am_builder', PROJECT / 'am_rndv/build_zip.py')
SHARED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARED)


def entry():
    text = SHARED.ENTRY
    replacements = {
        'r1_shared_80b28a25b_dvKqlu': 'r1_shared_c7ef02b51_vkSZ69',
        "prefix='toolkit_b_am_rndv_'": "prefix='toolkit_b_collective_'",
        'am_rndv_manifest.json': 'collective_manifest.json',
        "str(root / 'am_rndv')": "str(root / 'collective_matrix')",
        "str(root / 'am_rndv/run_rndv.py')": "str(root / 'collective_matrix/run_collective.py')"}
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise ValueError('shared zip entry changed: ' + old)
        text = text.replace(old, new, 1)
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('output already exists; preserve it')
    paths = {}
    for folder in ('collective_matrix', 'am_rndv', 'window_matrix', 'segment_default',
                   'wr_probe', 'boundary', 'osu_compare'):
        for path in sorted((PROJECT / folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo', '.so'):
                paths[str(path.relative_to(PROJECT))] = path.read_bytes()
    shared = json.loads((PROJECT / 'osu_compare/manifest.json').read_text())
    for name, expected in shared['a89_dependencies'].items():
        data = (PROJECT / 'a89' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('shared A89 source changed: ' + name)
        paths['a89/' + name] = data
    manifest = {'schema': 1, 'source_base': SOURCE_BASE, 'node_acceptance': 'PENDING',
                'product_unchanged': True,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(paths.items())}}
    paths['collective_manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    paths['__main__.py'] = entry().encode()
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
