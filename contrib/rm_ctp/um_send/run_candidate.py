#!/usr/bin/env python3
"""Isolated UM-derived RM/CTP SEND candidate; original OMB, explicit execution."""
import argparse
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
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'wr_probe'))
import run_probe as wr
osu = wr.osu
LAB = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-um-send-dev')
PROFILE = 'rm'
OBSERVE = False
FAULT = False
PROBE = None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clean_env(saved):
    return {k: v for k, v in saved.items()
            if not k.startswith(('UCX_', 'MPI_UCX_', 'HUCX_', 'OMPI_MCA_'))
            and k not in ('LD_PRELOAD', 'LD_DEBUG', 'A89_RUN_NONCE')}


def environment(saved, mode, install, unused):
    env = clean_env(saved)
    kind = PROFILE.upper()
    prefix = 'UCX_' + kind + '_URMA_'
    env.update({prefix + 'TX_DEPTH': '64', prefix + 'RX_DEPTH': '512'})
    if PROFILE in ('rm', 'um'):
        env.update({prefix + 'MAX_WINDOW': '32', prefix + 'TIMEOUT': '10s'})
    else:
        env.update({prefix + 'FC_ENABLE': 'y', prefix + 'FC_WND_SIZE': '8'})
    env.update(UCX_TLS=('rc_urma,um_urma:aux,self' if PROFILE == 'rc' else PROFILE + '_urma,self'), UCX_NET_DEVICES='udmac0d1e2:1',
               UCX_PROTO_ENABLE='n', UCX_RNDV_THRESH='inf', UCX_ZCOPY_THRESH='inf',
               UCX_LOG_LEVEL='warn', UCX_MODULE_DIR=str(install / 'lib/ucx'),
               LD_LIBRARY_PATH=':'.join(dict.fromkeys([str(install / 'lib')] +
                   saved.get('LD_LIBRARY_PATH', '').split(':'))).rstrip(':'),
               LD_PRELOAD=':'.join(str(install / 'lib' / ('lib' + n + '.so.0'))
                   for n in ('ucp', 'uct', 'ucs', 'ucm')),
               OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    if PROFILE == 'rm' and OBSERVE:
        # Load interposer before libuct_ub, which is later loaded as a plugin.
        env['LD_PRELOAD'] = str(PROBE) + ':' + env['LD_PRELOAD']
        env['HUCX_UM_SEND_MODULE'] = str(install / 'lib/ucx/libuct_ub.so.0.0.0')
        if FAULT: env['HUCX_UM_SEND_FAULT'] = 'loss_dup'
    return env


def check_config(text, env):
    parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
    prefix = 'UCX_' + PROFILE.upper() + '_URMA_'
    for k, v in env.items():
        if not k.startswith(prefix): continue
        actual = parsed.get(k, '')
        if k.endswith('_TIMEOUT'):
            # UCX pretty printer may convert 10s to 10.00s.
            equal = re.fullmatch(r'10(?:\.0+)?s', actual, re.I) is not None
        else:
            equal = actual.lower() == v.lower()
        if not equal: raise ValueError('effective configuration differs: %s=%s expected=%s' % (k, actual, v))
    if PROFILE == 'rm':
        if parsed.get(prefix + 'SEG_SIZE', '').upper() not in ('4K', '4KB', '4096', '4096B'):
            raise ValueError('RM default SEG_SIZE is not 4096')
        if any(prefix + key in parsed for key in ('LINK_MODE','MAX_PEERS','NATIVE_RMA')):
            raise ValueError('old RM protocol configuration was loaded')
    return parsed


def parsed_config(install, env, directory, unused):
    code = ('import json,os,subprocess,sys\n'
            'e={k:v for k,v in os.environ.items() if not k.startswith(("UCX_","HUCX_","MPI_UCX_")) and k not in ("LD_PRELOAD","LD_DEBUG")}\n'
            'e.update(json.loads(sys.argv[2]))\n'
            'p=subprocess.run([sys.argv[1]+"/bin/ucx_info","-c"],env=e,text=True,capture_output=True,timeout=35)\n'
            'print(json.dumps({"exit":p.returncode,"text":p.stdout+p.stderr}))\n')
    # No diagnostic observer in singleton ucx_info processes.
    effective = {k:v for k,v in env.items() if not k.startswith('HUCX_')}
    effective['LD_PRELOAD'] = ':'.join(str(install / 'lib' / ('lib'+n+'.so.0')) for n in ('ucp','uct','ucs','ucm'))
    argv = [sys.executable, '-c', code, str(install), json.dumps(effective)]
    local = json.loads(subprocess.check_output(argv, text=True, timeout=45))
    peer = json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3"\n', code, install, json.dumps(effective), timeout=45))
    for host, row in (('node89',local),('node97',peer)):
        (directory / (host+'_ucx_config.txt')).write_text(row['text'])
        if row['exit']: raise ValueError('ucx_info config failed: '+host)
        check_config(row['text'], env)
    return True


def deploy(case, build):
    state = osu.base.ssh('if test -e "$1"; then echo EXISTS; else echo ABSENT; fi\n', case).strip()
    if state == 'ABSENT':
        archive = case / 'install.tar.gz'
        osu.base.ssh('set -eu\nmkdir -p "$1"\nmkdir "$2"\n', LAB, case)
        osu.base.copy_remote([archive], str(case)+'/')
        if osu.base.ssh('sha256sum "$1"\n',archive).split()[0] != sha(archive):
            raise ValueError('peer install archive hash mismatch')
        osu.base.ssh('tar -xzf "$1" -C "$2"\n', archive, case, timeout=100)
    elif state != 'EXISTS': raise ValueError('unexpected remote state')
    return osu.base.module_check(case/'install', build['module_sha256'])


def fetch_protocol(run, row):
    pids = row['rank_evidence']['pids']
    names = ['protocol_rank%d_pid%d.json' % (rank,pids[rank]) for rank in range(row['ppn'],2*row['ppn'])]
    code = ('import json,pathlib,sys\nr=pathlib.Path(sys.argv[1]);out={}\n'
            'for n in json.loads(sys.argv[2]):\n'
            ' p=r/n\n'
            ' if p.is_symlink() or not p.is_file() or p.stat().st_size>65536: raise ValueError("protocol receipt missing/invalid")\n'
            ' out[n]=p.read_text()\n'
            'print(json.dumps(out))\n')
    files = json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3"\n',code,run,json.dumps(names)))
    if set(files) != set(names): raise ValueError('peer protocol coverage differs')
    for name, text in files.items(): (run/name).write_text(text)
    rows=[]
    for rank in range(2*row['ppn']):
        p=run/('protocol_rank%d_pid%d.json' % (rank,pids[rank]))
        value=json.loads(p.read_text())
        if (value['rank']!=rank or value['pid']!=pids[rank] or value['nonce']!=row['configuration']['nonce']
            or value['header_bytes']!=8 or value['key_overflow'] or value['duplicate_failed']):
            raise ValueError('protocol receipt identity/coverage failed')
        rows.append(value)
    return rows


def plan(with_um=False):
    sizes=[8,64,104,105,4079,4080,4081,8135,8136,8137,16384,65536,1048576]
    return ([('rc','osu_multi_lat',1,8,False)] +
        ([('um','osu_multi_lat',1,8,False)] if with_um else []) +
        [('rm','osu_multi_lat',1,n,False) for n in sizes] +
        [('rm',b,4,n,False) for b in ('osu_multi_lat','osu_alltoall') for n in (8,65536)] +
        [('rm','osu_multi_lat',1,65536,True)])


def validate_source(case):
    if case.parent != LAB or (case/'source/contrib/rm_ctp/um_send').resolve() != ROOT:
        raise ValueError('execute this isolated source under the new experiment root')
    build=json.loads((case/'build_manifest.json').read_text())
    if sha(case/'install/lib/ucx/libuct_ub.so.0.0.0') != build['module_sha256']:
        raise ValueError('candidate module changed after build')
    manifest=json.loads((case/'source/contrib/rm_ctp/r1/candidate_manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if sha(case/'source'/name)!=expected: raise ValueError('reviewed source changed: '+name)
    return build


def main():
    global PROFILE,OBSERVE,FAULT,PROBE
    parser=argparse.ArgumentParser(description='UM-derived RM/CTP SEND; default prints plan only')
    parser.add_argument('case',type=Path)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--timing',action='store_true')
    parser.add_argument('--with-um-control',action='store_true',help='also require a working native UM/UTP control path')
    args=parser.parse_args()
    if not args.run:
        print(json.dumps({'functional_mpi_tasks':len(plan(args.with_um_control)),'max_ppn':4,'max_bytes':1048576,
            'header_bytes':8,'psn_bits':16,'timing_optional':True,'native_read_write':False},indent=2));return
    if os.geteuid()!=0 or socket.gethostname()!='node89': raise ValueError('explicit run is limited to root on node89')
    case=args.case.resolve(); build=validate_source(case)
    out=Path(tempfile.mkdtemp(prefix='um_send_accept_',dir=case))
    print('RM_UM_SEND_DIR='+str(out),flush=True)
    report={'source_commit':build['source_commit'],'module_sha256':build['module_sha256'],
        'status':'RUNNING','scope':'SELECTED_SEND_NORMAL_AND_SOFTWARE_FAULT','cases':[],'timing':[]}
    def save():osu.base.write_json(out/'results.json',report)
    save()
    try:
        deploy(case,build)
        osu.DEFAULT_CASE=case;osu.PRODUCT_COMMIT=build['source_commit'];osu.PRODUCT_MODULE=build['module_sha256']
        osu.environment=environment;osu.base.parsed_config=parsed_config
        # Fixed receive budget only; no reliable_v3 K*W formula in this transport.
        osu.budget=lambda ppn:{'ppn':ppn,'tx_depth':64,'rx_depth':512,'window':32}
        commands={m:osu.saved_spec(osu.LAB/'osu_minimal8_ff_14ns8'/(m+'_run.json'),case/'install') for m in ('rm','rc')}
        PROFILE='rm'
        for mode,(argv,saved) in commands.items(): commands[mode]=(argv,environment(saved,mode,case/'install',{}))
        evidence=osu.preflight(out,commands);report['preflight']=evidence
        PROBE=out/'libum_send_probe.so'
        compile_cmd=['gcc','-std=gnu99','-shared','-fPIC','-O2','-g','-Wall','-Wextra','-Werror',
            '-DHAVE_CONFIG_H','-I'+str(case/'build'),'-I'+str(case/'build/src'),'-isystem',str(case/'source/src'),
            '-isystem','/usr/include/ub/umdk/urma',str(ROOT/'probe.c'),'-ldl','-lpthread','-o',str(PROBE)]
        osu.base.run_logged(compile_cmd,dict(os.environ),out/'compile_probe.log',60)
        for src,dst in ((osu.ROOT/'osu_rank.py',out/'osu_rank.py'),(PROJECT/'a89/rank_exec.py',out/'a89_rank_exec.py')):
            shutil.copy2(src,dst)
        osu.base.ssh('mkdir "$1"\n',out)
        osu.base.copy_remote([out/'libosu_identity.so',PROBE,out/'osu_rank.py',out/'a89_rank_exec.py'],str(out)+'/')
        report['tool_hashes']={p.name:sha(p) for p in (PROBE,out/'libosu_identity.so',out/'osu_rank.py',out/'a89_rank_exec.py')}
        trialargs=SimpleNamespace(iterations=100,warmup=10,timeout=120)
        for i,(profile,benchmark,ppn,n,fault) in enumerate(plan(args.with_um_control)):
            PROFILE=profile;OBSERVE=profile=='rm';FAULT=fault
            row=osu.one_trial(out,commands,evidence,benchmark,'rc' if profile=='rc' else 'rm',ppn,'validation',i,trialargs,message_bytes=n)
            row.update(profile=profile,software_fault=fault)
            run=out/row['name']
            if row['passed'] and OBSERVE:
                try:
                    wr.fetch_peer_summaries(run,row)
                    row['send_observations']=wr.inspect_trial(run,row,PROBE)
                    row['protocol']=fetch_protocol(run,row)
                    if fault:
                        totals={k:sum(r[k] for r in row['protocol']) for k in ('data_drop','ack_drop','duplicate','repeat_posts')}
                        if any(totals[k]<=0 for k in totals):raise ValueError('injection or actual repeated DATA post not observed')
                        row['fault_totals']=totals
                    if n==8 and benchmark=='osu_multi_lat':
                        for r in row['send_observations']:
                            if 24 not in r['lengths'] or r['inline_wrs']<=0:raise ValueError('8B 24-byte SEND/inline not observed')
                except Exception as exc:row.update(passed=False,error=type(exc).__name__+': '+str(exc))
            osu.base.write_json(run/'trial.json',row);report['cases'].append(row);save()
            print('SEND PROFILE=%s OSU=%s PPN=%d BYTES=%d INJECTION=%s PASS=%s' % (profile,benchmark,ppn,n,fault,row['passed']),flush=True)
            if not row['passed']:raise RuntimeError(row.get('error','trial failed')+' LOG='+str(run/'launcher.log'))
        if args.timing:
            OBSERVE=FAULT=False;trialargs=SimpleNamespace(iterations=10000,warmup=1000,timeout=120)
            for repeat in range(5):
                modes=['rc','um','rm'] if args.with_um_control else ['rc','rm'];modes=modes[repeat%len(modes):]+modes[:repeat%len(modes)]
                for profile in modes:
                    PROFILE=profile
                    row=osu.one_trial(out,commands,evidence,'osu_multi_lat','rc' if profile=='rc' else 'rm',1,'timing',100+repeat*3+modes.index(profile),trialargs)
                    row['profile']=profile;report['timing'].append(row);save()
                    if not row['passed']:raise RuntimeError(row.get('error','timing failed'))
            report['timing_summary']={m:{'samples_us':[r['output']['latency_us'] for r in report['timing'] if r['profile']==m]} for m in (('rc','um','rm') if args.with_um_control else ('rc','rm'))}
            for value in report['timing_summary'].values(): value['median_us']=statistics.median(value['samples_us'])
            print('OSU_8B='+json.dumps(report['timing_summary']),flush=True)
        report['status']='SELECTED_UM_DERIVED_RM_SEND_PASS_FULL_ACCEPTANCE_PENDING';save()
        print('所选SEND/分片/软件丢包恢复通过；硬件故障、规模、全性能、READ/WRITE未验收',flush=True)
    except Exception as exc:
        report.update(status='STOPPED_INCOMPLETE',error=type(exc).__name__+': '+str(exc));save()
        print('RM_UM_SEND_STOPPED='+report['error'],flush=True)
        raise
    finally:print('RESULTS_JSON='+str(out/'results.json'),flush=True)

if __name__=='__main__':main()
