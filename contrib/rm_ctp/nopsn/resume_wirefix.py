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
PRIOR_NAME='nopsn_accept_fhv5h0ny'
ENTRY='contrib/rm_ctp/nopsn/run_candidate.py'


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def review_failure(case):
    root=case/PRIOR_NAME;path=root/'results.json';report=json.loads(path.read_text())
    rows=report.get('trials',[]);active=report.get('active_task',{})
    if (report.get('status')!='STOPPED_INCOMPLETE' or len(rows)!=3 or report.get('comparisons')
            or report.get('products',{}).get('nopsn',{}).get('source_commit')!=COMMIT
            or report['products']['nopsn'].get('module_sha256')!=MODULE
            or 'unplanned OSU workload' not in report.get('error','')
            or report.get('observer_wirefix',{}).get('tool_commit')!='fea1fbf8d565dc1ced4ea2b43fb6f895d2de2e08'
            or active!={'index':3,'variant':'nopsn','benchmark':'osu_multi_lat','ppn':1,
                'phase':'validation','bytes':64,'wire_observed':False}):
        raise ValueError('prior stop is not the recorded 64B CLI-generation failure')
    receipts=[]
    for index,row in enumerate(rows):
        run=(root/row['name']).resolve();variant='full' if index==0 else 'nopsn'
        if (run.parent!=root.resolve() or row.get('passed') is not True or row.get('exit')!=0
                or row.get('cleanup_error') or row.get('variant')!=variant
                or row.get('benchmark')!='osu_latency' or row.get('ppn')!=1
                or row.get('phase')!='validation' or row.get('message_bytes')!=8
                or row.get('wire_observed') is not (index<2)
                or row.get('output',{}).get('validation_pass') is not True
                or json.loads((run/'trial.json').read_text())!=row):
            raise ValueError('prior completed 8B gate differs')
        if index<2:
            wire=[]
            for rank,pid in sorted((int(r),p) for r,p in row['rank_evidence']['pids'].items()):
                v=json.loads((run/('nopsn_wire_rank%d_pid%d.json'%(rank,pid))).read_text())
                if (v['rank']!=rank or v['pid']!=pid or v['nonce']!=row['configuration']['nonce']
                        or v['errors'] or v['retry_success'] or v['data_success']<=0
                        or v['short_success']<=0 or v['header_mask']!=1<<(8 if index==0 else 6)
                        or v['max_wr_bytes']>4096):raise ValueError('prior observed header receipt differs')
                wire.append(v)
            if len(wire)!=2 or wire!=row['wire_evidence']:raise ValueError('prior wire receipt coverage differs')
        receipts.append({'trial_sha256':digest(run/'trial.json'),'log_sha256':digest(run/'launcher.log')})
    return {'path':str(path),'sha256':digest(path),'completed_8b_gates_reviewed':3,
        'receipts':receipts,'failed_task_started_mpi':False,'prior_promoted_to_pass':False,
        'prior_samples_merged':False,'new_batch_restarts_all_tasks':True}


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
    print('CLI_FIX=FUNCTIONAL_SIZES_ONLY PRODUCT_UNCHANGED=YES',flush=True)
    try:namespace['main']()
    finally:
        if (digest(CASE/'source'/ENTRY)!=meta['original_controller_sha256']
                or digest(CASE/'install/lib/ucx/libuct_ub.so.0.0.0')!=MODULE):
            raise RuntimeError('original source/module changed during recovery')


if __name__=='__main__':main()
