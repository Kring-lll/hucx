#!/usr/bin/env python3
"""Small standard OSU diagnostic; no transport rebuild or performance claim."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'boundary'))
sys.path.insert(0, str(PROJECT / 'osu_compare'))
import run_boundary as boundary
osu = boundary.osu


def inspect_summary(row, rank, pid, nonce):
    if (row.get('schema') != 1 or row.get('rank') != rank or row.get('pid') != pid
            or row.get('nonce') != nonce or row.get('calls', 0) <= 0
            or row.get('error_calls') != 0 or row.get('incomplete') != 0
            or row.get('provider_is_liburma') is not True):
        raise ValueError('missing/wrong identity, forwarding provider, or incomplete/error trace')
    buckets = row.get('buckets', [])
    if not buckets or any(not 0 <= b['bytes'] <= 4096 or b['submitted'] <= 0
                          or b['success'] != b['submitted']
                          or not 0 <= b['num_sge'] <= 64
                          or b['inline'] not in (0, 1) for b in buckets):
        raise ValueError('SEND length exceeds 4096 or invalid/incomplete bucket')
    return {'send_wrs': sum(b['submitted'] for b in buckets),
            'max_send_bytes': max(b['bytes'] for b in buckets),
            'lengths': sorted({b['bytes'] for b in buckets}),
            'inline_wrs': sum(b['submitted'] for b in buckets if b['inline'])}


def main():
    parser = argparse.ArgumentParser(description='RM 4K配置下逐SEND长度直方图；默认仅计划')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('PLAN RM_ONLY PPN=1 OSU=osu_multi_lat SIZES=8/4093/4094/8192/16384')
        print('ITERATIONS=100 WARMUP=10 VALIDATION=YES SEG=4096 DIAGNOSTIC_NOT_PERFORMANCE')
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may explicitly run')
    out = Path(tempfile.mkdtemp(prefix='b_wr_probe_', dir=osu.DEFAULT_CASE))
    print('WR_PROBE_DIR=' + str(out), flush=True)
    report = {'tool_source_base': '0376bbd1acbe53a1a02be4475d8ee120464b0811',
              'product_commit': osu.PRODUCT_COMMIT, 'module_sha256': osu.PRODUCT_MODULE,
              'status': 'PREFLIGHT', 'scope': 'SEND_METADATA_NOT_NETWORK_PACKETS', 'cases': []}
    def save():
        osu.base.write_json(out / 'results.json', report)
    save()
    try:
        osu.validate_package()
        manifest = json.loads((osu.DEFAULT_CASE / 'build_manifest.json').read_text())
        if (manifest['source_commit'] != osu.PRODUCT_COMMIT or
                manifest['module_sha256'] != osu.PRODUCT_MODULE):
            raise ValueError('fixed product build identity mismatch')
        commands = {mode: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                        (mode + '_run.json'), osu.DEFAULT_CASE / 'install')
                    for mode in ('rm', 'rc')}
        evidence = osu.preflight(out, commands)
        env = osu.environment(commands['rm'][1], 'iface', osu.DEFAULT_CASE / 'install', osu.budget(1))
        env[boundary.SEGMENT_KEY] = '4096'
        report['capability'] = boundary.check_remote_caps(osu.DEFAULT_CASE / 'install', env, out)
        osu.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        for src, name in ((osu.ROOT / 'osu_rank.py', 'osu_rank.py'),
                          (osu.A89 / 'rank_exec.py', 'a89_rank_exec.py'),
                          (ROOT / 'send_probe.c', 'send_probe.c')):
            shutil.copy2(src, out / name)
        for name in ('osu_rank.py', 'a89_rank_exec.py', 'send_probe.c', 'libosu_identity.so'):
            osu.base.copy_remote([out / name], str(out) + '/')
            if osu.base.ssh('sha256sum "$1"\n', out / name).split()[0] != osu.digest(out / name):
                raise ValueError('peer helper mismatch: ' + name)
        library = out / 'libsend_probe.so'
        command = ['gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                   '-I/usr/include/ub/umdk/urma', str(out / 'send_probe.c'),
                   '-o', str(library), '-ldl', '-pthread']
        result = subprocess.run(command, text=True, capture_output=True, timeout=40)
        (out / 'compile_local.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise ValueError('local diagnostic compile failed; see compile_local.log')
        peer_compile = osu.base.ssh('set -eu\ngcc -std=gnu11 -O2 -Wall -Wextra -Werror -shared -fPIC '
                                    '-I/usr/include/ub/umdk/urma "$1" -o "$2" -ldl -pthread\n',
                                    out / 'send_probe.c', library, timeout=45)
        (out / 'compile_peer.log').write_text(peer_compile)
        report['probe_sha256'] = {'local': osu.digest(library),
                                  'peer': osu.base.ssh('sha256sum "$1"\n', library).split()[0]}
        original_environment = osu.environment
        def traced_environment(*items, **kwargs):
            value = original_environment(*items, **kwargs)
            # FIRST: otherwise preloaded liburma may win symbol lookup and bypass us.
            value['LD_PRELOAD'] = str(library) + ':' + value['LD_PRELOAD']
            return value
        osu.environment = traced_environment
        try:
            for size in (8, 4093, 4094, 8192, 16384):
                row = osu.one_trial(out, commands, evidence, 'osu_multi_lat', 'rm', 1,
                                    'validation', 0,
                                    argparse.Namespace(iterations=100, warmup=10, timeout=120),
                                    message_bytes=size, segment_size=4096)
                report['cases'].append(row)
                save()
                if not row['passed']:
                    raise ValueError('standard OSU failed: ' + row.get('error', str(row)))
                run = out / row['name']
                row['wr_probe_passed'] = False
                save()
                observations = []
                for rank in (0, 1):
                    pid = row['rank_evidence']['pids'][rank]
                    path = run / ('wr_rank%d_pid%d.json' % (rank, pid))
                    if rank == 1:
                        path.write_text(osu.base.ssh('cat "$1"\n', path))
                    summary = json.loads(path.read_text())
                    for phase in ('after_init', 'before_finalize'):
                        maps = (run / ('rank%d_%s.maps' % (rank, phase))).read_text()
                        if str(library) not in [line.split()[-1] for line in maps.splitlines() if line.split()]:
                            raise ValueError('probe DSO missing from actual OSU mapping')
                    observations.append(inspect_summary(summary, rank, pid,
                                                        row['configuration']['nonce']))
                row['send_observations'] = observations
                row['wr_probe_passed'] = True
                save()
                print('SIZE=%d DATA=PASS EXIT=0 SEND_WR_MAX=%s COUNTS=%s LENGTH_BOUND_4096=PASS' %
                      (size, [v['max_send_bytes'] for v in observations],
                       [v['send_wrs'] for v in observations]), flush=True)
        finally:
            osu.environment = original_environment
        report['status'] = 'SELECTED_SEND_LENGTH_DIAGNOSTIC_PASS'
        print('WR诊断=通过；统计含流控/建链/集合通信，不能把总WR数当单条MPI消息片数', flush=True)
        print('硬件最大报长/网络分包/故障恢复=本次未测；无性能结论', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('WR_PROBE_STOPPED=' + report['error'][:500], flush=True)
        raise SystemExit(1)
    finally:
        save()
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)

if __name__ == '__main__':
    main()
