#!/usr/bin/env python3
"""Deterministic, hash-checked tool overlay; never overwrite CASE/source/install."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('rkey_am_zip_reference', PROJECT / 'am_rndv/build_zip.py')
SHARED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARED)


def entry():
    text = SHARED.ENTRY
    for old, new in (
            ('r1_shared_80b28a25b_dvKqlu', 'r1_shared_ac4146054_sbFyVf'),
            ("prefix='toolkit_b_am_rndv_'", "prefix='toolkit_rkey_loglimit_'"),
            ('am_rndv_manifest.json', 'rkey_loglimit_manifest.json'),
            ("str(root / 'am_rndv')", "str(root / 'rkey_lifecycle')"),
            ("str(root / 'am_rndv/run_rndv.py')", "str(root / 'rkey_lifecycle/resume_large_log.py')")):
        if text.count(old) != 1:
            raise ValueError('shared ZIP entry changed: ' + old)
        text = text.replace(old, new, 1)
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('output exists; preserve it')
    paths = {}
    for folder in ('rkey_lifecycle', 'pending_order', 'collective_matrix', 'am_rndv',
                   'window_matrix', 'segment_default', 'wr_probe', 'boundary', 'osu_compare'):
        for path in sorted((PROJECT / folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo', '.so'):
                paths[str(path.relative_to(PROJECT))] = path.read_bytes()
    shared = json.loads((PROJECT / 'osu_compare/manifest.json').read_text())
    for name, expected in shared['a89_dependencies'].items():
        data = (PROJECT / 'a89' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('shared A89 file changed: ' + name)
        paths['a89/' + name] = data
    manifest = {'source_base': '5e0b3727a1720e068bdee54f218ce540cc2f360a',
                'product_commit': 'ac4146054b0e1364d0ea691b37b9f14238a43bb0',
                'product_unchanged': True, 'node_acceptance': 'PENDING',
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in paths.items()}}
    paths['rkey_loglimit_manifest.json'] = (json.dumps(manifest, sort_keys=True, indent=2)+'\n').encode()
    paths['__main__.py'] = entry().encode()
    with zipfile.ZipFile(args.output, 'w') as archive:
        for name, data in sorted(paths.items()):
            if b'\r\n' in data:
                raise ValueError('LF required: ' + name)
            info = zipfile.ZipInfo(name, (2026, 10, 31, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    print('NODE_PACKAGE='+str(args.output))
    print('SHA256='+hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
