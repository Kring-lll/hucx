#!/usr/bin/env python3
"""Build a deterministic tools-only overlay for the already built 343d CASE."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
CASE = '/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_343d934fe_vpkpQc'
COMMIT = '343d934fea013fb98c4bb5465b6eeb8aef39ac44'
MODULE = '5373b75bbfc7429ba34f9f84191886ba1eaaf0b0bde2308a76ab5ed7e8b0122c'

def main():
    destination = Path(sys.argv[1])
    if destination.exists():
        raise ValueError('output exists; preserve it')
    controller = (ROOT / 'normal_validation.py').read_bytes()
    digest = hashlib.sha256(controller).hexdigest()
    entry = '''import hashlib,json,sys,zipfile
from pathlib import Path
case=Path(CASE_VALUE)
if sys.argv[1:] != ['--run']:
    if sys.argv[1:]: raise SystemExit('usage: python3 PACKAGE.zip [--run]')
    print('PLAN=prior WRITE proof reused; one WRITE-enabled normal 28 matrix; RMA files/OSC inventory only; no WRITE rerun or rebuild')
    raise SystemExit(0)
build=json.loads((case/'build_manifest.json').read_text())
assert build['source_commit']==COMMIT_VALUE,'wrong product commit'
assert build['module_sha256']==MODULE_VALUE,'wrong module identity'
manifest=json.loads((case/'source/contrib/rm_ctp/r1/candidate_manifest.json').read_text())
for name,expected in manifest['files'].items():
    assert hashlib.sha256((case/'source'/name).read_bytes()).hexdigest()==expected,name
assert hashlib.sha256((case/'install/lib/ucx/libuct_ub.so.0.0.0').read_bytes()).hexdigest()==MODULE_VALUE,'module changed'
with zipfile.ZipFile(sys.argv[0]) as archive:
    code=archive.read('normal_validation.py')
assert hashlib.sha256(code).hexdigest()==CONTROLLER_VALUE,'overlay changed'
source=case/'source/contrib/rm_ctp/native_write/normal_validation.py'
sys.argv=[str(source),'--run']
exec(compile(code,str(source),'exec'),{'__file__':str(source),'__name__':'__main__','OVERLAY_SHA256':CONTROLLER_VALUE,'OVERLAY_CODE':code.decode('utf-8')})
'''
    for key, value in [('CASE_VALUE', CASE), ('COMMIT_VALUE', COMMIT), ('MODULE_VALUE', MODULE), ('CONTROLLER_VALUE', digest)]:
        entry = entry.replace(key, repr(value))
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in [('__main__.py', entry.encode()), ('normal_validation.py', controller)]:
            info = zipfile.ZipInfo(name, (2026,10,31,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    print(hashlib.sha256(destination.read_bytes()).hexdigest() + '  ' + str(destination))

if __name__ == '__main__':
    main()
