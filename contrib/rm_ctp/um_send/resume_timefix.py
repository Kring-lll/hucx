"""Pinned, memory-only time-format correction for the already built a4d candidate."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import os
import sys
import zipfile

COMMIT = 'a4d07599bd5b66b84c5de8257e5f7fe15e3a6f63'
ARCHIVE_SHA = 'c108cf9d453008fe8759062639a62cdf37a656d4b87430209ed05850ee8a48e5'
MODULE_SHA = '48dcd30a30f29a39d8f9b63e0c2afd7279bdd3da23b1757a610c828b77b04c2f'
ENTRY_SHA = '6a391dd5133bacfa79c66acd390b49fc718050cf0390d1875dfa4273215dea23'
FIXED_SHA = 'c36157064a0c389f0023ea808a4b96c5cc6b742e4320506faf5daa007ab8d612'
CASE = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-um-send-dev/r1_shared_a4d07599b_BaJAce')
RELATIVE_ENTRY = 'contrib/rm_ctp/um_send/run_candidate.py'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_controller(case, payload, module_sha=MODULE_SHA):
    """Validate original source and module before executing the exact fixed runner."""
    case = Path(case).resolve()
    if case != CASE.resolve(): raise ValueError('unexpected candidate directory')
    build = json.loads((case/'build_manifest.json').read_text())
    if (build['source_commit'] != COMMIT or build['archive_sha256'] != ARCHIVE_SHA
            or build['module_sha256'] != module_sha):
        raise ValueError('original build identity mismatch')
    module = case/'install/lib/ucx/libuct_ub.so.0.0.0'
    if digest(module) != module_sha: raise ValueError('original ARM module changed')
    source = case/'source'
    manifest = json.loads((source/'contrib/rm_ctp/r1/candidate_manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts: raise ValueError('manifest path invalid')
        if digest(source/path) != expected: raise ValueError('original source changed: '+name)
    entry = source/RELATIVE_ENTRY
    if digest(entry) != ENTRY_SHA: raise ValueError('original runner not the expected version')
    if hashlib.sha256(payload).hexdigest() != FIXED_SHA: raise ValueError('fixed runner hash differs')
    namespace = {'__name__':'um_send_memory_timefix','__file__':str(entry)}
    exec(compile(payload,str(entry),'exec'),namespace)
    return namespace


def main():
    parser=argparse.ArgumentParser(description='a4d ARM install reuse; time-unit correction only')
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--timing',action='store_true')
    args=parser.parse_args()
    if not args.run:
        print(json.dumps({'case':str(CASE),'product_commit':COMMIT,'product_rebuild':False,
            'memory_only':True,'functional_tasks':19,'timing_requested':args.timing},indent=2));return
    if os.geteuid()!=0 or socket.gethostname()!='node89': raise ValueError('run as root on node89')
    sys.dont_write_bytecode=True
    with zipfile.ZipFile(Path(__file__).parent) as z: payload=z.read('patched_runner.py')
    namespace=load_controller(CASE,payload)
    base=namespace['osu'].base
    original_write=base.write_json
    proof={'kind':'UCX_TIME_UNIT_NORMALIZATION','fixed_runner_sha256':FIXED_SHA,
        'original_runner_sha256':ENTRY_SHA,'memory_only':True,'product_rebuild':False,
        'prior_result':str(CASE/'um_send_accept_6aycbjyk/results.json'),
        'prior_result_reused_as_pass':False}
    def write_with_proof(path,value):
        if Path(path).name=='results.json' and str(Path(path).parent.name).startswith('um_send_accept_'):
            value['controller_timefix']=proof
        return original_write(path,value)
    base.write_json=write_with_proof
    for key in ('HUCX_UM_SEND_FAULT','HUCX_UM_SEND_MODULE'):os.environ.pop(key,None)
    os.environ.update(OMPI_ALLOW_RUN_AS_ROOT='1',OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    sys.argv=[str(CASE/'source'/RELATIVE_ENTRY),str(CASE),'--run']+(['--timing'] if args.timing else [])
    print('SOURCE_MODULE_AND_TIME_UNITS=PASS MEMORY_ONLY=YES PRODUCT_REBUILD=NO',flush=True)
    try: namespace['main']()
    finally:
        base.write_json=original_write
        if digest(CASE/'source'/RELATIVE_ENTRY)!=ENTRY_SHA or digest(CASE/'install/lib/ucx/libuct_ub.so.0.0.0')!=MODULE_SHA:
            raise RuntimeError('original source/module changed during run')
