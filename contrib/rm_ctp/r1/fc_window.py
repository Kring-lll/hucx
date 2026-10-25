#!/usr/bin/env python3
"""Isolated FC-window regression; reuses the recorded burst without product edits."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

C_SOURCE = r'''/* One-way burst: no application ACK until all 16 messages arrive.
 * With FC window 8, independent credit grants are required for progress. */
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static void require(int ok) { if (!ok) MPI_Abort(MPI_COMM_WORLD, 21); }
static unsigned char pattern(int peer, unsigned seq, size_t off)
{ return (unsigned char)(peer * 17u + seq * 29u + off * 13u); }
static void record(int rank, int maps)
{
    char path[4096], key[128], line[4096];
    const char *dir = getenv("R1_MAP_DIR"), *kind = getenv("R2_FC_KIND");
    const char *names[] = {"FC_ENABLE", "FC_WND_SIZE", "TX_DEPTH", "RX_DEPTH"};
    const char *values[] = {"y", "8", "8", "64"};
    FILE *in, *out;
    unsigned i;
    require(dir && kind && (!strcmp(kind, "RM") || !strcmp(kind, "RC")));
    if (!maps) {
        snprintf(path, sizeof(path), "%s/rank%d.fc.env", dir, rank);
        out = fopen(path, "w"); require(out != NULL);
        for (i = 0; i < 4; ++i) {
            const char *value;
            snprintf(key, sizeof(key), "UCX_%s_URMA_%s", kind, names[i]);
            value = getenv(key); require(value && !strcmp(value, values[i]));
            fprintf(out, "%s=%s\n", key, value);
        }
        fclose(out); return;
    }
    snprintf(path, sizeof(path), "%s/rank%d_pid%ld.maps", dir, rank, (long)getpid());
    in = fopen("/proc/self/maps", "r"); out = fopen(path, "w");
    require(in && out);
    while (fgets(line, sizeof(line), in))
        if (strstr(line, "libucp") || strstr(line, "libuct") ||
            strstr(line, "libucs") || strstr(line, "libucm") || strstr(line, "liburma"))
            fputs(line, out);
    fclose(in); fclose(out);
}
int main(int argc, char **argv)
{
    int rank, size, peer, pos;
    unsigned seq, batch, rounds = 2000, burst = 16;
    size_t bytes, off, i;
    unsigned char *tx, *rx;
    unsigned long long ack[2];
    MPI_Request *req;
    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank); MPI_Comm_size(MPI_COMM_WORLD, &size);
    require(argc == 3); bytes = strtoul(argv[1], NULL, 10);
    rounds = strtoul(argv[2], NULL, 10);
    require(size >= 2 && bytes >= 8 && bytes <= 2048 && rounds > 0);
    tx = malloc(bytes * (size_t)size * burst); rx = malloc(bytes);
    req = malloc((size_t)size * burst * sizeof(*req)); require(tx && rx && req);
    record(rank, 0);
    for (seq = 0; seq < rounds; ++seq) {
        if (rank == 0) {
            pos = 0;
            for (batch = 0; batch < burst; ++batch)
                for (peer = 1; peer < size; ++peer) {
                    off = ((size_t)batch * size + peer) * bytes;
                    for (i = 0; i < bytes; ++i)
                        tx[off + i] = pattern(peer, seq * burst + batch, i);
                    MPI_Isend(tx + off, (int)bytes, MPI_BYTE, peer, 10,
                              MPI_COMM_WORLD, &req[pos++]);
                }
            MPI_Waitall(pos, req, MPI_STATUSES_IGNORE);
            for (peer = 1; peer < size; ++peer) {
                MPI_Recv(ack, sizeof(ack), MPI_BYTE, peer, 11,
                         MPI_COMM_WORLD, MPI_STATUS_IGNORE);
                require(ack[0] == (unsigned)peer && ack[1] == seq);
            }
        } else {
            for (batch = 0; batch < burst; ++batch) {
                MPI_Recv(rx, (int)bytes, MPI_BYTE, 0, 10, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
                for (i = 0; i < bytes; ++i)
                    require(rx[i] == pattern(rank, seq * burst + batch, i));
            }
            ack[0] = (unsigned)rank; ack[1] = seq;
            MPI_Send(ack, sizeof(ack), MPI_BYTE, 0, 11, MPI_COMM_WORLD);
        }
    }
    record(rank, 1); free(req); free(rx); free(tx);
    if (rank == 0) {
        printf("R2_FC_ONEWAY_PASS peers=%d bytes=%zu rounds=%u burst=%u\n",
               size - 1, bytes, rounds, burst); fflush(stdout);
    }
    MPI_Finalize(); return 0;
}
'''

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def settings(kind):
    prefix = 'UCX_' + kind.upper() + '_URMA_'
    values = {prefix+k:v for k,v in {'FC_ENABLE':'y', 'FC_WND_SIZE':'8',
        'TX_DEPTH':'8', 'RX_DEPTH':'64', 'FC_SOFT_THRESH':'0.5', 'FC_HARD_THRESH':'0.25'}.items()}
    # Keep these small messages on eager short/bcopy; rendezvous handshakes
    # must not provide an alternative piggyback-credit path in this test.
    values.update(UCX_RNDV_THRESH='inf', UCX_ZCOPY_THRESH='inf')
    return values

def configured_command(cmd, env, values):
    cmd = [x.split('=',1)[0]+'='+values[x.split('=',1)[0]]
           if '=' in x and x.split('=',1)[0] in values else x for x in cmd]
    env = dict(env); env.update(values)
    mp = next(i for i,x in enumerate(cmd) if Path(x).name in ('mpirun','mpiexec'))
    cmd[mp+1:mp+1] = [x for k in values for x in ('-x', k)]
    return cmd, env

def effective_env(cmd, env):
    selected = {k:v for k,v in env.items() if k.startswith('UCX_') or
                k in ('LD_PRELOAD','LD_LIBRARY_PATH')}
    for arg in cmd:
        k, sep, v = arg.partition('=')
        if sep and (k.startswith('UCX_') or k in ('LD_PRELOAD','LD_LIBRARY_PATH')):
            selected[k] = v
    return selected

def check_config(text, values):
    parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
    for key, expected in values.items():
        actual = parsed.get(key)
        if actual is None or (float(actual) != float(expected) if key.endswith('_THRESH') else actual != expected):
            raise RuntimeError('effective configuration mismatch: '+key+'='+str(actual))

def control_senders(text, minimum, maximum):
    """Diagnostic API objects, not hardware Jettys or network messages."""
    found = re.findall(r'\[([^]\n]+):\s*(\d+):\d+\].*'
                       r'RM control sender created iface=(\S+) ep=(\S+)', text)
    processes = {}
    for host, pid, iface, ep in found:
        process = host + ':' + pid
        if process in processes:
            raise RuntimeError('more than one control sender in '+process)
        processes[process] = {'iface': iface, 'ep': ep}
    if not minimum <= len(processes) <= maximum:
        raise RuntimeError('control sender coverage mismatch: '+str(len(processes)))
    return processes

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('case', type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--module', required=True)
    parser.add_argument('--normal-results', type=Path, required=True)
    args = parser.parse_args()
    case = args.case.resolve()
    commit, module_hash = args.commit, args.module
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('invalid fixed commit')
    if not re.fullmatch(r'[0-9a-f]{64}', module_hash):
        raise RuntimeError('invalid module hash')
    if args.normal_results.resolve().parent.parent != case:
        raise RuntimeError('normal results must belong to this isolated case')
    normal = json.loads(args.normal_results.read_text())
    if (normal['source_commit'] != commit or
            normal['module_sha256'] != module_hash or
            len(normal['many_peer']) != 6 or not normal['osu'] or
            not normal['status'].startswith('PLANNED_NODE_CASES_PASSED')):
        raise RuntimeError('normal regression identity or completion mismatch')
    src = case/'source/contrib/rm_ctp/r1'
    reviewed = json.loads((src/'candidate_manifest.json').read_text())
    for name, expected in reviewed['files'].items():
        if digest(case/'source'/name) != expected:
            raise RuntimeError('reviewed source mismatch: '+name)
    spec = importlib.util.spec_from_file_location('r2_existing_accept', src/'accept.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    if case.parent != runner.LAB:
        raise RuntimeError('case must be a direct child of experiment root')
    install = case/'install'; module = install/'lib/ucx/libuct_ub.so.0.0.0'
    manifest = json.loads((case/'build_manifest.json').read_text())
    if manifest['source_commit'] != commit or digest(module) != module_hash:
        raise RuntimeError('product identity mismatch')
    peer_hash = runner.remote('set -eu\nsha256sum "$1"\n', str(module)).split()[0]
    if peer_hash != module_hash: raise RuntimeError('peer module identity mismatch')
    os.environ['OMPI_ALLOW_RUN_AS_ROOT'] = '1'
    os.environ['OMPI_ALLOW_RUN_AS_ROOT_CONFIRM'] = '1'
    out = Path(tempfile.mkdtemp(prefix='a4_control_fc_window8_', dir=case))
    shutil.copyfile(__file__, out/'executed_helper.py')
    print('FC_DIR='+str(out), flush=True)
    results = {'source_commit':commit, 'module_sha256':module_hash, 'cases':[],
               'helper_sha256':digest(__file__),
               'normal_regression_results':str(args.normal_results.resolve()),
               'status':'IN_PROGRESS', 'scope':'Normal software-FC progress; no hardware fault injection'}
    report = out/'results.json'
    report.write_text(json.dumps(results,indent=2)+'\n')
    specs = {k:runner.saved_spec(k, install, runner.LAB/'osu_minimal8_ff_14ns8') for k in ('rm','rc')}
    for kind in ('rm','rc'):
        cmd, env, _ = specs[kind]
        cmd, env = configured_command(cmd, env, settings(kind))
        selected = effective_env(cmd, env)
        probe = ('import os,subprocess,sys\n'
                 'env={k:v for k,v in os.environ.items() if not k.startswith("UCX_")}\n'
                 'env.update('+repr(selected)+')\n'
                 'p=subprocess.run('+repr([str(install/'bin/ucx_info'),'-c'])+',env=env,text=True,capture_output=True,timeout=40)\n'
                 'sys.stdout.write(p.stdout);sys.stderr.write(p.stderr);sys.exit(p.returncode)\n')
        local = subprocess.run([sys.executable,'-c',probe],capture_output=True,text=True,timeout=50)
        (out/(kind+'_node89_config.log')).write_text(local.stdout+local.stderr)
        if local.returncode: raise RuntimeError('local config tool failed: '+local.stderr[-600:])
        other = runner.remote("set -eu\npython3 - <<'R2_CONFIG'\n"+probe+'R2_CONFIG\n')
        (out/(kind+'_node97_config.log')).write_text(other)
        check_config(local.stdout, settings(kind)); check_config(other, settings(kind))
        print('CONFIG '+kind.upper()+' BOTH_NODES=PASS FC=y WINDOW=8 TX=8 RX=64', flush=True)
    c = out/'fc_oneway.c'; c.write_text(C_SOURCE)
    binary = out/'fc_oneway'
    mpi = next(Path(x) for x in specs['rm'][0] if Path(x).name in ('mpirun','mpiexec'))
    subprocess.run([str(mpi.with_name('mpicc')),'-O2','-Wall','-Wextra',str(c),'-o',str(binary)],check=True)
    results['test_source_sha256'] = digest(c)
    results['test_binary_sha256'] = digest(binary)
    old_wrapper = case/'r1_peer_wrapper.sh'
    text = old_wrapper.read_text()
    old_exec = 'exec "'+str(case/'r1_many_peer')+'" "$@"'
    if text.count(old_exec)!=1: raise RuntimeError('unexpected existing observer wrapper')
    wrapper = out/'fc_wrapper.sh'
    wrapper.write_text(text.replace(old_exec,'exec "'+str(binary)+'" "$@"')); wrapper.chmod(0o755)
    runner.remote('set -eu\nmkdir "$1"\n', str(out))
    subprocess.run(['scp','-q',str(binary),str(wrapper),runner.PEER+':'+str(out)+'/'],check=True)
    hashes = runner.remote('set -eu\nsha256sum "$1" "$2"\n',str(binary),str(wrapper)).splitlines()
    if [s.split()[0] for s in hashes] != [digest(binary),digest(wrapper)]:
        raise RuntimeError('peer test binary/wrapper identity mismatch')
    for peers in (1,2,4):
        for kind in ('rm','rc'):
            cmd, env, idx = specs[kind]
            cmd = runner.many_command(cmd, idx, wrapper, peers, 64 if peers<4 else 1024)
            d = out/(kind+'_peers'+str(peers)); d.mkdir()
            runner.remote('set -eu\nmkdir "$1"\n',str(d))
            values = settings(kind)
            values.update(RM_RESOURCE_OBSERVER_DIR=str(d),R1_MAP_DIR=str(d),R2_FC_KIND=kind.upper())
            if kind == 'rm':
                values['UCX_LOG_LEVEL'] = 'debug'
            cmd, env = configured_command(cmd, env, values)
            r = runner.run(cmd, env, out/(kind+'_peers'+str(peers)+'.log'))
            marker = 'R2_FC_ONEWAY_PASS peers='+str(peers)+' '
            if marker not in Path(r['log']).read_text(errors='replace'): raise RuntimeError('data marker missing')
            local = json.loads(subprocess.check_output([sys.executable,str(src/'inspect_run.py'),str(d),'1' if kind=='rm' else '2',str(install)],text=True))
            other = json.loads(runner.remote('set -eu\npython3 "$1" "$2" "$3" "$4"\n',str(case/'inspect_run.py'),str(d),'1' if kind=='rm' else '2',str(install)))
            if set(local)!={'0'} or set(other)!={str(i) for i in range(1,peers+1)}: raise RuntimeError('rank placement mismatch')
            recorded = {k:v for k,v in settings(kind).items() if not k.endswith('_THRESH')}
            env_probe = ('import json,pathlib\n'
                         'd=pathlib.Path('+repr(str(d))+')\n'
                         'print(json.dumps({p.stem:dict(s.split("=",1) for s in p.read_text().splitlines()) for p in d.glob("rank*.fc.env")}))\n')
            local_env = json.loads(subprocess.check_output([sys.executable,'-c',env_probe],text=True))
            other_env = json.loads(runner.remote("set -eu\npython3 - <<'R2_ENV'\n"+env_probe+'R2_ENV\n'))
            if set(local_env)!={'rank0.fc'} or set(other_env)!={'rank'+str(i)+'.fc' for i in range(1,peers+1)}: raise RuntimeError('rank FC record mismatch')
            if any(v!=recorded for v in list(local_env.values())+list(other_env.values())): raise RuntimeError('rank FC environment mismatch')
            r.update(kind=kind, peers=peers, bytes=64 if peers<4 else 1024, rounds=2000, burst=16,
                     resources={**local,**other}, rank_environment={**local_env,**other_env}, settings=settings(kind))
            if kind == 'rm':
                # Receivers need standalone grants to complete a 16-message
                # burst at W8. The sender may answer soft requests by
                # piggybacking, so its private owner can legitimately be absent.
                r['control_senders'] = control_senders(
                    Path(r['log']).read_text(errors='replace'), peers, peers + 1)
            results['cases'].append(r); report.write_text(json.dumps(results,indent=2)+'\n')
            if kind == 'rm':
                print('CONTROL_SENDERS COUNT='+str(len(r['control_senders']))+
                      ' MAX_ONE_PER_PROCESS=PASS', flush=True)
            print('FC '+kind.upper()+' PEERS='+str(peers)+' PASS WINDOW=8 DEPTH=8 MAPS=PASS LIVE_AT_EXIT=0',flush=True)
    results['status']='FC_NORMAL_PROGRESS_PASSED; injected failures remain CPU-only; full CTP contract not accepted'
    report.write_text(json.dumps(results,indent=2)+'\n')
    print('RESULTS_JSON='+str(report),flush=True)

if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print('A4_FC_FAILED: '+str(exc),file=sys.stderr,flush=True)
        sys.exit(1)
