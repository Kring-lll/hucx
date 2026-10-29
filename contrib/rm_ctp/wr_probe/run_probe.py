#!/usr/bin/env python3
"""Small standard OSU diagnostic; no transport rebuild or performance claim."""
import argparse
import hashlib
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


# Ordinary tag eager headers in this pinned 64-bit product: UCT=3,
# eager-only=8, eager-first=24, eager-middle=16 bytes. These are source
# calculations, not a promise that every MPI call uses this exact protocol.
BOUNDARY_SIZES = (8, 4084, 4085, 4086, 8145, 8146, 8147, 16384)
BENCHMARKS = ('osu_multi_lat', 'osu_alltoall')


def make_parser():
    parser = argparse.ArgumentParser(description='RM 4K配置下逐SEND长度验证；默认仅计划')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--boundary-matrix', action='store_true',
                        help='固定PPN4、两种标准OSU、8项真实头部边界；另跑两项RC 8B门禁')
    parser.add_argument('--ppn', type=int, choices=(1, 4), default=1)
    parser.add_argument('--sizes', type=int, nargs='+', default=[8, 4093, 4094, 8192, 16384])
    parser.add_argument('--benchmarks', choices=BENCHMARKS, nargs='+', default=['osu_multi_lat'])
    return parser


def validate_arguments(parser, args):
    if args.boundary_matrix:
        args.ppn = 4
        args.sizes = list(BOUNDARY_SIZES)
        args.benchmarks = list(BENCHMARKS)
    if (args.sizes != sorted(set(args.sizes))
            or any(size < 1 or size > 16384 for size in args.sizes)
            or args.benchmarks != list(dict.fromkeys(args.benchmarks))):
        parser.error('尺寸必须递增唯一且1..16384，用例不可重复')
    return args


def planned_matrix(args):
    return {'ppn': args.ppn, 'total_ranks': 2 * args.ppn,
            'sizes': args.sizes, 'benchmarks': args.benchmarks,
            'rm_validation_trials': len(args.sizes) * len(args.benchmarks),
            'rc_8b_gates': len(args.benchmarks) if args.boundary_matrix else 0,
            'segment_bytes': 4096, 'iterations': 100, 'warmup': 10,
            'validation': True, 'performance_claim': False,
            'transport_product_changed': False, 'C_retry': 'DEFERRED'}


def inspect_trial(run, row, library):
    """Check every actual OSU PID, not wrappers or a sample of ranks."""
    run = Path(run)
    ppn = row['ppn']
    pids = row['rank_evidence']['pids']
    if len(pids) != 2 * ppn:
        raise ValueError('OSU application PID coverage is incomplete')
    observations = []
    for rank in range(2 * ppn):
        pid = pids[rank]
        path = run / ('wr_rank%d_pid%d.json' % (rank, pid))
        summary = json.loads(path.read_text())
        for phase in ('after_init', 'before_finalize'):
            maps = (run / ('rank%d_%s.maps' % (rank, phase))).read_text()
            paths = [line.split()[-1] for line in maps.splitlines() if line.split()]
            if str(library) not in paths:
                raise ValueError('probe DSO absent from actual OSU mapping: rank %d' % rank)
        observation = inspect_summary(summary, rank, pid, row['configuration']['nonce'])
        observation.update(rank=rank, pid=pid)
        observations.append(observation)
    return observations


def fetch_peer_summaries(run, row):
    """One bounded SSH for the selected peer summaries; no new processes on devices."""
    run = Path(run)
    ppn = row['ppn']
    pids = row['rank_evidence']['pids']
    names = ['wr_rank%d_pid%d.json' % (rank, pids[rank]) for rank in range(ppn, 2 * ppn)]
    code = ('import json,pathlib,sys\n'
            'root=pathlib.Path(sys.argv[1]);result={}\n'
            'for name in json.loads(sys.argv[2]):\n'
            ' p=root/name\n'
            ' if p.is_symlink() or not p.is_file() or p.stat().st_size>1048576: '
            'raise ValueError("missing/nonregular/oversized WR summary")\n'
            ' result[name]=p.read_text()\n'
            'print(json.dumps(result))\n')
    files = json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3"\n',
                                    code, run, json.dumps(names), timeout=40))
    if set(files) != set(names):
        raise ValueError('peer WR summary rank coverage differs')
    receipt = {}
    for name in names:
        value = files[name]
        if not isinstance(value, str) or len(value.encode()) > 1048576:
            raise ValueError('invalid peer WR summary')
        path = run / name
        if path.exists() and path.read_text() != value:
            raise ValueError('existing WR summary differs: ' + name)
        path.write_text(value)
        receipt[name] = hashlib.sha256(value.encode()).hexdigest()
    osu.base.write_json(run / 'wr_fetch_receipt.json', receipt)
    return receipt


