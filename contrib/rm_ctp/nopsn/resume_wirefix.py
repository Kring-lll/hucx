"""Pinned observer-only recovery; original candidate source/install stay unchanged."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import zipfile

CASE=Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-nopsn-dev/r1_shared_370bbc3bb_HkyfXO')
COMMIT='370bbc3bbf1f343e98af7dab49c8db19b6fcd55e'
MODULE='5b85f1087b3431c7ebc82a314cc8223317b7534a5f58127da01dcc86007714aa'
ARCHIVE='28a14d33ed52e34b27f2e0762a79dffde0c8cb1bd611774042abcb10a4c16af5'
PRIOR_NAME='nopsn_accept_lriahtwi'
ENTRY='contrib/rm_ctp/nopsn/run_candidate.py'


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def review_failure(case):
    root=case/PRIOR_NAME;path=root/'results.json';report=json.loads(path.read_text())
    rows=report.get('trials',[])
    if (report.get('status')!='STOPPED_INCOMPLETE' or len(rows)!=1 or report.get('comparisons')
            or report.get('products',{}).get('nopsn',{}).get('source_commit')!=COMMIT
            or report['products']['nopsn'].get('module_sha256')!=MODULE):
        raise ValueError('prior observer stop identity/coverage differs')
    row=rows[0];run=(root/row['name']).resolve()
    if (run.parent!=root.resolve() or row.get('passed') is not False or row.get('variant')!='full'
            or row.get('benchmark')!='osu_latency' or row.get('ppn')!=1
            or row.get('phase')!='validation' or row.get('wire_observed') is not True
            or 'EXIT=121' not in row.get('error','')
            or json.loads((run/'trial.json').read_text())!=row):
        raise ValueError('prior failure is not the recorded first full-RM observer EXIT121')
    return {'path':str(path),'sha256':digest(path),'launcher_log_sha256':digest(run/'launcher.log'),
        'prior_failed_tasks':1,'prior_promoted_to_pass':False,'prior_samples_merged':False}


def load_controller(case,payload,meta):
    case=Path(case).resolve()
    if case!=CASE.resolve():raise ValueError('unexpected candidate case')
    entry=case/'source'/ENTRY
    if digest(entry)!=meta['original_controller_sha256']:raise ValueError('original controller changed')
    if hashlib.sha256(payload).hexdigest()!=meta['files']['patched_runner.py']:
        raise ValueError('fixed controller changed')
    namespace={'__name__':'nopsn_memory_wirefix','__file__':str(entry)}
    exec(compile(payload,str(entry),'exec'),namespace)
    build=namespace['validate_candidate'](case,COMMIT)
    if (build['archive_sha256']!=ARCHIVE or build['module_sha256']!=MODULE):
        raise ValueError('built archive/module differs from reported ARM candidate')
    return namespace


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',action='store_true');p.add_argument('--timing',action='store_true')
    args=p.parse_args()
    if not args.run:
        print(json.dumps({'case':str(CASE),'product_commit':COMMIT,'module_sha256':MODULE,
            'product_rebuild':False,'original_source_overwritten':False,'new_mpi':215 if args.timing else 47},indent=2));return
    if os.geteuid()!=0 or socket.gethostname()!='node89':raise ValueError('run as root on node89')
    sys.dont_write_bytecode=True
    with zipfile.ZipFile(Path(__file__).parent) as z:
        meta=json.loads(z.read('metadata.json'))
        files={name:z.read(name) for name in meta['files']}
    for name,payload in files.items():
        if Path(name).name!=name or hashlib.sha256(payload).hexdigest()!=meta['files'][name]:
            raise ValueError('tool package hash/path differs: '+name)
    namespace=load_controller(CASE,files['patched_runner.py'],meta)
    historical=review_failure(CASE)
    toolroot=Path(tempfile.mkdtemp(prefix='nopsn_wirefix_tools_',dir=CASE))
    for name,payload in files.items():(toolroot/name).write_bytes(payload)
    (toolroot/'metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
    clean_env={k:v for k,v in os.environ.items() if k not in ('LD_PRELOAD','LD_DEBUG','LD_LIBRARY_PATH','FAKE_ERROR')}
    command=[sys.executable,'-B',str(toolroot/'test_wire.py'),'--include','/usr/include/ub/umdk/urma']
    with (toolroot/'wire_cpu_check.log').open('w') as log:
        result=subprocess.run(command,env=clean_env,stdout=log,stderr=subprocess.STDOUT,timeout=60)
    if result.returncode:raise RuntimeError('observer CPU forwarding check failed; see '+str(toolroot/'wire_cpu_check.log'))
    namespace['WIRE_PROBE_SOURCE']=toolroot/'wire_probe.c'
    original_dump=namespace['dump']
    def dump_with_proof(path,value):
        if Path(path).name=='results.json':
            value['observer_wirefix']={'tool_commit':meta['tool_commit'],'tool_directory':str(toolroot),
                'files':meta['files'],'product_rebuild':False,'original_source_overwritten':False,
                'cpu_check':'GLOBAL_LOCAL_VERSIONED_ERRORS_MISSING_14_PASS','historical_failure':historical}
        return original_dump(path,value)
    namespace['dump']=dump_with_proof
    sys.argv=[str(CASE/'source'/ENTRY),str(CASE),'--expected-commit',COMMIT,'--run']+(['--timing'] if args.timing else [])
    print('SOURCE_MODULE_AND_OBSERVER_FIX=PASS PRODUCT_REBUILD=NO MEMORY_ONLY_CONTROLLER=YES',flush=True)
    print('OBSERVER_CPU_FORWARDING=14/14 PASS; PRIOR_FAILED_SAMPLE_PRESERVED=YES NEW_MPI='+str(215 if args.timing else 47),flush=True)
    print('NOPSN_WIREFIX_TOOLS_DIR='+str(toolroot),flush=True)
    try:namespace['main']()
    finally:
        if (digest(CASE/'source'/ENTRY)!=meta['original_controller_sha256']
                or digest(CASE/'install/lib/ucx/libuct_ub.so.0.0.0')!=MODULE):
            raise RuntimeError('original source/module changed during recovery')


if __name__=='__main__':main()
