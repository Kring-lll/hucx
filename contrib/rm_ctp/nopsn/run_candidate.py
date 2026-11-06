#!/usr/bin/env python3
"""Isolated no-DATA-PSN experiment. No hardware claim until node acceptance."""
import argparse
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import sys
from types import SimpleNamespace
import tempfile

ROOT=Path(__file__).resolve().parent
WIRE_PROBE_SOURCE=ROOT/'wire_probe.c'
PROJECT=ROOT.parent
LAB=Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-nopsn-dev')
FULL_CASE=Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-um-send-dev/r1_shared_a4d07599b_BaJAce')
FULL_COMMIT='a4d07599bd5b66b84c5de8257e5f7fe15e3a6f63'
FULL_MODULE='48dcd30a30f29a39d8f9b63e0c2afd7279bdd3da23b1757a610c828b77b04c2f'
FULL_RESULT='um_osu_rx4096_ssdyq3ne'
VARIANTS=('full','nopsn')
LEVELS=(1,8,16,20,24,32,48,64,96,128,160)
GROUPS=(('osu_latency',1,8),('osu_bw',1,65536),('osu_bibw',1,65536))+tuple(('osu_alltoall',p,8) for p in LEVELS)
SIZES=(8,64,104,105,4079,4080,4081,8135,8136,8137,16384,65536,1048576)


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def dump(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def schedule(timing):
    # Separate observed trials establish actual wire layout, never timed.
    for variant in VARIANTS:
        yield (-1,'osu_latency',1,8,'validation',0,variant,True)
    for size in SIZES:
        yield (-2,'osu_latency' if size==8 else 'osu_multi_lat',1,size,'validation',0,'nopsn',False)
    for ppn in (1,8):
        yield (-3,'nopsn_alternating',ppn,8,'validation',0,'nopsn',False)
    for size in (8,65536):
        yield (-4,'osu_alltoall',8,size,'validation',0,'nopsn',False)
    for group,(benchmark,ppn,size) in enumerate(GROUPS):
        for variant in VARIANTS:yield (group,benchmark,ppn,size,'validation',0,variant,False)
        if timing:
            for repeat in range(5):
                order=VARIANTS if repeat%2==0 else VARIANTS[::-1]
                for variant in order:yield(group,benchmark,ppn,size,'timing',repeat,variant,False)
            for variant in VARIANTS:yield(group,benchmark,ppn,size,'resources',0,variant,False)


def plan(timing):
    rows=list(schedule(timing))
    return {'scope':'NORMAL_PATH_DATA_PSN_ABLATION_NOT_RELIABILITY_ACCEPTANCE',
        'total_mpi':len(rows),'functional_and_validation':sum(r[4]=='validation' for r in rows),
        'timing':sum(r[4]=='timing' for r in rows),'resources':sum(r[4]=='resources' for r in rows),
        'groups':GROUPS,'variants':VARIANTS,'data_header_bytes':{'full':8,'nopsn':6},
        'control_header_bytes':8,'ack_psn_retained':True,'internal_window_sequence_retained':True,
        'am_bcopy_cap_unchanged':4088,'tx_depth':64,'rx_depth':4096,
        'max_ppn':160,'retry_policy':'DATA retransmission invalidates and stops candidate sample; connection controls retain UM retries',
        'no_psn_simplified_reliability_claim':True,'old_samples_merged':False}


def workload_arguments(shared_arguments,benchmark,iterations,warmup,validation,message_bytes=8):
    """Only the added functional Multi Latency sizes extend the old CLI policy."""
    if benchmark=='nopsn_alternating':return []
    if benchmark!='osu_multi_lat':
        return shared_arguments(benchmark,iterations,warmup,validation,message_bytes)
    if not validation or message_bytes not in SIZES:
        raise ValueError('unplanned functional Multi Latency workload')
    return ['--message-size','%d:%d'%(message_bytes,message_bytes),
        '--iterations',str(iterations),'--warmup',str(warmup),'--validation']


def cli_preflight(shared_arguments,timing):
    """Run the exact installed argument selector for every task before any MPI."""
    tasks=list(schedule(timing))
    for _,benchmark,_,size,phase,_,_,_ in tasks:
        iterations=100 if phase=='validation' else 10000 if benchmark=='osu_latency' else 1000
        warmup=10 if phase=='validation' else 1000 if benchmark=='osu_latency' else 100
        workload_arguments(shared_arguments,benchmark,iterations,warmup,phase=='validation',size)
    return {'planned_tasks':len(tasks),'parameter_generation':'PASS','mpi_executed':False}


def validate_candidate(case,expected_commit):
    if (case.parent!=LAB or ROOT!=case/'source/contrib/rm_ctp/nopsn' or
            not re.fullmatch('[0-9a-f]{40}',expected_commit)):
        raise ValueError('run the isolated new source under the nopsn experiment root')
    build=json.loads((case/'build_manifest.json').read_text())
    marker=(case/'source/.hucx_source_commit').read_text().strip()
    if build['source_commit']!=expected_commit or marker!=expected_commit:
        raise ValueError('archive/build/source commit differs')
    if digest(build['archive'])!=build['archive_sha256']:
        raise ValueError('source archive changed since build')
    if digest(case/'install/lib/ucx/libuct_ub.so.0.0.0')!=build['module_sha256']:
        raise ValueError('candidate module changed')
    manifest=json.loads((case/'source/contrib/rm_ctp/r1/candidate_manifest.json').read_text())
    for name,value in manifest['files'].items():
        p=Path(name)
        if p.is_absolute() or '..' in p.parts or digest(case/'source'/p)!=value:
            raise ValueError('candidate source mismatch: '+name)
    return build


def load_engine():
    namespace={'__name__':'nopsn_bound_runner','__file__':str(PROJECT/'um_send/run_candidate.py')}
    exec(compile((PROJECT/'um_send/run_candidate.py').read_bytes(),namespace['__file__'],'exec'),namespace)
    namespace['LAB']=LAB
    sys.path.insert(0,str(PROJECT/'um_send_compare'))
    import policy,scale_policy,run_compare
    return namespace,policy,scale_policy,run_compare


def prior_checkpoint(scale):
    root=FULL_CASE/FULL_RESULT;path=root/'results.json';report=json.loads(path.read_text())
    rows=report['gates']+report['trials']
    if (report['source_commit']!=FULL_COMMIT or report['module_sha256']!=FULL_MODULE or
            report['status']!='SELECTED_P2P_AND_ALLTOALL_SCALE_COMPLETE_REVIEW_REQUIRED' or
            len(rows)!=255 or len(report['comparisons'])!=14 or
            report['plan']['requested_rx_depth']!=4096 or
            report['tool']['tool_commit']!='56c8913f5c69bf99f483f3a18c5152fa18dec029'):
        raise ValueError('reported complete RX4096 baseline identity/coverage differs')
    for row in rows:
        p=(root/row['name']/'trial.json').resolve()
        if p.parent.parent!=root.resolve() or json.loads(p.read_text())!=row or not row['passed']:
            raise ValueError('baseline trial changed or failed: '+row['name'])
        scale.policy.inspect_trial(row,row['profile'],row['phase'])
        prefix='UCX_'+row['profile'].upper()+'_URMA_'
        if (row.get('requested_rx_depth')!=4096 or
                row['configuration']['environment'].get(prefix+'RX_DEPTH')!='4096' or
                any(v.get('RX_DEPTH')!='4096' for v in row['effective_config'].values())):
            raise ValueError('baseline effective queue depth differs')
        if row['phase']=='resources' and any(v.get('jfr_rx_depths')!=[4096] for v in row['jettys'].values()):
            raise ValueError('baseline actual JFR depth differs')
    if [scale.summarize(report['trials'],i) for i in range(14)]!=report['comparisons']:
        raise ValueError('baseline statistics differ')
    if digest(FULL_CASE/'install/lib/ucx/libuct_ub.so.0.0.0')!=FULL_MODULE:
        raise ValueError('accepted full-RM module changed')
    return {'path':str(path),'sha256_captured_now':digest(path),'reviewed_mpi':255,'old_samples_merged':False}


def wire_evidence(base,run,row,header):
    pids={int(k):v for k,v in row['rank_evidence']['pids'].items()}
    names=['nopsn_wire_rank%d_pid%d.json'%(r,pids[r]) for r in range(row['ppn'],2*row['ppn'])]
    code='import json,pathlib,sys\np=pathlib.Path(sys.argv[1]);print(json.dumps({n:(p/n).read_text() for n in json.loads(sys.argv[2])}))\n'
    for name,text in json.loads(base.ssh('python3 -c "$1" "$2" "$3"\n',code,run,json.dumps(names))).items():
        if name not in names or len(text)>65536:raise ValueError('invalid wire receipt')
        (run/name).write_text(text)
    values=[]
    for rank,pid in sorted(pids.items()):
        v=json.loads((run/('nopsn_wire_rank%d_pid%d.json'%(rank,pid))).read_text())
        if (v['rank']!=rank or v['pid']!=pid or v['nonce']!=row['configuration']['nonce'] or
                v['schema']!=1 or v['errors'] or v['data_success']<=0 or
                v['short_success']<=0 or v['header_mask']!=(1<<header) or v['max_wr_bytes']>4096 or
                v['retry_success']!=0):
            raise ValueError('wire format/identity/retry proof differs')
        values.append(v)
    return values


def summarize(rows,group):
    result={'benchmark':GROUPS[group][0],'ppn':GROUPS[group][1],'message_bytes':GROUPS[group][2],'variants':{}}
    selected=[r for r in rows if r['group']==group]
    if len(selected)!=14 or any(not r['passed'] for r in selected):raise ValueError('incomplete comparison group')
    for variant in VARIANTS:
        items=[r for r in selected if r['variant']==variant]
        t=sorted((r for r in items if r['phase']=='timing'),key=lambda r:r['repeat'])
        resources=[r for r in items if r['phase']=='resources']
        if [r['repeat'] for r in t]!=list(range(5)) or len(resources)!=1 or sum(r['phase']=='validation' for r in items)!=1:
            raise ValueError('phase/repeat coverage differs')
        if any(r['configuration']['arguments']!=resources[0]['configuration']['arguments'] for r in t):
            raise ValueError('timing/resource workload differs')
        values=[r['output']['value'] for r in t]
        rss=[r['rank_evidence']['before_finalize_rss_sum_mib'] for r in t]
        peaks=[r['peak_per_process'] for r in resources[0]['jettys'].values()]
        result['variants'][variant]={'samples':values,'unit':t[0]['output']['unit'],
            'median':statistics.median(values),'minimum':min(values),'maximum':max(values),
            'rss_samples_mib':rss,'rss_median_mib':statistics.median(rss),
            'jetty_peak_range':[min(peaks),max(peaks)],'effective_config':resources[0]['effective_config']}
    a,b=(result['variants'][v] for v in VARIANTS)
    result['nopsn_improvement_pct']=100*((a['median']-b['median']) if a['unit']=='us' else (b['median']-a['median']))/a['median']
    return result


def tables(out,report):
    lines=['# 完整RM与无DATA PSN实验副本：同批正常路径对照','',
        'DATA头8B/6B，ACK与建链头8B；保留ACK及内部窗口计数。不是完全无序号协议或可靠性验收。',
        'RX4096/TX64、AM_BCOPY4088、同绑核/OSU参数；RSS为两节点各rank非同步合计。',
        '全部样本保留，负载快照不能单独定位他人干扰；高PPN已知异常不被隐去。','',
        '| OSU | PPN | Bytes | Unit | 完整RM | 副本 | 副本改善% |',
        '|---|---:|---:|---|---:|---:|---:|']
    records=[]
    for g in report['comparisons']:
        a,b=(g['variants'][v] for v in VARIANTS)
        lines.append('| %s | %d | %d | %s | %.3f | %.3f | %+.2f |'%(g['benchmark'],g['ppn'],g['message_bytes'],a['unit'],a['median'],b['median'],g['nopsn_improvement_pct']))
        for v,s in g['variants'].items():records.append({'benchmark':g['benchmark'],'ppn':g['ppn'],'bytes':g['message_bytes'],'variant':v,**s})
    lines+=['','状态：'+report['status'],'','完整样本：',json.dumps(report['comparisons'],ensure_ascii=False,indent=2)]
    (out/'comparison.md').write_text('\n'.join(lines)+'\n')
    with (out/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['benchmark','ppn','bytes','variant','median','minimum','maximum','unit','rss_median_mib','samples','rss_samples_mib','jetty_peak_range','effective_config']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in records:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in r.items()})