def main():
    parser = make_parser()
    args = validate_arguments(parser, parser.parse_args())
    if not args.run:
        print(json.dumps(planned_matrix(args), ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may explicitly run')
    out = Path(tempfile.mkdtemp(prefix='b_wr_probe_', dir=osu.DEFAULT_CASE))
    print('WR_PROBE_DIR=' + str(out), flush=True)
    report = {'tool_source_base': '4a61e88ddf8884f61893d5e0da1baf9c45e8d250',
              'product_commit': osu.PRODUCT_COMMIT, 'module_sha256': osu.PRODUCT_MODULE,
              'plan': planned_matrix(args), 'status': 'PREFLIGHT', 'scope': 'SEND_METADATA_NOT_NETWORK_PACKETS', 'cases': []}
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
        env = osu.environment(commands['rm'][1], 'iface', osu.DEFAULT_CASE / 'install', osu.budget(args.ppn))
        env[boundary.SEGMENT_KEY] = '4096'
        report['capability'] = boundary.check_remote_caps(osu.DEFAULT_CASE / 'install', env, out)
        osu.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        for src, name in ((osu.ROOT / 'osu_rank.py', 'osu_rank.py'),
                          (osu.A89 / 'rank_exec.py', 'a89_rank_exec.py'),
                          (ROOT / 'send_probe.c', 'send_probe.c'),
                          (ROOT / 'symbol_smoke.py', 'symbol_smoke.py'),
                          (ROOT / 'tests/fake_provider.c', 'fake_provider.c'),
                          (ROOT / 'tests/local_client.c', 'local_client.c')):
            shutil.copy2(src, out / name)
        for name in ('osu_rank.py', 'a89_rank_exec.py', 'send_probe.c', 'libosu_identity.so',
                     'symbol_smoke.py', 'fake_provider.c', 'local_client.c'):
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
        result = subprocess.run([sys.executable, str(out / 'symbol_smoke.py'), str(out)],
                                text=True, capture_output=True, timeout=100)
        (out / 'symbol_smoke_local.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise ValueError('local loader smoke failed; see symbol_smoke_local.log')
        report['symbol_smoke'] = {'local': json.loads(result.stdout),
            'peer': json.loads(osu.base.ssh('python3 "$1" "$2"\n',
                                          out / 'symbol_smoke.py', out, timeout=100))}
        save()
        print('符号转发门禁=双端局部加载模拟通过；尚非真实设备SEND验收', flush=True)
        trial_args = argparse.Namespace(iterations=100, warmup=10, timeout=120)
        if args.boundary_matrix:
            for benchmark in args.benchmarks:
                gate = osu.one_trial(out, commands, evidence, benchmark, 'rc', args.ppn,
                                     'validation', 0, trial_args, message_bytes=8)
                report['cases'].append(gate)
                save()
                if not gate['passed']:
                    raise ValueError('RC 8B regression gate failed: ' + str(gate.get('error')))
                print('RC_GATE OSU=%s PPN=%d SIZE=8 DATA=PASS EXIT=0' %
                      (benchmark, args.ppn), flush=True)
        original_environment = osu.environment

        def traced_environment(*items, **kwargs):
            value = original_environment(*items, **kwargs)
            # FIRST: otherwise preloaded liburma can bypass the observer.
            value['LD_PRELOAD'] = str(library) + ':' + value['LD_PRELOAD']
            return value

        osu.environment = traced_environment
        try:
            for benchmark in args.benchmarks:
                for size in args.sizes:
                    row = osu.one_trial(out, commands, evidence, benchmark, 'rm', args.ppn,
                                        'validation', 0, trial_args,
                                        message_bytes=size, segment_size=4096)
                    report['cases'].append(row)
                    save()
                    if not row['passed']:
                        raise ValueError('standard OSU failed: ' + str(row.get('error')))
                    run = out / row['name']
                    row['wr_probe_passed'] = False
                    save()
                    row['wr_fetch_sha256'] = fetch_peer_summaries(run, row)
                    observations = inspect_trial(run, row, library)
                    row['send_observations'] = observations
                    row['wr_probe_passed'] = True
                    save()
                    print('OSU=%s PPN=%d SIZE=%d DATA=PASS EXIT=0 RANKS=%d/%d '
                          'SEND_WR_MAX=%d LENGTH_BOUND_4096=PASS' %
                          (benchmark, args.ppn, size, len(observations), 2 * args.ppn,
                           max(item['max_send_bytes'] for item in observations)), flush=True)
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
