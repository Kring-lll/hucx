#!/usr/bin/env python3
"""Node89 only: pinned two-node AM/iface candidate checks, no system changes.

Diagnostics and timings are separate. Every failure remains in results.json;
passing these normal cases does not prove hardware loss recovery or PPN20.
"""
import argparse
import json
import math
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
import tempfile

import accept as base
from inspect_run import inspect


def remote(script, *args):
    # SSH passes its command through the remote shell. Quote each argument,
    # including the Python inspection program; never join it unescaped.
    command = 'bash -s -- ' + shlex.join([str(arg) for arg in args])
    result = subprocess.run(['ssh', base.PEER, command], input=script,
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('SSH失败：' + result.stderr[-1200:] + result.stdout[-1200:])
    return result.stdout


def configured(cmd, env, values):
    """Replace inline overrides as well as exporting the effective environment."""
    result = []
    index = 0
    while index < len(cmd):
        item = cmd[index]
        if item == '-x' and index + 1 < len(cmd):
            key = cmd[index + 1].split('=', 1)[0]
            if key in values:
                index += 2
                continue
        key, sep, value = item.partition('=')
        if sep and key in values:
            item = key + '=' + values[key]
        result.append(item)
        index += 1
    env = dict(env)
    env.update(values)
    env.pop('LD_DEBUG', None)
    env['OMPI_ALLOW_RUN_AS_ROOT'] = '1'
    env['OMPI_ALLOW_RUN_AS_ROOT_CONFIRM'] = '1'
    mp = next(i for i, value in enumerate(result)
              if Path(value).name in ('mpirun', 'mpiexec'))
    result[mp + 1:mp + 1] = [item for key in values for item in ('-x', key)]
    return result, env


def values(mode, debug=False):
    kind = 'RC' if mode == 'rc' else 'RM'
    prefix = 'UCX_' + kind + '_URMA_'
    result = {prefix + key: value for key, value in {
        'FC_ENABLE': 'y', 'FC_WND_SIZE': '8', 'TX_DEPTH': '8',
        'RX_DEPTH': '64', 'FC_SOFT_THRESH': '0.5', 'FC_HARD_THRESH': '0.25'
    }.items()}
    # With rm/um:aux only, UCP can also use RM for the local rank. Five ranks
    # therefore need a bound including self, not just four remote ranks.
    result.update(UCX_RM_URMA_LINK_MODE='iface_v2' if mode == 'iface' else 'legacy_ep',
                  UCX_RM_URMA_MAX_PEERS='5', UCX_RNDV_THRESH='inf',
                  UCX_ZCOPY_THRESH='inf', UCX_LOG_LEVEL='debug' if debug else 'warn')
    return result


def latency(text):
    found = re.findall(r'^\s*8\s+([0-9]+(?:\.[0-9]+)?)\s+Pass\s*$', text, re.M)
    if len(found) != 1:
        raise RuntimeError('缺少唯一的8B数据校验Pass行')
    value = float(found[0])
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError('时延数值无效')
    return value


def trace_counts(text):
    counts = {
        'iface_address_exports': text.count('RM iface address export'),
        'legacy_ep_address_exports': text.count('RM legacy EP address export'),
        'legacy_connect_calls': text.count('RM legacy connect_to_ep'),
        'iface_connect_calls': text.count('RM iface connect'),
        'fc_requests_posted': text.count('RM iface control post kind=1'),
        'fc_grants_posted': text.count('RM iface control post kind=2')
    }
    if not re.search(r'tag\(rm_urma/', text):
        raise RuntimeError('缺少RM tag数据lane选择证据')
    ready = re.findall(r'RM iface_v2 ready .*? R=(\d+) K=(\d+) W=(\d+)', text)
    if len(ready) < 2 or any(tuple(map(int, row)) != (64, 5, 8) for row in ready):
        raise RuntimeError('两端实际iface模式/RX预算证据缺失或不符')
    if (counts['iface_address_exports'] < 2 or counts['iface_connect_calls'] < 2 or
            counts['legacy_ep_address_exports'] or counts['legacy_connect_calls'] or
            not counts['fc_requests_posted'] or not counts['fc_grants_posted']):
        raise RuntimeError('新地址/连接/独立流控路径证据不完整')
    return counts


def main():
    parser = argparse.ArgumentParser(description='RM iface候选双节点验证，保留全部结果')
    parser.add_argument('case', type=Path)
    parser.add_argument('--baseline', type=Path, default=base.LAB / 'osu_minimal8_ff_14ns8')
    parser.add_argument('--experiment-root', type=Path, default=base.LAB)
    parser.add_argument('--no-deploy', action='store_true')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--rounds', type=int, default=2000)
    args = parser.parse_args()
    if args.repeats < 1 or args.rounds < 1:
        parser.error('repeats和rounds必须大于0')
    case = args.case.resolve()
    experiment_root = args.experiment_root.resolve()
    if case.parent != experiment_root:
        raise RuntimeError('必须使用实验根下的隔离CASE')
    install = case / 'install'
    manifest = json.loads((case / 'build_manifest.json').read_text())
    module = install / 'lib/ucx/libuct_ub.so.0.0.0'
    if base.digest(module) != manifest['module_sha256']:
        raise RuntimeError('构建后模块发生变化')
    root = Path(__file__).resolve().parents[3]
    sources = json.loads((root / 'contrib/rm_ctp/r1/candidate_manifest.json').read_text())
    for name, expected in sources['files'].items():
        if base.digest(root / name) != expected:
            raise RuntimeError('验收源码不符：' + name)
    out = Path(tempfile.mkdtemp(prefix='iface_accept_', dir=str(case)))
    report = {'source_commit': manifest['source_commit'],
              'module_sha256': manifest['module_sha256'], 'cases': [],
              'functional_acceptance': False, 'performance_accepted': False,
              'ctp_contract_closed': False, 'scope': '两节点AM正常功能；非PPN20/丢包恢复证明'}

    def save():
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    def execute(name, command, environment, check):
        try:
            result = base.run(command, environment, out / (name + '.log'), timeout=120)
            text = Path(result['log']).read_text(errors='replace')
            result.update(check(text))
            result.update(name=name, passed=True)
            if 'latency_us' in result:
                print(name + ' 通过 8B=%.3fus 校验Pass 退出0' % result['latency_us'], flush=True)
            elif 'resources' in result:
                peaks = list(result['resources']['0']['jetty_peak_by_context'].values())
                print(name + ' 通过 root每context的Jetty峰值=' + str(peaks) +
                      ' 退出存活=0', flush=True)
            else:
                print(name + ' 通过', flush=True)
        except Exception as exc:
            result = {'name': name, 'passed': False, 'error': str(exc),
                      'log': str(out / (name + '.log'))}
            print(name + ' 失败：' + str(exc).splitlines()[0], flush=True)
        report['cases'].append(result)
        save()
        return result

    print('结果目录=' + str(out), flush=True)
    save()
    if not args.no_deploy:
        target = experiment_root / 'upload' / (case.name + '.install.tar.gz')
        remote('set -eu\ntest ! -e "$1"\nmkdir -p "$2"\n', str(case), str(target.parent))
        subprocess.run(['scp', '-q', str(case / 'install.tar.gz'), base.PEER + ':' + str(target)], check=True)
        remote('set -eu\nmkdir "$1"\ntar -xzf "$2" -C "$1"\n', str(case), str(target))
    peer = remote('set -eu\nsha256sum "$1"\nldd "$1"\n', str(module))
    if peer.split()[0] != manifest['module_sha256'] or 'not found' in peer:
        raise RuntimeError('对端模块身份或依赖不符')
    print('双端模块一致=通过', flush=True)
    specs = {kind: base.saved_spec(kind, install, args.baseline) for kind in ('rm', 'rc')}
    command, environment, index = specs['rm']
    diagnostic, diag_env = configured(command, environment, values('iface', True))
    diag_env['LD_DEBUG'] = 'libs'

    def check_loader(text):
        if str(install / 'lib/ucx/libuct_ub.so') not in text:
            raise RuntimeError('缺少候选模块加载证据')
        latency(text)
        counts = trace_counts(text)
        return {'validation': True, 'mode': 'iface_v2',
                'transport_api_counts_not_network_packets': counts}

    first = execute('新模式加载与8B校验', diagnostic, diag_env, check_loader)
    if not first['passed']:
        raise RuntimeError('新模式初始门禁失败；停止后续大用例，保留原始日志')
    samples = {mode: [] for mode in ('legacy', 'iface', 'rc')}
    for repeat in range(args.repeats):
        order = ('legacy', 'iface', 'rc') if repeat % 2 == 0 else ('rc', 'iface', 'legacy')
        for mode in order:
            cmd, env, _ = specs['rc' if mode == 'rc' else 'rm']
            cmd, env = configured(cmd, env, values(mode))
            result = execute(mode + '_8B_' + str(repeat), cmd, env,
                             lambda text: {'latency_us': latency(text), 'validation': True})
            if result['passed']:
                samples[mode].append(result['latency_us'])
    mpi = next(Path(item) for item in command if Path(item).name in ('mpirun', 'mpiexec'))
    binaries = {}
    for name, source in (('many', 'many_peer.c'), ('burst', 'fc_burst.c')):
        binary = case / ('iface_' + name)
        subprocess.run([str(mpi.with_name('mpicc')), '-O2', '-Wall', '-Wextra',
                        str(Path(__file__).with_name(source)), '-o', str(binary)], check=True)
        binaries[name] = binary
    wrapper = case / 'iface_observed.sh'
    wrapper.write_text('#!/bin/bash\nset -eu\n'
        'if [ "$OMPI_COMM_WORLD_RANK" = 0 ]; then\n'
        'observer=' + shlex.quote(str(base.LAB / 'observer_build_d886fc3/liburma_resource_observer.so')) + '\n'
        'else\nobserver=' + shlex.quote(str(base.LAB / 'observer_runtime_d886fc3/liburma_resource_observer.so')) + '\nfi\n'
        'test -f "$observer"\nexport LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$observer"\n'
        'exec "$@"\n')
    wrapper.chmod(0o755)
    subprocess.run(['scp', '-q', *map(str, binaries.values()), str(wrapper),
                    base.PEER + ':' + str(case) + '/'], check=True)
    subprocess.run(['scp', '-q', str(Path(__file__).with_name('inspect_run.py')),
                    base.PEER + ':' + str(case) + '/'], check=True)
    for peers in (1, 2, 4):
        for mode in ('iface', 'legacy', 'rc'):
            for pattern in ('many', 'burst'):
                name = '%s_%s_peers%d' % (mode, pattern, peers)
                directory = out / name
                directory.mkdir()
                remote('set -eu\nmkdir -p "$1"\n', str(directory))
                cmd, env, idx = specs['rc' if mode == 'rc' else 'rm']
                size = 64 if peers < 4 else 1024
                # Reuse the pinned launcher/placement transform. The wrapper
                # takes the binary explicitly, leaving the two MPI args intact.
                cmd = base.many_command(cmd, idx, wrapper, peers, size)
                where = cmd.index(str(wrapper))
                cmd[where + 1:where + 1] = [str(binaries[pattern])]
                cmd[-1] = str(args.rounds)
                setup = values(mode, debug=mode == 'iface')
                setup.update(R1_MAP_DIR=str(directory), RM_RESOURCE_OBSERVER_DIR=str(directory),
                             R2_FC_KIND='RC' if mode == 'rc' else 'RM')
                cmd, env = configured(cmd, env, setup)

                def check_many(text, p=peers, m=mode, pat=pattern, d=directory):
                    marker = 'R1_MANY_PEER_PASS' if pat == 'many' else 'R2_FC_ONEWAY_PASS'
                    if marker + ' peers=' + str(p) not in text:
                        raise RuntimeError('缺少对应数据校验标记')
                    resource_mode = 2 if m == 'rc' else 1
                    local = inspect(str(d), resource_mode, str(install))
                    inspector = 'import sys,json;sys.path.insert(0,sys.argv[1]);from inspect_run import inspect;print(json.dumps(inspect(sys.argv[2],int(sys.argv[3]),sys.argv[4])))'
                    # Source is only on node89; send the unchanged inspector
                    # separately, rather than assuming the peer has the tree.
                    remote_result = remote('set -eu\npython3 -c "$1" "$2" "$3" "$4" "$5"\n',
                        inspector, str(case), str(d), str(resource_mode), str(install))
                    other = json.loads(remote_result)
                    if set(local) != {'0'} or set(other) != {str(i) for i in range(1, p + 1)}:
                        raise RuntimeError('rank位置或观察器数量不符')
                    result = {'validation': True, 'resources': {**local, **other}}
                    if m == 'iface':
                        result['transport_api_counts_not_network_packets'] = trace_counts(text)
                    return result

                execute(name, cmd, env, check_many)
    medians = {mode: statistics.median(rows) for mode, rows in samples.items()
               if len(rows) == args.repeats}
    report['median_8B_us'] = medians
    report['functional_acceptance'] = all(item['passed'] for item in report['cases'])
    report['performance_note'] = '少量功能回归样本，完整保留波动；尚未约定或完成性能验收'
    save()
    print('8B中位数(us)=' + json.dumps(medians, ensure_ascii=False), flush=True)
    if not report['functional_acceptance']:
        print('存在失败，本轮不作优势或无回退结论；完整样本已保留', flush=True)
    if report['functional_acceptance'] and 'rc' in medians and 'iface' in medians:
        delta = (medians['rc'] - medians['iface']) / medians['rc'] * 100
        print('新RM相对RC时延变化：降低为正，升高为负；%+.2f%%，仅本轮样本' % delta, flush=True)
    print('功能矩阵=' + ('通过' if report['functional_acceptance'] else '未通过'), flush=True)
    print('性能验收=待完成；PPN20/CTP故障语义未验收', flush=True)
    print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
    return 0 if report['functional_acceptance'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('IFACE_ACCEPT_FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
