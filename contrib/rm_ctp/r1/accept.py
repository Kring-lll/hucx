#!/usr/bin/env python3
"""Run on node89. Reuse saved, validated launcher JSON; no environment probing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import statistics
import subprocess
import sys
import time

LAB = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019')
OLD = str(LAB / 'rm_minimal_d58a141bc_zMBCk1/install')
PEER = 'root@9.88.43.97'

def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def run(cmd, env, out, timeout=120):
    out.parent.mkdir(parents=True, exist_ok=True)
    selected = {k:v for k,v in env.items() if k.startswith('UCX_') or
                k in ('LD_PRELOAD','LD_LIBRARY_PATH','RM_RESOURCE_OBSERVER_DIR','R1_MAP_DIR')}
    out.with_suffix('.json').write_text(json.dumps({'command':cmd, 'environment':selected}, indent=2)+'\n')
    start = time.monotonic()
    with out.open('w') as f:
        proc = subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            raise RuntimeError('timeout; own launcher terminated: '+str(out))
    if rc:
        raise RuntimeError('EXIT=%d LOG=%s\n%s' % (rc,out,'\n'.join(out.read_text(errors='replace').splitlines()[-12:])))
    return {'exit_code':rc,'seconds':time.monotonic()-start,'log':str(out)}

def saved_spec(kind, install, baseline):
    obj=json.loads((baseline/(kind+'_run.json')).read_text(encoding='utf-8-sig'))
    cmd=obj.get('command')
    if isinstance(cmd,str): cmd=shlex.split(cmd)
    if not isinstance(cmd,list) or not all(isinstance(x,str) for x in cmd):
        raise RuntimeError('unsupported saved command shape; keys='+','.join(obj))
    original_env=obj.get('environment',{})
    if isinstance(original_env,list): original_env=dict(x.split('=',1) for x in original_env)
    if not isinstance(original_env,dict): raise RuntimeError('unsupported environment shape')
    cmd=[x.replace(OLD,str(install)) for x in cmd]
    env={k:v for k,v in os.environ.items() if not k.startswith('UCX_')}
    env.update({k:str(v).replace(OLD,str(install)) for k,v in original_env.items()})
    env['UCX_MODULE_DIR']=str(install/'lib/ucx')
    if str(install) not in ' '.join(cmd)+str(original_env).replace(OLD,str(install)):
        raise RuntimeError('saved command does not reference expected tested install; refusing to guess')
    if '-c' not in cmd: raise RuntimeError('OSU data validation -c missing in saved command')
    idx=next((i for i,x in enumerate(cmd) if Path(x).name=='osu_latency'),None)
    if idx is None: raise RuntimeError('OSU executable not found in saved command')
    return cmd,env,idx

def remote(script, *args):
    # All arguments are generated paths or fixed internal node identifiers.
    cmd=['ssh',PEER,'bash','-s','--']+list(args)
    p=subprocess.run(cmd,input=script,text=True,capture_output=True)
    if p.returncode:
        raise RuntimeError('SSH failed: '+p.stderr[-1200:]+p.stdout[-1200:])
    return p.stdout

def many_command(cmd, idx, wrapper, peers, size):
    result=cmd[:idx]+[str(wrapper),str(size),'2000']
    found_n=found_host=False
    for i,x in enumerate(result[:-1]):
        if x in ('-n','-np','--np'):
            result[i+1]=str(peers+1); found_n=True
        if x in ('--host','-H'):
            result[i+1]='9.88.43.89:1,9.88.43.97:'+str(peers); found_host=True
        if x in ('--map-by','-map-by'):
            result[i+1]='slot'
        if re.fullmatch(r'-n(?:p)?2',x):
            result[i]='-np'+str(peers+1); found_n=True
    if not found_host:
        raise RuntimeError('launcher lacks explicit host list; refusing to guess')
    if not found_n:
        mp=next(i for i,x in enumerate(result) if Path(x).name in ('mpirun','mpiexec'))
        result[mp+1:mp+1]=['-np',str(peers+1)]
    return result

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('case',type=Path)
    ap.add_argument('--baseline',type=Path,default=LAB/'osu_minimal8_ff_14ns8')
    ap.add_argument('--no-deploy',action='store_true')
    ap.add_argument('--repeats',type=int,default=5)
    args=ap.parse_args()
    case=args.case.resolve(); install=case/'install'
    if case.parent!=LAB: raise RuntimeError('case must be an isolated direct child of experiment root')
    manifest=json.loads((case/'build_manifest.json').read_text())
    module=install/'lib/ucx/libuct_ub.so.0.0.0'
    assert digest(module)==manifest['module_sha256'], 'candidate module changed after build'
    out=case/('accept_'+time.strftime('%Y%m%d_%H%M%S'))
    out.mkdir()
    print('ACCEPT_DIR='+str(out),flush=True)
    specs={k:saved_spec(k,install,args.baseline) for k in ('rm','rc')}
    if not args.no_deploy:
        package=case/'install.tar.gz'
        target=LAB/'upload'/('r1_install_'+manifest['source_commit'][:9]+'.tar.gz')
        remote('set -eu\nmkdir -p "$1"\n',str(target.parent))
        subprocess.run(['scp','-q',str(package),PEER+':'+str(target)],check=True)
        remote('set -eu\ntest ! -e "$1"\nmkdir "$1"\ntar -xzf "$2" -C "$1"\n',str(case),str(target))
    remote_sha=remote('set -eu\nsha256sum "$1"\nldd "$1"\n',str(module))
    if remote_sha.split()[0]!=manifest['module_sha256'] or 'not found' in remote_sha:
        raise RuntimeError('peer module hash or dependencies mismatch')
    print('PEER_MODULE_SHA256_MATCH=PASS',flush=True)
    rm_cmd,rm_env,rm_idx=specs['rm']
    mpi=next(Path(x) for x in rm_cmd if Path(x).name in ('mpirun','mpiexec'))
    binary=case/'r1_many_peer'
    subprocess.run([str(mpi.with_name('mpicc')),'-O2','-Wall','-Wextra',
                    str(Path(__file__).with_name('many_peer.c')),'-o',str(binary)],check=True)
    wrapper=case/'r1_peer_wrapper.sh'
    wrapper.write_text('#!/bin/bash\nset -eu\n'
        'case "$OMPI_COMM_WORLD_RANK" in\n'
        '0) observer="'+str(LAB/'observer_build_d886fc3/liburma_resource_observer.so')+'";;\n'
        '*) observer="'+str(LAB/'observer_runtime_d886fc3/liburma_resource_observer.so')+'";;\nesac\n'
        'test -f "$observer"\nexport LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$observer"\n'
        'exec "'+str(binary)+'" "$@"\n')
    wrapper.chmod(0o755)
    inspect_src=Path(__file__).with_name('inspect_run.py')
    inspect_peer=case/'inspect_run.py'
    subprocess.run(['scp','-q',str(binary),str(wrapper),str(inspect_src),PEER+':'+str(case)+'/'],check=True)
    results={'source_commit':manifest['source_commit'],'module_sha256':manifest['module_sha256'],
             'osu':[],'many_peer':[],'scope':'R1 AM only; no hardware loss/retry or RMA acceptance'}
    samples={'rm':[],'rc':[]}
    # A loader-diagnostic correctness run is excluded from performance samples.
    cmd,env,idx=specs['rm']; diagnostic=dict(env)
    diagnostic['LD_DEBUG']='libs'
    r=run(cmd,diagnostic,out/'loader_check.log')
    loader=Path(r['log']).read_text(errors='replace')
    if str(module) not in loader and str(install/'lib/ucx/libuct_ub.so') not in loader:
        raise RuntimeError('loader trace does not show candidate URMA module')
    print('LOADER_CHECK=PASS VALIDATION_ENABLED=YES EXIT=0',flush=True)
    for rep in range(args.repeats):
        for kind in (('rc','rm') if rep%2==0 else ('rm','rc')):
            cmd,env,_=specs[kind]
            r=run(cmd,env,out/('%s_%02d.log'%(kind,rep)))
            text=Path(r['log']).read_text(errors='replace')
            matches=re.findall(r'^\s*8\s+([0-9]+(?:\.[0-9]+)?)\s+Pass\s*$',text,re.M)
            if len(matches)!=1: raise RuntimeError('cannot parse one 8B latency row: '+r['log'])
            value=float(matches[0]); samples[kind].append(value)
            r.update(kind=kind,repeat=rep,bytes=8,latency_us=value,validation_enabled=True)
            results['osu'].append(r)
            print('OSU %s REP=%d 8B_US=%.3f VALIDATION_ENABLED=YES EXIT=0'%(kind.upper(),rep,value),flush=True)
            (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    for peers in (1,2,4):
        for kind in ('rm','rc'):
            cmd,env,idx=specs[kind]; env=dict(env)
            d=out/('%s_peers%d'%(kind,peers)); d.mkdir()
            remote('set -eu\nmkdir -p "$1"\n',str(d))
            env['RM_RESOURCE_OBSERVER_DIR']=str(d); env['R1_MAP_DIR']=str(d)
            env['UCX_'+kind.upper()+'_URMA_TX_DEPTH']='8'
            cmd=many_command(cmd,idx,wrapper,peers,64 if peers<4 else 1024)
            mp=next(i for i,x in enumerate(cmd) if Path(x).name in ('mpirun','mpiexec'))
            for key in ('RM_RESOURCE_OBSERVER_DIR','R1_MAP_DIR','UCX_'+kind.upper()+'_URMA_TX_DEPTH'):
                cmd[mp+1:mp+1]=['-x',key]
            r=run(cmd,env,out/('%s_peers%d.log'%(kind,peers)))
            if 'R1_MANY_PEER_PASS' not in Path(r['log']).read_text(errors='replace'):
                raise RuntimeError('multi-peer validation marker missing')
            mode=1 if kind=='rm' else 2
            local=json.loads(subprocess.check_output([sys.executable,str(inspect_src),str(d),str(mode),str(install)],text=True))
            other=json.loads(remote('set -eu\npython3 "$1" "$2" "$3" "$4"\n',str(inspect_peer),str(d),str(mode),str(install)))
            if set(local)!={'0'} or set(other)!={str(i) for i in range(1,peers+1)}:
                raise RuntimeError('unexpected rank placement or observer count')
            counts=local['0']['jetty_peak_by_context']
            r.update(kind=kind,peers=peers,resources={**local,**other},validation=True,burst=16)
            results['many_peer'].append(r)
            (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
            print('MANY %s PEERS=%d PASS JETTY_BY_CONTEXT=%s EXIT=0'%(kind.upper(),peers,json.dumps(list(counts.values()))),flush=True)
    results['median_8B_us']={k:statistics.median(v) for k,v in samples.items()}
    results['status']='PLANNED_NODE_CASES_PASSED; CTP semantic guarantees require contract evidence'
    (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print('MEDIAN_8B_US='+json.dumps(results['median_8B_us']),flush=True)
    print('RESULTS_JSON='+str(out/'results.json'),flush=True)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('R1_ACCEPT_FAILED: '+str(exc),file=sys.stderr,flush=True)
        sys.exit(1)
