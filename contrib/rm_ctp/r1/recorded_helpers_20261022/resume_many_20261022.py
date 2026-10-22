#!/usr/bin/env python3
"""Resume hardware many-peer checks; retain the completed 8B results."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

def main():
    lab = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019')
    case = lab / 'r1_shared_f0b5cd4ab_MEepCG'
    source = case / 'source/contrib/rm_ctp/r1'
    helper = source / 'accept_root_20261022.py'
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == 'e63a45a9d5fc85a41bbf56b5c184fc31c507239c699697c28c727ca8b2ab839a', 'helper changed'
    spec = importlib.util.spec_from_file_location('r1_accept', helper)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    prior = case / 'accept_20261022_091204'
    results = json.loads((prior / 'results.json').read_text())
    assert results['source_commit'] == 'f0b5cd4ab6fdfceaf6ce0ed98e64db91db861842'
    assert len(results['osu']) == 10 and all(r['exit_code'] == 0 for r in results['osu'])
    results['many_peer'] = []
    results['resumed_from'] = str(prior)
    results['runner'] = str(Path(__file__).resolve())
    results['runner_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    out = Path(tempfile.mkdtemp(prefix='many_depth8_', dir=str(prior)))
    install = case / 'install'
    wrapper = case / 'r1_peer_wrapper.sh'
    inspect_src = source / 'inspect_run.py'
    assert wrapper.is_file() and (case / 'r1_many_peer').is_file()
    os.environ['OMPI_ALLOW_RUN_AS_ROOT'] = '1'
    os.environ['OMPI_ALLOW_RUN_AS_ROOT_CONFIRM'] = '1'
    print('MANY_DIR=' + str(out), flush=True)
    for peers in (1, 2, 4):
        for kind in ('rm', 'rc'):
            cmd, env, idx = runner.saved_spec(kind, install, lab / 'osu_minimal8_ff_14ns8')
            env.pop('LD_DEBUG', None)
            d = out / ('%s_peers%d' % (kind, peers))
            d.mkdir()
            runner.remote('set -eu\nmkdir -p "$1"\n', str(d))
            depth_var = 'UCX_' + kind.upper() + '_URMA_TX_DEPTH'
            env.update(RM_RESOURCE_OBSERVER_DIR=str(d), R1_MAP_DIR=str(d))
            env[depth_var] = '8'
            cmd = runner.many_command(cmd, idx, wrapper, peers, 64 if peers < 4 else 1024)
            mp = next(i for i, x in enumerate(cmd) if Path(x).name in ('mpirun', 'mpiexec'))
            for key in ('RM_RESOURCE_OBSERVER_DIR', 'R1_MAP_DIR', depth_var):
                cmd[mp+1:mp+1] = ['-x', key]
            r = runner.run(cmd, env, out / ('%s_peers%d.log' % (kind, peers)))
            assert 'R1_MANY_PEER_PASS' in Path(r['log']).read_text(errors='replace'), 'data validation marker missing'
            mode = '1' if kind == 'rm' else '2'
            proc = subprocess.run([sys.executable, str(inspect_src), str(d), mode, str(install)], text=True, capture_output=True)
            if proc.returncode:
                raise RuntimeError('LOCAL_RESOURCE_CHECK_FAILED:\n' + proc.stderr[-1800:] + proc.stdout[-1800:])
            local = json.loads(proc.stdout)
            peer = json.loads(runner.remote('set -eu\npython3 "$1" "$2" "$3" "$4"\n', str(case / 'inspect_run.py'), str(d), mode, str(install)))
            assert set(local) == {'0'} and set(peer) == {str(i) for i in range(1, peers+1)}, 'rank placement mismatch'
            r.update(kind=kind, peers=peers, resources={**local, **peer}, validation=True, burst=16, tx_depth=8)
            results['many_peer'].append(r)
            (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            counts = list(local['0']['jetty_peak_by_context'].values())
            remote_counts = {k: list(v['jetty_peak_by_context'].values()) for k, v in peer.items()}
            print('MANY %s PEERS=%d PASS DEPTH=8 ROOT_JETTYS=%s PEER_JETTYS=%s MAPS=PASS LIVE_AT_EXIT=0' % (kind.upper(), peers, counts, remote_counts), flush=True)
    results['median_8B_us'] = {k: statistics.median(r['latency_us'] for r in results['osu'] if r['kind'] == k) for k in ('rc', 'rm')}
    results['status'] = 'MANY_PEER_CASES_PASSED; CTP guarantees and RMA outside this test'
    (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print('REUSED_8B_MEDIAN_US=' + json.dumps(results['median_8B_us']), flush=True)
    print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('R1_MANY_FAILED: ' + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
