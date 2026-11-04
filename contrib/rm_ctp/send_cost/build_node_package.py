#!/usr/bin/env python3
"""Build a tool-only node ZIP from one committed, reviewable Git snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[2]


def git(*arguments):
    return subprocess.check_output(['git', '-C', str(REPOSITORY), *arguments])


def build(destination):
    if destination.exists():
        raise ValueError('destination already exists; preserve it')
    commit = git('rev-parse', 'HEAD').decode().strip()
    names = git('ls-tree', '-r', '--name-only', '-z', commit, 'contrib/rm_ctp').decode().strip('\0').split('\0')
    files = {name: git('show', commit + ':' + name) for name in names}
    manifest = {'tool_commit': commit, 'source_commit': 'a2b4b07c12be5401afaf427877aadd6752871810',
                'module_sha256': '2543923f6b2cae2a37365d293e715bc63fefa539ecc8b146880b6270b0351aa0', 'plan': __import__('run_cost').plan(),
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        entries = {'__main__.py': files['contrib/rm_ctp/send_cost/node_entry.py'],
                   'package_manifest.json': (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()}
        entries.update({'payload/' + name: data for name, data in files.items()})
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (2026, 11, 4, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    manifest = build(args.output.resolve())
    print('TOOL_COMMIT=' + manifest['tool_commit'])
    print('PAYLOAD_FILES=' + str(len(manifest['files'])))
    print('SHA256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
