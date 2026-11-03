#!/usr/bin/env python3
"""Fixed97b, tool-only SEND diagnosis. All new latencies are diagnostic only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'send_validation'))
import run_validation as previous
integration=previous.integration
osu=previous.osu
CASE=integration.DEFAULT_CASE
CHECKPOINT=CASE/'send_validation_08z6t0e2/results.json'
CHECKPOINT_SHA='5a88cb94e89e1d8494a9382aae8b86684ef53e753ecbd056f10a7da97aff6a13'


def plan():
    return {'product_commit':integration.matrix.COMMIT,'max_ppn':4,'message_bytes':8,
            'workloads':[{'benchmark':name,'ppn':ppn} for name,ppn in
                         (('osu_multi_lat',1),('osu_alltoall',4))],
            'profiles':['iface_v2','reliable_v3'],'validation_tasks':4,'counter_tasks':4,
            'optional_sample_tasks':4,'counter_iterations':20000,'sample_iterations':2000000,
            'rank_timeout_seconds':180,'sample_seconds':3,'sample_frequency':99,
            'default_action':'PLAN_ONLY','product_changed':False,'performance_acceptance':False,
            'checkpoint_sha256':CHECKPOINT_SHA,'rto_changed':False}


def checkpoint():
    if integration.digest(CHECKPOINT)!=CHECKPOINT_SHA:
        raise ValueError('accepted149/6 checkpoint changed; preserve evidence')
    value=json.loads(CHECKPOINT.read_text())
    if (value['source_commit']!=integration.matrix.COMMIT or value['module_sha256']!=integration.matrix.MODULE
            or value['status']!='SELECTED_SEND_FUNCTIONAL_AND_OSU_TIMING_RESOURCES_PASS_REVIEW_REQUIRED'):
        raise ValueError('accepted checkpoint identity/status differs')
    return {'results_json':str(CHECKPOINT),'sha256':CHECKPOINT_SHA,'rerun':False}


def fetch_extra(run, names, optional=False):
    """Small checked one-file SCPs, not the old enormous multi-source command."""
    code='''import hashlib,json,sys
from pathlib import Path
r=Path(sys.argv[1]); names=json.loads(sys.argv[2]); answer={}; optional=sys.argv[3]=="True"
for name in names:
 p=r/name
 if optional and not p.exists() and not p.is_symlink(): continue
 if p.is_symlink() or not p.is_file() or p.stat().st_size>64*1024*1024: raise ValueError("artifact missing/type/size: "+name)
 h=hashlib.sha256()
 with p.open("rb") as f:
  for block in iter(lambda:f.read(1048576),b""): h.update(block)
 answer[name]={"sha256":h.hexdigest(),"size":p.stat().st_size}
print(json.dumps(answer))
'''
    if len(names)!=len(set(names)) or any(not re.fullmatch(r'(?:profile_rank\d+_pid\d+(?:_ready|_end)?\.json|perf_rank\d+\.(?:json|data|log|script|script\.stderr))',n) for n in names):
        raise ValueError('unsafe diagnostic artifact names')
    manifest=json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3" "$4"\n',code,run,json.dumps(names),str(optional)))
    for name in manifest:
        target=run/name
        if target.exists():
            raise ValueError('refuse to overwrite local diagnostic evidence')
        subprocess.run(['scp','-q','-o','BatchMode=yes','-o','ConnectTimeout=8',
                        osu.base.PEER+':'+str(target),str(target)],check=True,timeout=45)
        if target.stat().st_size!=manifest[name]['size'] or integration.digest(target)!=manifest[name]['sha256']:
            raise ValueError('diagnostic transfer identity differs: '+name)
    return manifest


def inspect_counter(value,rank,pid,nonce,profile):
    if value.get('rank')!=rank or value.get('pid')!=pid or value.get('nonce')!=nonce or value.get('finalized') is not True:
        raise ValueError('counter actual process identity/finalization differs')
    for key in ('incomplete','error_calls','process_poll_errors','process_cqe_errors'):
        if value[key]:
            raise ValueError('counter incomplete/provider error: '+key)
    flags=value['send_flags']
    if len(flags)!=4 or {(r['inline'],r['complete_enable']) for r in flags}!={(0,0),(0,1),(1,0),(1,1)}:
        raise ValueError('counter flag coverage invalid')
    if any(r['attempts']!=r['success'] or r['success']<0 for r in flags):
        raise ValueError('counter submission mismatch')
    sent=sum(r['success'] for r in flags)
    if sent<=0 or value['max_send_bytes']>4096 or value['process_jfc_poll_calls']<=0:
        raise ValueError('counter interposition not exercised or SEND exceeded4K')
    if value['reliable_data']+value['reliable_ack']+value['unclassified_send']!=sent:
        raise ValueError('SEND classifications do not sum')
    if profile=='reliable_v3' and not(value['reliable_data']>0 and value['reliable_ack']>0):
        raise ValueError('reliable wire headers not observed')
    if profile=='iface_v2' and (value['reliable_data'] or value['reliable_ack']):
        raise ValueError('old mode unexpectedly produced reliable headers')
    return value


def sdk_check():
    code='''import hashlib,json
from pathlib import Path
root=Path("/usr/include/ub/umdk/urma")
files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.h")) if p.is_file()}
if "urma_api.h" not in files: raise ValueError("URMA SDK headers missing")
print(json.dumps(files,sort_keys=True))
'''
    local=json.loads(subprocess.check_output([sys.executable,'-c',code],text=True,timeout=10))
    remote=json.loads(osu.base.ssh('python3 -c "$1"\n',code,timeout=15))
    if local!=remote:
        raise ValueError('URMA SDK header sets differ between nodes; observer ABI not accepted')
    return {'header_directory':'/usr/include/ub/umdk/urma','both_match':True,'sha256':local,
            'note':'observer compiles against current SDK; hardware/provider compatibility still checked by node gates'}


def collect(run,row,profile,phase,sampling):
    pids=row['rank_evidence']['pids']
    ppn=row['ppn']; nonce=row['configuration']['nonce']
    def pid(rank):
        return pids[rank] if rank in pids else pids[str(rank)]
    names=['profile_rank%d_pid%d%s.json'%(rank,pid(rank),suffix)
           for rank in range(ppn,2*ppn) for suffix in ('_ready','_end','')]
    if phase=='sample' and sampling:
        # Agent always saves its JSON. Other perf outputs exist only after attach.
        names.append('perf_rank%d.json'%ppn)
    row['peer_diagnostic_manifest']=fetch_extra(run,names)
    summaries=[]
    for rank in range(2*ppn):
        prefix='profile_rank%d_pid%d'%(rank,pid(rank))
        value=json.loads((run/(prefix+'.json')).read_text())
        ready=json.loads((run/(prefix+'_ready.json')).read_text())
        end=json.loads((run/(prefix+'_end.json')).read_text())
        for item in (ready,end,value):
            if (item['rank'],item['pid'],item['nonce'])!=(rank,pid(rank),nonce):
                raise ValueError('phase marker identity differs')
        if ready['begin_ns']!=end['begin_ns'] or end['at_ns']<=ready['at_ns'] or not value['finalized']:
            raise ValueError('phase boundaries/finalization invalid')
        if phase=='counter':
            inspect_counter(value,rank,pid(rank),nonce,profile)
        summaries.append(value)
    row['phase_evidence']=summaries
    if phase=='sample' and sampling:
        agents=[]
        for rank in (0,ppn):
            agent=json.loads((run/('perf_rank%d.json'%rank)).read_text())
            if agent['rank']!=rank or agent['nonce']!=nonce or agent.get('pid')!=pid(rank):
                raise ValueError('sampler/application identity differs')
            if rank==ppn:
                row['peer_perf_manifest']=fetch_extra(run,['perf_rank%d.%s'%(rank,suffix) for suffix in
                                 ('data','log','script','script.stderr')],optional=True)
            agents.append(agent)
        row['samples']=agents


def trial(case,out,commands,evidence,item,phase,index,enabled):
    ppn=item['ppn']; profile=item['profile']; benchmark=item['benchmark']
    run=out/('%s_rm_p%d_%s_%d'%(benchmark,ppn,phase,index))
    saved_environment=osu.environment
    def environment(saved,mode,install,budget):
        if mode!='iface' or install!=case/'install' or budget!=osu.budget(ppn):
            raise ValueError('unbound diagnostic environment')
        env=previous.profile_environment(case,ppn,saved,profile,'eager',run)
        if phase!='validation':
            library=out/('libsend_profile_counts.so' if phase=='counter' else 'libsend_profile_phase.so')
            env['LD_PRELOAD']=str(library)+':'+env['LD_PRELOAD']
            env.update(HUCX_SEND_PROFILE_KIND='sample' if phase=='sample' and enabled else 'counter',
                       HUCX_SEND_PROFILE_MODULE=str(case/'install/lib/ucx/libuct_ub.so.0.0.0'))
        return env
    osu.environment=environment
    iterations=20 if phase=='validation' else (20000 if phase=='counter' else 2000000)
    try:
        row=osu.one_trial(out,commands,evidence,benchmark,'rm',ppn,phase,index,
                          argparse.Namespace(iterations=iterations,warmup=100,timeout=180),message_bytes=8)
        row.update(profile=profile,diagnostic_only=True,run=str(run))
        if row['passed'] and phase!='validation':
            try:
                collect(run,row,profile,phase,enabled)
            except Exception as exc:
                row.update(passed=False,error=type(exc).__name__+': '+str(exc))
        return row
    finally:
        osu.environment=saved_environment
        if 'row' in locals():
            osu.base.write_json(run/'trial.json',row)


def execute(case,out,build,requested):
    report={'plan':plan(),'source_commit':integration.matrix.COMMIT,'module_sha256':integration.matrix.MODULE,
            'status':'PREFLIGHT','trials':[],'product_changed':False,'formal_timing':False}
    def save():
        osu.base.write_json(out/'results.json',report)
        lines=['# 可靠SEND定向诊断','',
               '本轮所有OSU耗时仅作诊断；不能替代既有五轮正式OSU表。',
               '计数区间为Init完成至Finalize调用前，包含预热和基准控制操作。',
               'JFC计数为进程所有JFC，包含收发与辅助路径，不等于RM发送CQE数。',
               '采样仅选每节点一个真实应用PID，排除Init/Finalize，Init后延迟250ms开始；不是精确OSU计时区间。','',
               'STATUS='+report['status']]
        for row in report['trials']:
            lines.extend(['','## '+row['name']+' / '+row.get('profile',''),
                          'PASSED='+str(row['passed'])])
            if row.get('phase')=='counter' and 'phase_evidence' in row:
                for value in row['phase_evidence']:
                    lines.append('rank%d DATA=%d ACK=%d 重复成功DATA_PSN=%d SHORT=%d BCOPY=%d 发送标志=%s'%
                        (value['rank'],value['reliable_data'],value['reliable_ack'],value['repeated_successful_data_psn'],
                         value['am_short_calls'],value['am_bcopy_calls'],json.dumps(value['send_flags'],sort_keys=True)))
            for sample in row.get('samples',[]):
                lines.append('rank%d SAMPLING=%s'%(sample['rank'],sample['status']))
                if 'analysis' in sample:
                    lines.append('SAMPLES='+str(sample['analysis']['samples']))
                    lines.append('FLAT_TOP='+json.dumps(sample['analysis']['flat_top'][:10],ensure_ascii=False))
                    lines.append('INCLUSIVE_OVERLAPPING='+json.dumps(sample['analysis']['inclusive_category_samples']))
        (out/'diagnostic_summary.md').write_text('\n'.join(lines)+'\n')
    save()
    try:
        with previous.registered_runner(case,build):
            report['checkpoint']=checkpoint()
            report['prior40']=previous.prior_checkpoint(case)
            receipt=ROOT.parents[2]/'tool_receipt.json'
            if not receipt.is_file():
                raise ValueError('run the checked node ZIP; tool receipt missing')
            report['tool_receipt']={'path':str(receipt),'sha256':integration.digest(receipt),'identity':json.loads(receipt.read_text())}
            osu.validate_package()
            osu.BENCHMARKS={name:previous.policy.BENCHMARKS[name] for name in ('osu_multi_lat','osu_alltoall')}
            commands={mode:osu.saved_spec(osu.LAB/'osu_minimal8_ff_14ns8'/(mode+'_run.json'),case/'install') for mode in ('rm','rc')}
            evidence=osu.preflight(out,commands)
            report['preflight']=evidence
            report['sdk_headers']=sdk_check()
            osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n',out.parent,out)
            paths=[]
            for source,name in ((osu.ROOT/'osu_rank.py','osu_original_rank.py'),
                                (osu.A89/'rank_exec.py','a89_rank_exec.py'),
                                (ROOT/'profile_rank.py','osu_rank.py'),(ROOT/'perf_agent.py','perf_agent.py')):
                target=out/name; shutil.copy2(source,target); paths.append(target)
            env=integration.runtime_environment(commands['rm'][1])
            for kind,extra in (('counts',[str(ROOT/'counters.c'),'-I/usr/include/ub/umdk/urma','-pthread']),
                               ('phase',['-DPROFILE_PHASE_ONLY'])):
                target=out/('libsend_profile_'+kind+'.so')
                command=['gcc','-std=gnu11','-O2','-g','-Wall','-Wextra','-Werror','-shared','-fPIC',
                         str(ROOT/'phase.c'),*extra,'-ldl','-o',str(target)]
                osu.base.run_logged(command,env,out/('compile_'+kind+'.log'),60)
                paths.append(target)
            paths.append(out/'libosu_identity.so')
            integration.copy_checked(paths,out)
            report['helper_sha256']={p.name:integration.digest(p) for p in paths}
            perf_env={k:v for k,v in os.environ.items() if not k.startswith(('UCX_','MPI_UCX_')) and k not in ('LD_PRELOAD','LD_DEBUG')}
            local=json.loads(subprocess.check_output([sys.executable,str(out/'perf_agent.py'),str(out/'perf_preflight_node89'),'--preflight'],env=perf_env,text=True,timeout=15))
            remote=json.loads(osu.base.ssh('env -u LD_PRELOAD -u LD_DEBUG python3 "$1" "$2" --preflight\n',out/'perf_agent.py',out/'perf_preflight_node97',timeout=20))
            report['perf_preflight']={'node89':local,'node97':remote}
            enabled=local['available'] and remote['available']
            report.update(status='PREFLIGHT_PASS',sampling_enabled=enabled)
            save()
            print('预检=PASS 固定97b产品/原版OSU/工具隔离；PERF_BOTH_AVAILABLE='+str(enabled),flush=True)
            if not requested:
                return report
            items=[dict(benchmark=name,ppn=ppn,profile=profile) for name,ppn in
                   (('osu_multi_lat',1),('osu_alltoall',4)) for profile in ('iface_v2','reliable_v3')]
            for phase in ('validation','counter','sample'):
                if phase=='sample' and not enabled:
                    print('采样轮=SKIPPED；节点perf不可用，保留计数，不宣称热点已定位',flush=True)
                    continue
                for item in items:
                    row=trial(case,out,commands,evidence,item,phase,len(report['trials']),enabled)
                    report['trials'].append(row); save()
                    if not row['passed']:
                        raise RuntimeError(row['name']+': '+row.get('error',row.get('cleanup_error','incomplete')))
                    print('%s %s PPN=%d MODE=%s DATA/MAPS/EXIT=PASS DIAGNOSTIC_ONLY=YES'%
                          (phase.upper(),item['benchmark'],item['ppn'],item['profile']),flush=True)
                    if phase=='counter':
                        totals={key:sum(r[key] for r in row['phase_evidence']) for key in
                                ('reliable_data','reliable_ack','repeated_successful_data_psn','am_short_calls','am_bcopy_calls','process_jfc_poll_calls','process_cqes')}
                        print('COUNTERS='+json.dumps(totals,sort_keys=True),flush=True)
                        flags=[dict(rank=r['rank'],flags=r['send_flags']) for r in row['phase_evidence']]
                        print('COUNTER_FLAGS='+json.dumps(flags,sort_keys=True),flush=True)
                    if phase=='sample':
                        for sample in row['samples']:
                            detail=sample.get('analysis',{})
                            print('SAMPLING rank%d STATUS=%s N=%s TOP3=%s'%(sample['rank'],sample['status'],
                                  detail.get('samples',0),json.dumps(detail.get('flat_top',[])[:3])),flush=True)
            samples=[r for t in report['trials'] for r in t.get('samples',[])]
            adequate=enabled and len(samples)==8 and all(r['status']=='SAMPLES_CAPTURED_REVIEW_REQUIRED' for r in samples)
            report['status']='COUNTERS_AND_SAMPLES_CAPTURED_REVIEW_REQUIRED' if adequate else 'COUNTERS_PASS_SAMPLING_INCOMPLETE'
            save()
            print('诊断采集完成；尚无根因/优化/正式性能验收结论',flush=True)
            return report
    except BaseException as exc:
        report.update(status='STOPPED_INCOMPLETE',error=type(exc).__name__+': '+str(exc)); save()
        raise
    finally:
        print('RESULTS_JSON='+str(out/'results.json'),flush=True)
        print('DIAGNOSTIC_SUMMARY='+str(out/'diagnostic_summary.md'),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('case',type=Path)
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--run',action='store_true')
    action.add_argument('--preflight-only',action='store_true')
    args=parser.parse_args()
    if not (args.run or args.preflight_only):
        print(json.dumps(plan(),ensure_ascii=False,indent=2)); return
    if os.geteuid()!=0 or socket.gethostname()!='node89':
        raise ValueError('run only as root on node89')
    build=integration.validate_case(args.case)
    out=Path(tempfile.mkdtemp(prefix='send_profile_',dir=args.case))
    print('SEND_PROFILE_DIR='+str(out),flush=True)
    try:
        execute(args.case,out,build,args.run)
    except Exception as exc:
        print('SEND_PROFILE_STOPPED='+type(exc).__name__+': '+str(exc),file=sys.stderr,flush=True)
        raise SystemExit(1)


if __name__=='__main__':
    main()
