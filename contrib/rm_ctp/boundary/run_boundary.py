#!/usr/bin/env python3
"""Bounded standard-OSU size validation using the already measured RM install."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'osu_compare'))
import run as osu
from boundary_config import DEVICE, SEGMENT_KEY, check_outputs, query_local

SIZES = (8, 117, 118, 4092, 4093, 4094, 8192, 16384)
BENCHMARKS = ('osu_multi_lat', 'osu_alltoall')


def check_remote_caps(install, env, out):
    local = query_local(install, env)
    code = ('import json,os,subprocess,sys\n'
            'env={k:v for k,v in os.environ.items() if not k.startswith("UCX_")}\n'
            'env.update(json.loads(sys.argv[2]))\n'
            'info=sys.argv[1]+"/bin/ucx_info"\n'
            'data={}\n'
            'for key,argv in (("config",[info,"-c"]),("device",[info,"-d","-t","rm_urma"])):\n'
            ' p=subprocess.run(argv,env=env,text=True,capture_output=True,timeout=40)\n'
            ' if p.returncode: raise RuntimeError(key+":"+p.stderr[-400:])\n'
            ' data[key]=p.stdout\n'
            'print(json.dumps(data))\n')
    remote = json.loads(osu.base.ssh('exec python3 -c "$1" "$2" "$3"\n',
                                     code, install, json.dumps(env), timeout=110))
    report = {}
    for host, output in (('node89', local), ('node97', remote)):
        for kind in ('config', 'device'):
            (out / (host + '_ucx_' + kind + '.txt')).write_text(output[kind])
        report[host] = check_outputs(output, env, 4096)
    return report


def plan(args):
    return {'product_commit': osu.PRODUCT_COMMIT,
            'module_sha256': osu.PRODUCT_MODULE,
            'seg_bytes': 4096, 'device': DEVICE,
            'osubinary_original': True,
            'size_bytes': args.sizes, 'ppn': args.ppn,
            'benchmarks': args.benchmarks,
            'validation': True, 'performance_claim': False,
            'ctp_single_wr_limit': 'NOT_ESTABLISHED',
            'reliability_C': 'DEFERRED'}


def main():
    parser = argparse.ArgumentParser(description='标准OSU B1/B2/B3尺寸边界正常路径验证；默认只输出计划')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true', help='明确启动两节点；逐项校验，失败停止')
    action.add_argument('--preflight', action='store_true', help='双端能力和OSU依赖预检，不启动MPI')
    parser.add_argument('--sizes', type=int, nargs='+', default=list(SIZES))
    parser.add_argument('--ppn', type=int, nargs='+', default=[1, 4])
    parser.add_argument('--benchmarks', choices=BENCHMARKS, nargs='+', default=list(BENCHMARKS))
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()
    if (args.sizes != sorted(set(args.sizes)) or any(s < 1 or s > 16384 for s in args.sizes)
            or args.ppn != sorted(set(args.ppn)) or any(p < 1 or p > 4 for p in args.ppn)
            or args.benchmarks != list(dict.fromkeys(args.benchmarks))
            or not 60 <= args.timeout <= 600):
        parser.error('sizes递增唯一且1..16384，PPN递增唯一且1..4，超时60..600')
    if not (args.run or args.preflight):
        print(json.dumps(plan(args), indent=2, ensure_ascii=False))
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise ValueError('节点任务只允许root在node89显式启动')
    build = json.loads((osu.DEFAULT_CASE / 'build_manifest.json').read_text())
    if build['source_commit'] != osu.PRODUCT_COMMIT or build['module_sha256'] != osu.PRODUCT_MODULE:
        raise ValueError('固定已测产品版本不符')
    out = Path(tempfile.mkdtemp(prefix='b_boundary_', dir=osu.DEFAULT_CASE))
    print('B_BOUNDARY_DIR=' + str(out), flush=True)
    report = {'plan': plan(args), 'status': 'PREFLIGHT', 'cases': []}

    def save():
        osu.base.write_json(out / 'results.json', report)

    save()
    try:
        osu.validate_package()
        commands = {kind: osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' /
                                         (kind + '_run.json'), osu.DEFAULT_CASE / 'install')
                    for kind in ('rm', 'rc')}
        evidence = osu.preflight(out, commands)
        env = osu.environment(commands['rm'][1], 'iface',
                              osu.DEFAULT_CASE / 'install', osu.budget(max(args.ppn)))
        env[SEGMENT_KEY] = '4096'
        report['capability'] = check_remote_caps(osu.DEFAULT_CASE / 'install', env, out)
        report['preflight'] = evidence
        save()
        print('双端配置=4K AM_BCOPY=4093 产品/OSU/HMPI哈希=PASS', flush=True)
        if not args.run:
            report['status'] = 'PREFLIGHT_PASS_NO_MPI'
            save()
            return
        shutil.copy2(osu.ROOT / 'osu_rank.py', out / 'osu_rank.py')
        shutil.copy2(osu.A89 / 'rank_exec.py', out / 'a89_rank_exec.py')
        osu.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        for path in (out / 'osu_rank.py', out / 'a89_rank_exec.py',
                     out / 'libosu_identity.so'):
            osu.base.copy_remote([path], str(out) + '/')
            if osu.base.ssh('sha256sum "$1"\n', path).split()[0] != osu.digest(path):
                raise ValueError('peer helper hash mismatch')
        report['status'] = 'RUNNING'
        save()
        trial_args = argparse.Namespace(iterations=1000, warmup=100, timeout=args.timeout)
        for ppn in args.ppn:
            for benchmark in args.benchmarks:
                for size in args.sizes:
                    for mode in (('rc', 'rm') if size == 8 else ('rm',)):
                        row = osu.one_trial(out, commands, evidence, benchmark, mode,
                                            ppn, 'validation', 0, trial_args,
                                            message_bytes=size,
                                            segment_size=4096 if mode == 'rm' else None)
                        report['cases'].append(row)
                        save()
                        if not row['passed']:
                            raise RuntimeError('%s: %s' %
                                               (row['name'], row.get('error', row.get('cleanup_error'))))
                        print('%s PPN=%d MODE=%s SIZE=%d DATA=PASS EXIT=0' %
                              (benchmark, ppn, mode.upper(), size), flush=True)
        report['status'] = 'SELECTED_STANDARD_OSU_NORMAL_PATH_PASS'
        save()
        print('正常路径验证=PASS；未取证每片WR数量/硬件极限，C可靠性仍待确认', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        print('B_BOUNDARY_STOPPED=' + report['error'][:500], file=sys.stderr, flush=True)
        raise SystemExit(1)
    finally:
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
