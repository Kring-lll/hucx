"""Review 13 completed gates, then run six remaining tasks with a bounded observer."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import zipfile
import resume_timefix as guard

PRIOR = guard.CASE / 'um_send_accept_93f7bxv2'
TOOLS_BASE = 'dff7593a25bf0be78b591576ec864619d1c8aa41'


def normalize_row(row):
    value=copy.deepcopy(row)
    value['rank_evidence']['pids']={int(k):v for k,v in value['rank_evidence']['pids'].items()}
    return value


def checkpoint_shape(report, specs):
    if report.get('source_commit')!=guard.COMMIT or report.get('module_sha256')!=guard.MODULE_SHA:
        raise ValueError('prior product identity differs')
    if report.get('status')!='STOPPED_INCOMPLETE' or report.get('timing'):
        raise ValueError('prior checkpoint is not the recorded functional stop')
    rows=report.get('cases',[])
    if len(rows)!=14 or len(specs)!=19:raise ValueError('prior task coverage differs')
    for index,(row,spec) in enumerate(zip(rows,specs)):
        profile,benchmark,ppn,size,fault=spec
        size_args=row['configuration']['arguments']
        if (row.get('profile')!=profile or row.get('benchmark')!=benchmark or row.get('ppn')!=ppn
                or row.get('software_fault') is not fault
                or size_args[size_args.index('--message-size')+1]!='%d:%d'%(size,size)):
            raise ValueError('prior task differs at index '+str(index))
        if row.get('phase')!='validation' or row.get('exit')!=0 or row.get('cleanup_error') or not row.get('output',{}).get('validation_pass'):
            raise ValueError('prior task data/exit/cleanup failed')
        if index<13 and row.get('passed') is not True:raise ValueError('prior completed gate failed')
    last=rows[-1]
    if last.get('passed') is not False or 'protocol receipt identity/coverage failed' not in last.get('error',''):
        raise ValueError('prior 1MiB is not the recorded probe-only failure')
    if report.get('controller_timefix',{}).get('fixed_runner_sha256')!='c36157064a0c389f0023ea808a4b96c5cc6b742e4320506faf5daa007ab8d612':
        raise ValueError('prior controller identity differs')
    return rows[:13]


def review_checkpoint(engine):
    root=PRIOR.resolve()
    if root.parent!=guard.CASE.resolve():raise ValueError('checkpoint outside candidate')
    path=root/'results.json';report=json.loads(path.read_text())
    rows=checkpoint_shape(report,engine['plan'](False))
    old_probe=root/'libum_send_probe.so'
    if guard.digest(old_probe)!=report['tool_hashes']['libum_send_probe.so']:
        raise ValueError('prior observer changed')
    receipts=[]
    for index,row in enumerate(rows):
        run=(root/row['name']).resolve()
        if run.parent!=root:raise ValueError('prior task path escapes checkpoint')
        disk=json.loads((run/'trial.json').read_text())
        if disk!=row:raise ValueError('prior trial differs from checkpoint')
        current=normalize_row(row);ppn=row['ppn'];profile,benchmark,_,size,_=engine['plan'](False)[index]
        engine['PROFILE']=profile
        for host in ('node89','node97'):
            engine['check_config']((run/(host+'_ucx_config.txt')).read_text(),row['configuration']['environment'])
        parsed=engine['osu'].parse_output((run/'launcher.log').read_text(errors='replace'),
                benchmark,True,row['iterations'],size)
        if parsed!=row['output']:raise ValueError('prior original OSU output changed')
        ranks=engine['osu'].inspect_ranks(run,ppn,report['preflight']['inventory'],guard.CASE/'install',
                row['configuration']['nonce'],report['preflight']['benchmarks'][benchmark]['mpi_library'])
        if ranks['pids']!=current['rank_evidence']['pids']:raise ValueError('prior rank identity changed')
        proof={'task':row['name'],'trial_sha256':guard.digest(run/'trial.json'),'data_maps_exit':True}
        if profile=='rm':
            observed=engine['wr'].inspect_trial(run,current,old_probe)
            if observed!=row['send_observations']:raise ValueError('prior WR observations changed')
            for rank,pid in current['rank_evidence']['pids'].items():
                value=json.loads((run/('protocol_rank%d_pid%d.json'%(rank,pid))).read_text())
                if (value['rank']!=rank or value['pid']!=pid or value['nonce']!=row['configuration']['nonce']
                        or value['header_bytes']!=8 or value['key_overflow'] or value['duplicate_failed']
                        or any(value[k] for k in ('data_drop','ack_drop','duplicate'))):
                    raise ValueError('prior non-injection protocol evidence invalid')
            if size==8:
                if any(24 not in v['lengths'] or v['inline_wrs']<=0 for v in observed):
                    raise ValueError('prior 8B inline evidence changed')
            proof['wr_observations']=len(observed)
        receipts.append(proof)
    return {'source_result':str(path),'source_result_sha256':guard.digest(path),
        'reused_passed_tasks':13,'receipts':receipts,'legacy_retry_numbers_used':False,
        'prior_1MiB_promoted_to_pass':False,'prior_1MiB_partial':report['cases'][-1]['output']}


def main():
    parser=argparse.ArgumentParser(description='Review prior 13; new observer, six remaining MPI and optional timing')
    parser.add_argument('--run',action='store_true');parser.add_argument('--timing',action='store_true')
    args=parser.parse_args()
    if not args.run:
        print(json.dumps({'case':str(guard.CASE),'prior_review':13,'new_functional_mpi':6,
            'timing_mpi':10 if args.timing else 0,'product_rebuild':False,'retry_method':'PINNED_UM_SGE_LAYOUT'},indent=2));return
    if os.geteuid()!=0 or socket.gethostname()!='node89':raise ValueError('run as root on node89')
    sys.dont_write_bytecode=True
    with zipfile.ZipFile(Path(__file__).parent) as z:
        metadata=json.loads(z.read('metadata.json'));payload=z.read('patched_runner.py')
        guard.FIXED_SHA=metadata['runner_sha256']
        engine=guard.load_controller(guard.CASE,payload)
        checkpoint=review_checkpoint(engine)
        toolroot=Path(tempfile.mkdtemp(prefix='um_probe_tools_',dir=guard.CASE))
        (toolroot/'um_send').mkdir();(toolroot/'wr_probe').mkdir()
        for name in ('probe.c','probe_layout.h'):
            (toolroot/'um_send'/name).write_bytes(z.read(name))
        shutil.copy2(guard.CASE/'source/contrib/rm_ctp/wr_probe/send_probe.c',toolroot/'wr_probe/send_probe.c')
    engine['PROBE_SOURCE']=toolroot/'um_send/probe.c'
    full_plan=engine['plan']
    engine['plan']=lambda with_um=False: full_plan(False)[13:] if not with_um else (_ for _ in ()).throw(ValueError('resume does not add UM control'))
    base=engine['osu'].base;original_write=base.write_json
    def write_review(path,value):
        if Path(path).name=='results.json' and Path(path).parent.name.startswith('um_send_accept_'):
            value['checkpoint']=checkpoint
            value['controller_probe_fix']={'runner_sha256':metadata['runner_sha256'],
                'probe_sha256':metadata['probe_sha256'],'product_rebuild':False,
                'memory_only_controller':True,'retry_method':'PINNED_UM_SGE_LAYOUT'}
            value['coverage']={'prior_reviewed':13,'new_passed':sum(r.get('passed') is True for r in value['cases']),
                'selected_total':19}
            if value['status']=='SELECTED_UM_DERIVED_RM_SEND_PASS_FULL_ACCEPTANCE_PENDING':
                value['status']='PRIOR_13_AND_REMAINING_6_PASS_FULL_ACCEPTANCE_PENDING'
        return original_write(path,value)
    base.write_json=write_review
    for key in ('HUCX_UM_SEND_FAULT','HUCX_UM_SEND_MODULE'):os.environ.pop(key,None)
    os.environ.update(OMPI_ALLOW_RUN_AS_ROOT='1',OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    sys.argv=[str(guard.CASE/'source'/guard.RELATIVE_ENTRY),str(guard.CASE),'--run']+(['--timing'] if args.timing else [])
    print('PRIOR_13_REVIEW=PASS PRODUCT_REBUILD=NO NEW_FUNCTIONAL_MPI=6',flush=True)
    try:engine['main']()
    finally:
        base.write_json=original_write
        if guard.digest(guard.CASE/'source'/guard.RELATIVE_ENTRY)!=guard.ENTRY_SHA or guard.digest(guard.CASE/'install/lib/ucx/libuct_ub.so.0.0.0')!=guard.MODULE_SHA:
            raise RuntimeError('original candidate changed during run')

if __name__=='__main__':main()
