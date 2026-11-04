#!/usr/bin/env python3
"""Replay metadata reuse: unchanged wire/ACK/CQE policy, original OSU and faults."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'send_validation'))
import run_validation as previous
integration=previous.integration
osu=previous.osu
EXPERIMENT=integration.DEFAULT_CASE.parent
OLD_CASE=EXPERIMENT / 'r1_shared_a2b4b07c1_Z1tIOO'
LABELS=('old_reliable','new_reliable','rc')
SUCCESS_STATUS='SELECTED_REPLAY_POOL_FUNCTIONAL_AND_REQUESTED_OSU_PASS_REVIEW_REQUIRED'
RUN_PREFIX='send_pool_accept_'


def sdk_headers():
    code='''import hashlib,json
from pathlib import Path
root=Path("/usr/include/ub/umdk/urma")
values={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.h")) if p.is_file()}
if "urma_api.h" not in values: raise ValueError("URMA SDK missing")
print(json.dumps(values,sort_keys=True))
'''
    local=json.loads(subprocess.check_output([sys.executable,'-c',code],text=True,timeout=10))
    peer=json.loads(osu.base.ssh('python3 -c "$1"\n',code,timeout=15))
    if local!=peer:raise ValueError('dual-node URMA SDK headers differ')
    return local


BASE_COMMIT='a2b4b07c12be5401afaf427877aadd6752871810'
BASE_MODULE='2543923f6b2cae2a37365d293e715bc63fefa539ecc8b146880b6270b0351aa0'


def load_prior():
    values={}
    for name,relative,sha in (
        ('inline','send_inline_accept_9gpdcz12/results.json','0c9d72fc108e25e9afc306256dc402d58dce9e70b41d4e27dab2eede25986d80'),
        ('cost','send_cost_accept_xkpfvmmc/results.json','8f883b45cf2c3a41f4a0a9809c90c2dd71ad6cdaa107ac5de51a70e1b5e35682')):
        path=OLD_CASE/relative
        if integration.digest(path)!=sha: raise ValueError('accepted checkpoint changed: '+name)
        values[name]={'path':str(path),'sha256':sha,'rerun':False}
    return values


def validate_old():
    value=integration.policy.validate_build(OLD_CASE,root=EXPERIMENT)
    if value['source_commit']!=BASE_COMMIT or value['module_sha256']!=BASE_MODULE:
        raise ValueError('use accepted inline baseline, never the diagnostic installation')
    return value


def validate_candidate(case):
    if (ROOT.name not in ('send_pool', 'send_cleanup') or
            ROOT != case/'source/contrib/rm_ctp'/ROOT.name):
        raise ValueError('execute this isolated candidate source, not an installed toolkit')
    build=integration.policy.validate_build(case,root=EXPERIMENT)
    identity=json.loads((ROOT/'product_identity.json').read_text())
    if len(identity['files'])!=15:
        raise ValueError('candidate product source coverage differs')
    for name,sha in identity['files'].items():
        if integration.digest(case/'source'/name)!=sha:
            raise ValueError('candidate source mismatch: '+name)
    return build


@contextmanager
def bound(case,build):
    old_case=previous.CASE
    old_capture=integration.capture_configuration
    previous.CASE=case
    integration.capture_configuration=previous.config_capture
    try:
        with previous.registered_runner(case,build):
            yield
    finally:
        previous.CASE=old_case
        integration.capture_configuration=old_capture


def inspect_inline(summary,rank,pid,nonce,expected_data_bytes=None,baseline=False):
    observed=integration.wr_probe.inspect_summary(summary,rank,pid,nonce)
    data=[]
    for item in summary['buckets']:
        if item['complete_enable']!=1 or item['inline_limit']<0:
            raise ValueError('unexpected completion or inline capability')
        if item['reliable_kind'] not in (1,2):
            continue
        expected=0 if baseline else int(item['bytes']<=item['inline_limit'])
        if item['inline']!=expected:
            raise ValueError('actual reliable WR did not follow full-length inline decision')
        if item['reliable_kind']==1:
            data.append(item)
    if not data:
        raise ValueError('actual reliable DATA header not observed')
    if expected_data_bytes is not None and not any(item['bytes']==expected_data_bytes for item in data):
        raise ValueError('original OSU did not exercise requested actual WR boundary: '+str(expected_data_bytes))
    observed.update(rank=rank,pid=pid,data_buckets=data,
                    ack_buckets=[b for b in summary['buckets'] if b['reliable_kind']==2])
    return observed


def add_inline_evidence(row,out,expected_bytes=None,baseline=False):
    run=out/row['name'];values=[]
    for rank,pid in row['rank_evidence']['pids'].items():
        value=json.loads((run/('wr_rank%d_pid%d.json'%(rank,pid))).read_text())
        values.append(inspect_inline(value,rank,pid,row['configuration']['nonce'],expected_bytes,baseline))
    row['inline_evidence']=values


def functional_cases():
    return [dict(benchmark='osu_multi_lat',ppn=1,size=size,path='eager',expected_wr=wr)
            for size,wr in ((8,123),(12,127),(13,128),(14,129),(3982,None),(65536,None),(1048576,None))]+[
        dict(benchmark='osu_multi_lat',ppn=4,size=8,path='eager',expected_wr=123),
        dict(benchmark='osu_alltoall',ppn=4,size=8,path='eager',expected_wr=None),
        dict(benchmark='osu_alltoall',ppn=4,size=65536,path='eager',expected_wr=None),
        dict(benchmark='osu_multi_lat',ppn=1,size=65536,path='am_rndv',expected_wr=None)]


def plan():
    return {'product_change':'reuse existing pending_mp for DATA/ACK replay metadata; no new pool or wire change',
            'prior_product':BASE_COMMIT,'historical_normal_tasks':28,
            'original_osu_functional_tasks':len(functional_cases()),'strict_alternating_tasks':2,
            'original_osu_software_injection_tasks':1,'bounded_uct_exit_tasks':6,
            'timing_groups':2,'timing_profiles':list(LABELS),'timing_repeats':5,
            'timing_control_measure_resource_tasks':42,'max_ppn':4,'max_message':1048576,
            'default_action':'PLAN_ONLY','rto_ack_format_lifetime_changed':False,
            'hardware_fault_acceptance':False,'node_acceptance':'PENDING'}


def one(case,out,build,commands,evidence,helpers,item,phase,index,label,repeat=0,inject=False):
    row=None
    try:
        with bound(case,build):
            profile='rc' if label=='rc' else 'reliable_v3'
            actual=dict(item,profile=profile)
            row=previous.run_trial(case,out,commands,evidence,helpers,actual,phase,index,repeat,inject)
            row.update(comparison_label=label,install=str(case/'install'),source_commit=build['source_commit'],
                       module_sha256=build['module_sha256'])
            if row['passed'] and phase=='validation' and label!='rc':
                add_inline_evidence(row,out,item.get('expected_wr'),False)
                if item['path']=='am_rndv':
                    am_case={'benchmark':item['benchmark'],'mode':'rm','ppn':item['ppn'],
                             'sizes':(item['size'],),'threshold':16384,'path':'am_rndv'}
                    integration.collect_diagnostics(out,row,evidence,am_case=am_case)
        return row
    except Exception as exc:
        if row is None:
            raise
        row.update(passed=False,error=type(exc).__name__+': '+str(exc))
        return row
    finally:
        if row is not None:
            osu.base.write_json(out/row['name']/'trial.json',row)


def compare(rows,item):
    group=dict(item,profiles={})
    for label in LABELS:
        selected=[r for r in rows if r.get('comparison_label')==label and
                  all(r[k]==item[k] for k in ('benchmark','ppn','size'))]
        timings=[r['output']['value'] for r in selected if r['phase']=='timing' and r['passed']]
        resources=[r for r in selected if r['phase']=='resources' and r['passed']]
        controls=[r for r in selected if r['phase']=='validation' and r['passed']]
        if len(timings)!=5 or len(resources)!=1 or len(controls)!=1:
            raise ValueError('comparison group incomplete; no filtering or historical mixing')
        rss=[r['rank_evidence']['before_finalize_rss_sum_mib'] for r in selected if r['phase']=='timing']
        group['profiles'][label]={'samples_us':timings,'median_us':statistics.median(timings),
            'min_us':min(timings),'max_us':max(timings),'rss_samples_mib':rss,
            'rss_median_mib':statistics.median(rss),'jettys':resources[0]['jettys']}
    old=group['profiles']['old_reliable']['median_us'];new=group['profiles']['new_reliable']['median_us']
    group['new_vs_old_latency_reduction_pct']=(old-new)/old*100
    return group


def write_table(out,report):
    lines=['# 可靠SEND元数据复用候选对照','',
           '旧可靠为已开inline的a2b安装；新可靠复用pending_mp；RC使用新候选安装。',
           ('同一窗口原版OSU7.5.2，轮换模式顺序，各5轮，不删异常、不混入旧样本。'
            if report['timing_requested'] else '本轮仅功能回归，不追加性能测量。'),
           '计时无WR/故障/资源观察器和调试日志；均保留相同Init/Finalize身份探针。',
           'RSS为各rank Finalize前采样求和，两节点合计、未同步；不是设备内存。',
           'RM4KiB、RC8KiB片长沿用原方案；本轮未改ACK策略/格式、正常RTO或回收条件。','',
           '| OSU | PPN | 模式 | 时延中位数[范围] μs | RSS中位数 MiB |',
           '|---|---:|---|---:|---:|']
    for group in report['comparisons']:
        for label,p in group['profiles'].items():
            lines.append('| %s | %d | %s | %.3f[%.3f,%.3f] | %.2f |'%(group['benchmark'],group['ppn'],
                label,p['median_us'],p['min_us'],p['max_us'],p['rss_median_mib']))
    lines+=['','STATUS='+report['status']]
    (out/'comparison.md').write_text('\n'.join(lines)+'\n')


def execute(case,build,out,timing):
    report={'plan':plan(),'source_commit':build['source_commit'],'module_sha256':build['module_sha256'],
            'status':'RUNNING','trials':[],'failures':[],'comparisons':[],'timing_requested':timing}
    def save():
        osu.base.write_json(out/'results.json',report);write_table(out,report)
    def accept(row,category='trials'):
        report[category].append(row);save()
        if not row['passed']:
            raise RuntimeError(row['name']+': '+row.get('error',row.get('cleanup_error','incomplete')))
    save()
    try:
        report['prior_checkpoints']=load_prior()
        report['sdk_headers_both_match']=sdk_headers()
        old_build=validate_old()
        # Reuse the proven deploy/normal regression implementation.
        spec=importlib.util.spec_from_file_location('inline_send_base',ROOT.parent/'send_reliable/run_candidate.py')
        send=importlib.util.module_from_spec(spec);spec.loader.exec_module(send)
        send.base.B_ROOT=EXPERIMENT
        report['deployment']=send.base.deploy(case,build,out)
        osu.validate_package()
        sets={}
        for label,install_case,install_build in (('old_reliable',OLD_CASE,old_build),('new_reliable',case,build)):
            directory=out/('preflight_'+label);directory.mkdir()
            with bound(install_case,install_build):
                commands={mode:osu.saved_spec(osu.LAB/'osu_minimal8_ff_14ns8'/(mode+'_run.json'),install_case/'install') for mode in ('rm','rc')}
                evidence=osu.preflight(directory,commands)
            sets[label]=(install_case,install_build,commands,evidence)
        sets['rc']=sets['new_reliable']
        report['preflight']={label:data[3] for label,data in sets.items()};save()
        osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n',out.parent,out)
        commands=sets['new_reliable'][2]
        with bound(case,build):
            helpers=integration.compile_helpers(case,out,commands)
            observer=out/'libsend_inline_probe.so'
            osu.base.run_logged(['gcc','-std=gnu11','-O2','-g','-Wall','-Wextra','-Werror','-shared','-fPIC',
                                '-I/usr/include/ub/umdk/urma',str(ROOT.parent/'send_inline/send_probe.c'),'-o',str(observer),'-ldl','-pthread'],
                                integration.runtime_environment(commands['rm'][1]),out/'compile_inline_probe.log',90)
            integration.copy_checked([observer],out);helpers['wr_probe']=observer
            binary,injector=previous.failure.compile_probe(integration,case,out,commands)
            shutil.copy2(out/'preflight_new_reliable/libosu_identity.so',out/'libosu_identity.so')
            shutil.copy2(osu.ROOT/'osu_rank.py',out/'osu_rank.py')
            shutil.copy2(osu.A89/'rank_exec.py',out/'a89_rank_exec.py')
            integration.copy_checked([out/'libosu_identity.so',out/'osu_rank.py',out/'a89_rank_exec.py'],out)
            report['helpers']={name:integration.digest(path) for name,path in helpers.items()}
            report['normal_28']=send.base.normal_regression(case,out);save()
            print('既有legacy/iface_v2/RC正常矩阵=28/28 PASS',flush=True)
        for item in functional_cases():
            row=one(case,out,build,sets['new_reliable'][2],sets['new_reliable'][3],helpers,
                    item,'validation',len(report['trials']),'new_reliable')
            accept(row)
            print('POOL_OSU=%s PPN=%d SIZE=%d DATA/MAPS/WR/INLINE/EXIT=PASS'%(item['benchmark'],item['ppn'],item['size']),flush=True)
            if item.get('expected_wr') is not None:
                selected=[b for value in row['inline_evidence'] for b in value['data_buckets'] if b['bytes']==item['expected_wr']]
                print('ACTUAL_DATA_WR=%d INLINE_LIMITS=%s INLINE_FLAGS=%s'%(item['expected_wr'],
                      sorted({b['inline_limit'] for b in selected}),sorted({b['inline'] for b in selected})),flush=True)
        with bound(case,build):
            for ppn in (1,4):
                row=integration.run_custom(case,out,commands,sets['new_reliable'][3],helpers,ppn,False)
                if row['passed']:
                    try:add_inline_evidence(row,out)
                    except Exception as exc:row.update(passed=False,error=str(exc))
                accept(row)
            item=dict(benchmark='osu_multi_lat',ppn=1,size=65536,path='eager',expected_wr=None)
            row=one(case,out,build,commands,sets['new_reliable'][3],helpers,item,'validation',len(report['trials']),'new_reliable',inject=True)
            accept(row)
            print('原版OSU软件丢DATA/ACK/重复=PASS RETRY_POSTS='+str(row['production_retransmit_posts']),flush=True)
            for item in previous.policy.failure_cases():
                accept(previous.failure.run(integration,case,out,commands,binary,injector,item),'failures')
            print('大小交替2项/原版OSU注入1项/有界退出6项=PASS',flush=True)
        if timing:
            for benchmark,ppn in (('osu_multi_lat',1),('osu_alltoall',4)):
                item=dict(benchmark=benchmark,ppn=ppn,size=8,path='eager')
                print('正式对照组开始 OSU=%s PPN=%d 三模式各1校验/5计时/1资源'%(benchmark,ppn),flush=True)
                first=len(report['trials'])
                schedule=[('validation',0,label) for label in LABELS]
                for repeat in range(5):
                    order=LABELS[repeat%3:]+LABELS[:repeat%3]
                    schedule.extend(('timing',repeat,label) for label in order)
                schedule.extend(('resources',0,label) for label in LABELS)
                for phase,repeat,label in schedule:
                    selected,selected_build,cmds,evidence=sets[label]
                    accept(one(selected,out,selected_build,cmds,evidence,helpers,item,phase,len(report['trials']),label,repeat))
                    print('对照 PHASE=%s REPEAT=%d MODE=%s EXIT=PASS'%(phase,repeat,label),flush=True)
                group=compare(report['trials'][first:],item);report['comparisons'].append(group);save()
                print('正式OSU=%s PPN=%d 8B_MEDIAN_US=%s 新版对旧可靠时延降低=%+.2f%%'%(benchmark,ppn,
                      json.dumps({k:v['median_us'] for k,v in group['profiles'].items()}),group['new_vs_old_latency_reduction_pct']),flush=True)
        report['status']=SUCCESS_STATUS;save()
        print('所选SEND功能通过；'+('性能样本保留待审阅；' if timing else '本轮未追加性能测量；')+'完整C/D/硬件故障未验收',flush=True)
    except BaseException as exc:
        report.update(status='STOPPED_INCOMPLETE',error=type(exc).__name__+': '+str(exc));save();raise
    finally:
        print('RESULTS_JSON='+str(out/'results.json'),flush=True)
        print('TABLE_MD='+str(out/'comparison.md'),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('case',type=Path)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--timing',action='store_true')
    args=parser.parse_args()
    if args.timing and not args.run:parser.error('--timing requires --run')
    if not args.run:print(json.dumps(plan(),ensure_ascii=False,indent=2));return
    case=args.case.resolve()
    if os.geteuid()!=0 or socket.gethostname()!='node89':raise ValueError('run only as root on node89')
    build=validate_candidate(case)
    out=Path(tempfile.mkdtemp(prefix=RUN_PREFIX,dir=case))
    print('SEND_POOL_DIR='+str(out),flush=True)
    try:execute(case,build,out,args.timing)
    except Exception as exc:
        print('SEND_POOL_STOPPED='+type(exc).__name__+': '+str(exc),file=sys.stderr,flush=True)
        raise SystemExit(1)


if __name__=='__main__':main()