def main():
    p=argparse.ArgumentParser();p.add_argument('case',type=Path,nargs='?');p.add_argument('--expected-commit');p.add_argument('--run',action='store_true');p.add_argument('--timing',action='store_true')
    args=p.parse_args()
    if not args.run:print(json.dumps(plan(args.timing),ensure_ascii=False,indent=2));return
    if not args.case or not args.expected_commit:p.error('explicit case and expected commit required')
    if os.geteuid()!=0 or socket.gethostname()!='node89':raise ValueError('run as root on node89')
    case=args.case.resolve();build=validate_candidate(case,args.expected_commit)
    engine,policy,scale,runner=load_engine();osu=engine['osu'];base=osu.base
    cli_check=cli_preflight(scale.arguments,args.timing)
    print('ALL_TASK_ARGUMENTS=%d/%d PASS BEFORE_MPI=YES'%(cli_check['planned_tasks'],cli_check['planned_tasks']),flush=True)
    lease=(FULL_CASE.parent/'um_osu_scale.lock').open('a+')
    fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
    out=Path(tempfile.mkdtemp(prefix='nopsn_accept_',dir=case));print('NOPSN_ACCEPT_DIR='+str(out),flush=True)
    report={'status':'RUNNING','plan':plan(args.timing),'products':{'full':{'case':str(FULL_CASE),'source_commit':FULL_COMMIT,'module_sha256':FULL_MODULE},'nopsn':build},'trials':[],'comparisons':[],'cli_preflight':cli_check}
    def save():dump(out/'results.json',report);tables(out,report)
    active_variant='full';observed=False
    try:
        report['baseline_checkpoint']=prior_checkpoint(scale);save()
        engine['deploy'](case,build)
        engine['PROFILE']='rm';engine['OBSERVE']=engine['FAULT']=False
        osu.DEFAULT_CASE=case;osu.PRODUCT_COMMIT=build['source_commit'];osu.PRODUCT_MODULE=build['module_sha256']
        osu.BENCHMARKS=dict(scale.BENCHMARKS)
        osu.BENCHMARKS['osu_multi_lat']=policy.BENCHMARKS['osu_multi_lat']
        osu.arguments=scale.arguments
        osu.parse_output=scale.parse_output
        osu.budget=lambda ppn:{'ppn':ppn,'tx_depth':64,'rx_depth':4096,'window':32}
        def environment(saved,mode,install,cfg):
            env=engine['environment'](saved,mode,install,cfg)
            env.update(UCX_RM_URMA_RX_DEPTH='4096',UCX_RM_URMA_RX_MAX_POLL='16',UCX_RM_URMA_RX_MAX_BATCH='16')
            if observed:
                env['LD_PRELOAD']=str(out/'libnopsn_wire.so')+':'+env['LD_PRELOAD']
                env['HUCX_NOPSN_WIRE_DIR']=str(current_run[0])
            return env
        osu.environment=environment;base.parsed_config=engine['parsed_config']
        original_jettys=osu.inspect_jettys
        def jettys(run,ppn,mode,pids,depth):
            rows=original_jettys(run,ppn,mode,pids,depth)
            depths=scale.jfr_depths(run,ppn,pids,4096)
            for rank in rows:rows[rank]['jfr_rx_depths']=depths[rank]
            return rows
        osu.inspect_jettys=jettys
        commands={}
        for mode in ('rc','rm'):
            command,saved=osu.saved_spec(osu.LAB/'osu_minimal8_ff_14ns8'/(mode+'_run.json'),case/'install')
            commands[mode]=(command,engine['environment'](saved,mode,case/'install',{}))
        evidence=osu.preflight(out,commands);report['preflight']=evidence
        # Independent preflight confirms the pinned full module on both nodes.
        base.module_check(FULL_CASE/'install',FULL_MODULE)
        mpi=next(Path(x) for x in commands['rm'][0] if Path(x).name in ('mpirun','mpiexec'))
        compile_env={k:v for k,v in os.environ.items() if k not in ('LD_PRELOAD','LD_DEBUG')}
        base.run_logged(['gcc','-shared','-fPIC','-O2','-g','-Wall','-Wextra','-Werror','-I/usr/include/ub/umdk/urma',str(WIRE_PROBE_SOURCE),'-ldl','-lpthread','-o',str(out/'libnopsn_wire.so')],compile_env,out/'compile_wire.log',60)
        base.run_logged([str(mpi.with_name('mpicc')),'-std=c99','-O2','-g','-Wall','-Wextra','-Werror',str(ROOT/'alternating.c'),'-o',str(out/'nopsn_alternating')],compile_env,out/'compile_alternating.log',60)
        alt_mpi=osu.mpi_dependency(subprocess.check_output(['ldd',str(out/'nopsn_alternating')],env=compile_env,text=True))
        if str(alt_mpi)!=evidence['benchmarks']['osu_latency']['mpi_library']:
            raise ValueError('alternating binary linked to a different MPI')
        base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n',out)
        for source,name in ((osu.ROOT/'osu_rank.py','osu_rank.py'),(osu.A89/'rank_exec.py','a89_rank_exec.py'),(PROJECT/'um_send_compare/load_snapshot.py','load_snapshot.py')):shutil.copy2(source,out/name)
        wrapper=out/'osu_rank.py';text=wrapper.read_text();old='        shared.main()'
        if text.count(old)!=1:raise ValueError('rank wrapper entry differs')
        wrapper.write_text(text.replace(old,'        import resource\n        resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n'+old))
        helpers=[out/n for n in ('osu_rank.py','a89_rank_exec.py','load_snapshot.py','libosu_identity.so','libnopsn_wire.so','nopsn_alternating')]
        base.copy_remote(helpers,str(out)+'/')
        for helper in helpers:
            if base.ssh('sha256sum "$1"\n',helper).split()[0]!=digest(helper):raise ValueError('peer helper mismatch')
        # Custom correctness binary has its own name and parser, never OSU timing.
        original_arguments,original_parse=osu.arguments,osu.parse_output
        osu.BENCHMARKS['nopsn_alternating']=(str(out/'nopsn_alternating'),'NOPSN_ALTERNATING_PASS','custom correctness only')
        evidence['benchmarks']['nopsn_alternating']={'mpi_library':evidence['benchmarks']['osu_latency']['mpi_library']}
        def arguments(b,i,w,v,n=8):return workload_arguments(original_arguments,b,i,w,v,n)
        def parse(text,b,v,i,n=8):
            if b=='nopsn_alternating':
                found=re.findall(r'^NOPSN_ALTERNATING_PASS RANKS=(\d+) ROUNDS=260\s*$',text,re.M)
                if len(found)!=1 or int(found[0])!=2*active_ppn[0]:raise ValueError('alternating correctness marker differs')
                return {'value':1,'unit':'PASS','validation_pass':True,'validation_enabled':True,'raw_row':found[0]}
            return policy.parse_output(text,b,v,i,n) if b=='osu_multi_lat' else original_parse(text,b,v,i,n)
        osu.arguments,osu.parse_output=arguments,parse
        save();current_run=[None];active_ppn=[1]
        for index,item in enumerate(schedule(args.timing)):
            group,benchmark,ppn,size,phase,repeat,active_variant,observed=item
            selected=FULL_CASE if active_variant=='full' else case
            product=report['products'][active_variant]
            osu.DEFAULT_CASE=selected;osu.PRODUCT_COMMIT=product['source_commit'];osu.PRODUCT_MODULE=product['module_sha256']
            # one_trial's deterministic name is needed before environment creation.
            name='%s_rm_p%d_%s_%d'%(benchmark,ppn,phase,index)
            if size!=8:name+='_b%d'%size
            current_run[0]=out/name;active_ppn[0]=ppn
            report['active_task']={'index':index,'variant':active_variant,'benchmark':benchmark,'ppn':ppn,'phase':phase,'bytes':size,'wire_observed':observed};save()
            print('START '+json.dumps(report['active_task']),flush=True)
            inv=base.inventory()
            from run_scale_compare import disk_inventory
            disks=disk_inventory(base,case)
            if not scale.capacity(inv,ppn,disks)['passed']:raise ValueError('capacity floor failed')
            args_trial=SimpleNamespace(iterations=100 if phase=='validation' else 10000 if benchmark=='osu_latency' else 1000,warmup=10 if phase=='validation' else 1000 if benchmark=='osu_latency' else 100,timeout=120 if ppn==1 else 300)
            row=runner.run_trial(engine,out,commands,evidence,(group,benchmark,ppn,size,phase,repeat,'rm'),index,capture=True,trial_args=args_trial)
            row.update(variant=active_variant,wire_observed=observed,source_commit=product['source_commit'],module_sha256=product['module_sha256'])
            if row['passed']:
                try:
                    run=out/row['name']
                    for rank in range(2*ppn):
                        for point in ('after_init','before_finalize'):
                            mapped='libnopsn_wire.so' in (run/('rank%d_%s.maps'%(rank,point))).read_text()
                            if mapped!=observed:raise ValueError('wire observer phase mismatch')
                    if observed:row['wire_evidence']=wire_evidence(base,run,row,8 if active_variant=='full' else 6)
                except Exception as exc:row.update(passed=False,error=type(exc).__name__+': '+str(exc))
            dump(out/row['name']/'trial.json',row);report['trials'].append(row);save()
            print('TRIAL VARIANT=%s OSU=%s PPN=%d BYTES=%d PHASE=%s PASS=%s'%(active_variant,benchmark,ppn,size,phase,row['passed']),flush=True)
            if not row['passed']:raise RuntimeError(row.get('error','trial failed'))
            if args.timing and group>=0 and phase=='resources' and active_variant==VARIANTS[-1]:
                g=summarize(report['trials'],group);report['comparisons'].append(g);save();print('GROUP='+json.dumps(g,sort_keys=True),flush=True)
        report.pop('active_task',None)
        report['status']='SELECTED_NOPSN_NORMAL_PATH_COMPARISON_COMPLETE_REVIEW_REQUIRED' if args.timing else 'SELECTED_NOPSN_FUNCTIONAL_PASS_PERFORMANCE_PENDING'
        save()
    except Exception as exc:
        report.update(status='STOPPED_INCOMPLETE',error=type(exc).__name__+': '+str(exc));save();raise
    finally:
        lease.close()
        print('RESULTS_JSON='+str(out/'results.json'),flush=True)
        print('TABLE_MD='+str(out/'comparison.md'),flush=True)
        if digest(case/'install/lib/ucx/libuct_ub.so.0.0.0')!=build['module_sha256'] or digest(FULL_CASE/'install/lib/ucx/libuct_ub.so.0.0.0')!=FULL_MODULE:
            raise RuntimeError('product module changed during comparison')

if __name__=='__main__':main()
