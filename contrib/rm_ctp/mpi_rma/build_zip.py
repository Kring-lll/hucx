#!/usr/bin/env python3
"""Deterministic, self-verifying tool-only package. No product installation."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
ENTRY = '''import hashlib,json,pathlib,runpy,sys,tempfile,zipfile
case=pathlib.Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_989fd7186_xncBHX')
# A plain plan needs no node files, SSH, MPI, compilation or temporary directory.
if not any(x in sys.argv[1:] for x in ('--run','--preflight-only')):
    print('PLAN: existing 989 product; two nodes PPN1; MPI guarded fence/lock_all, software AMO, original OSU RMA; --run enables functional diagnostics; optional --timing adds separate measurements')
    raise SystemExit(0)
if not case.is_dir():
    raise SystemExit('accepted 989 CASE missing; do not substitute another installation')
archive=zipfile.ZipFile(sys.argv[0])
manifest=json.loads(archive.read('mpi_rma_manifest.json'))
names=archive.namelist()
if len(names)!=len(set(names)) or set(names)!=set(manifest['files'])|{'mpi_rma_manifest.json','__main__.py'}:
    raise SystemExit('package member coverage mismatch')
for name,expected in manifest['files'].items():
    path=pathlib.PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or hashlib.sha256(archive.read(name)).hexdigest()!=expected:
        raise SystemExit('package content mismatch: '+name)
if hashlib.sha256(archive.read('__main__.py')).hexdigest()!=manifest['entry_sha256']:
    raise SystemExit('package entry mismatch')
root=pathlib.Path(tempfile.mkdtemp(prefix='toolkit_mpi_rma_',dir=case))
for name in manifest['files']:
    target=root/name
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(archive.read(name))
# No change to accepted CASE/source or install. Tool files live under a new root.
sys.path.insert(0,str(root/'mpi_rma'))
sys.argv=[str(root/'mpi_rma/run.py')]+sys.argv[1:]
runpy.run_path(str(root/'mpi_rma/run.py'),run_name='__main__')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('package exists; preserve it')
    paths = {}
    for folder in ('mpi_rma', 'native_rma', 'native_read', 'native_write', 'segment_default',
                   'wr_probe', 'boundary', 'osu_compare', 'a89'):
        for path in sorted((PROJECT / folder).rglob('*')):
            if (path.is_file() and '__pycache__' not in path.parts and
                    path.suffix in ('.py', '.c', '.h', '.md', '.json', '.sh')):
                paths[str(path.relative_to(PROJECT))] = path.read_bytes()
    manifest = {'schema': 1, 'tool_source_base': 'd2f2dd6b0ca1194be98e780d879868e3de7fe062',
                'product_commit': '989fd71865046bde988603c97416dc133c582e88',
                'product_unchanged': True, 'node_acceptance': 'PENDING',
                'entry_sha256': hashlib.sha256(ENTRY.encode()).hexdigest(),
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(paths.items())}}
    paths['mpi_rma_manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    paths['__main__.py'] = ENTRY.encode()
    with zipfile.ZipFile(args.output, 'w') as archive:
        for name, data in sorted(paths.items()):
            if b'\r\n' in data:
                raise ValueError('CRLF in package source: ' + name)
            info = zipfile.ZipInfo(name, (2026, 11, 2, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    print('NODE_PACKAGE=' + str(args.output))
    print('SHA256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
