#!/usr/bin/env python3
"""ZIP entry: validate payload, stage tools separately, reuse accepted install."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import zipfile

DEFAULT_CASE = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-c-send-reliability-dev/r1_shared_97b503287_f1PZta')


def sha(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            hasher.update(block)
    return hasher.hexdigest()


def validate_payload(archive, manifest):
    members = archive.infolist()
    names = [item.filename for item in members]
    if len(set(names)) != len(names):
        raise ValueError('duplicate ZIP members')
    expected_names = {'__main__.py', 'package_manifest.json'} | {'payload/' + name for name in manifest['files']}
    if set(names) != expected_names:
        raise ValueError('unexpected/missing ZIP payload members')
    for item in members:
        path = PurePosixPath(item.filename)
        if path.is_absolute() or '..' in path.parts or '\\' in item.filename or item.is_dir():
            raise ValueError('unsafe ZIP member: ' + item.filename)
        mode = item.external_attr >> 16
        if mode & 0o170000 == 0o120000:
            raise ValueError('ZIP symlink refused')
        if item.file_size > 16 * 1024 * 1024:
            raise ValueError('unexpected oversized tool payload')
    for name, expected in manifest['files'].items():
        if not name.startswith('contrib/rm_ctp/'):
            raise ValueError('payload outside test-tool directory')
        if hashlib.sha256(archive.read('payload/' + name)).hexdigest() != expected:
            raise ValueError('payload hash mismatch: ' + name)


def validate_product(case, manifest):
    if case.resolve() != DEFAULT_CASE:
        raise ValueError('only the accepted 97b CASE is supported')
    build = json.loads((case / 'build_manifest.json').read_text())
    if (build.get('source_commit') != manifest['source_commit'] or
            build.get('module_sha256') != manifest['module_sha256'] or
            sha(case / 'install/lib/ucx/libuct_ub.so.0.0.0') != manifest['module_sha256']):
        raise ValueError('accepted product/module does not match package')


def main():
    parser = argparse.ArgumentParser(description='Reliable SEND tools; default plan does not touch nodes')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--timing', action='store_true')
    args = parser.parse_args()
    if args.timing and not args.run:
        parser.error('--timing requires --run')
    with zipfile.ZipFile(sys.argv[0]) as archive:
        manifest = json.loads(archive.read('package_manifest.json'))
        validate_payload(archive, manifest)
        if not (args.run or args.preflight_only):
            print(json.dumps(manifest['plan'], ensure_ascii=False, indent=2))
            return
        if os.geteuid() != 0 or socket.gethostname() != 'node89':
            raise ValueError('run only as root on node89')
        validate_product(DEFAULT_CASE, manifest)
        tools = Path(tempfile.mkdtemp(prefix='send_validation_tools_', dir=DEFAULT_CASE))
        for name in manifest['files']:
            target = tools / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(archive.read('payload/' + name))
        receipt = {'tool_commit': manifest['tool_commit'], 'source_commit': manifest['source_commit'],
                   'module_sha256': manifest['module_sha256'], 'package_sha256': sha(sys.argv[0]),
                   'files': manifest['files'], 'accepted_source_or_install_overwritten': False}
        (tools / 'tool_receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print('SEND_TOOLS_DIR=' + str(tools), flush=True)
    runner = tools / 'contrib/rm_ctp/send_validation/run_validation.py'
    option = '--run' if args.run else '--preflight-only'
    raise SystemExit(subprocess.call([sys.executable, '-B', '-u', str(runner), str(DEFAULT_CASE), option] +
                                     (['--timing'] if args.timing else [])))


if __name__ == '__main__':
    main()
