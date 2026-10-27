#!/usr/bin/env python3
"""A8/A9 normal-path node runner; unchanged, pinned HUCX product binaries.

Run on node89 as root. This tool never pushes Git or changes drivers/devices.
All failure samples stay in the report. Timings exclude observer/debug runs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from support import (A8_LEVELS, DEFAULT_CASE, LAB, MINIMAL, MINIMAL_MODULE,
                     PRE_IFACE, PRE_IFACE_COMMIT, PRE_IFACE_MODULE,
                     PRODUCT_COMMIT, PRODUCT_MODULE, budget, check_maps,
                     digest, environment, launcher, levels, resource_counts,
                     saved_spec, statistics_for, summarize)

PEER = 'root@9.88.43.97'
HOSTS = ('9.88.43.89', '9.88.43.97')
ROOT = Path(__file__).resolve().parent
REGRESSION = ROOT / 'regression' if (ROOT / 'regression').is_dir() else ROOT.parent / 'r1'


def write_json(path, value):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def ssh(script, *args, timeout=30):
    command = 'bash -s -- ' + shlex.join([str(value) for value in args])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                             PEER, command], input=script, text=True,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('SSH失败：' + result.stderr[-1000:] + result.stdout[-1000:])
    return result.stdout


def copy_remote(paths, target, timeout=90):
    subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8'] +
                   [str(path) for path in paths] + [PEER + ':' + str(target)],
                   check=True, timeout=timeout)


def run_logged(command, env, log, timeout):
    # Own the launcher group, not every MPI job on either machine.
    write_json(log.with_suffix('.command.json'), {'command': command,
               'environment': {k: v for k, v in env.items()
                               if k.startswith(('UCX_', 'LD_', 'OMPI_'))},
               'timeout_seconds': timeout})
    started = time.monotonic()
    with log.open('w') as stream:
        process = subprocess.Popen(command, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if code != 0:
        raise RuntimeError('EXIT=%d LOG=%s' % (code, log))
    return {'exit': code, 'launcher_wall_s': time.monotonic() - started}


INVENTORY = r'''
import hashlib, json, os, resource, socket
from pathlib import Path
if int(Path('/proc/self/stat').read_text().split()[0]) != os.getpid():
    raise RuntimeError('PID/proc identity mismatch; scoped cleanup cannot be verified')
Path('/proc/self/environ').read_bytes()
allowed = sorted(os.sched_getaffinity(0))
cores = {}
for cpu in allowed:
    folder = Path('/sys/devices/system/cpu/cpu%d/topology' % cpu)
    try:
        key = ((folder/'physical_package_id').read_text().strip(),
               (folder/'core_id').read_text().strip())
    except OSError:
        key = ('logical', str(cpu))
    cores.setdefault(':'.join(key), []).append(cpu)
mem = dict((k, int(v.split()[0])) for k, v in
           (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines()))
print(json.dumps({'hostname': socket.gethostname(), 'allowed_cpus': allowed,
    'physical_core_groups': cores, 'available_cores': len(cores),
    'mem_available_kib': mem['MemAvailable'],
    'nofile_soft': resource.getrlimit(resource.RLIMIT_NOFILE)[0],
    'nproc_soft': resource.getrlimit(resource.RLIMIT_NPROC)[0],
    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))
'''


def inventory():
    local = subprocess.check_output([sys.executable, '-c', INVENTORY], text=True)
    remote = ssh('exec python3 -c "$1"\n', INVENTORY)
    rows = [json.loads(local), json.loads(remote)]
    if rows[0]['hostname'] == rows[1]['hostname']:
        raise RuntimeError('两节点hostname相同，无法验证rank放置；停止猜测')
    return rows


def module_check(install, expected):
    module = install / 'lib/ucx/libuct_ub.so.0.0.0'
    if digest(module) != expected:
        raise RuntimeError('本端模块不符：' + str(module))
    text = ssh('set -eu\nsha256sum "$1"\nldd "$1"\n', module)
    if text.split()[0] != expected or 'not found' in text:
        raise RuntimeError('对端模块/依赖不符：' + str(module))
    local_ldd = subprocess.check_output(['ldd', str(module)], text=True)
    if 'not found' in local_ldd:
        raise RuntimeError('本端依赖缺失')
    libraries = {}
    for name in ('ucp', 'uct', 'ucs', 'ucm'):
        path = install / 'lib' / ('lib' + name + '.so.0')
        local_hash = digest(path)
        peer_hash = ssh('sha256sum \"$1\"\n', path).split()[0]
        if local_hash != peer_hash:
            raise RuntimeError('两端UCX基础库不一致：' + str(path))
        libraries[name] = local_hash
    return {'install': str(install), 'module_sha256': expected,
            'base_libraries_sha256': libraries,
            'local_ldd': local_ldd, 'peer_sha256_and_ldd': text}


def parsed_config(install, env, directory, mode):
    code = ('import os,subprocess,json\n'
            'env={k:v for k,v in os.environ.items() if not k.startswith("UCX_")}\n'
            'env.update(json.loads(' + repr(json.dumps(env)) + '))\n'
            'p=subprocess.run(' + repr([str(install / 'bin/ucx_info'), '-c']) +
            ',env=env,text=True,capture_output=True,timeout=35)\n'
            'print(p.stdout);print(p.stderr)\nraise SystemExit(p.returncode)\n')
    local = subprocess.check_output([sys.executable, '-c', code], text=True, timeout=45)
    other = ssh('exec python3 -c "$1"\n', code, timeout=45)
    for name, text in (('node89', local), ('node97', other)):
        (directory / (name + '_ucx_config.txt')).write_text(text)
        parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
        required = {key: value for key, value in env.items()
                    if key.startswith('UCX_' + ('RC' if mode == 'rc' else 'RM') + '_URMA_')}
        for key, expected in required.items():
            actual = str(parsed.get(key, ''))
            same = (float(actual) == float(expected)) if key.endswith('_THRESH') and actual else actual.lower() == expected.lower()
            if not same:
                raise RuntimeError('解析后配置不符：%s %s=%s expected=%s' %
                                   (name, key, parsed.get(key), expected))
    return True


def old8b_control(out, baseline):
    rows = []
    for kind in ('rc', 'rm'):
        command, saved = saved_spec(baseline / (kind + '_run.json'), MINIMAL)
        env = {k: v for k, v in os.environ.items() if not k.startswith('UCX_')}
        env.update(saved)
        env.pop('LD_DEBUG', None)
        env.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
        log = out / ('old8b_' + kind + '.log')
        run_logged(command, env, log, 75)
        found = re.findall(r'^\s*8\s+(\d+(?:\.\d+)?)\s+Pass\s*$',
                           log.read_text(errors='replace'), re.M)
        if len(found) != 1:
            raise RuntimeError('旧8B校验行缺失：' + str(log))
        rows.append({'kind': kind, 'latency_us': float(found[0]), 'passed': True})
    print('旧版8B门禁=通过 RC=%.3fus RM=%.3fus（单轮）' %
          (rows[0]['latency_us'], rows[1]['latency_us']), flush=True)
    return rows


def regressions(case, out):
    # Reuse the tested 2a8e source/manifest for its original 28-case matrix.
    # New focused probes are shipped verbatim, pinned by toolkit_manifest.json.
    original_case = case
    case = LAB / (out.name + '_regressions')
    case.mkdir()
    shutil.copytree(original_case / 'install', case / 'install', symlinks=True)
    shutil.copy2(original_case / 'build_manifest.json', case / 'build_manifest.json')
    (case / 'source').symlink_to(original_case / 'source', target_is_directory=True)
    ssh('set -eu\ntest ! -e \"$2\"\nmkdir \"$2\"\ncp -a \"$1/install\" \"$2/install\"\n', original_case, case, timeout=120)
    write_json(out / 'regression_case.json', {'isolated_case': str(case),
               'immutable_source_reference': str(original_case / 'source')})
    source = case / 'source/contrib/rm_ctp/r1'
    stages = [
        ('28项正常矩阵', [str(source / 'accept_iface.py'), str(case), '--no-deploy']),
        ('四EP无反向业务EP', [str(REGRESSION / 'a7_iface_four_ep_20261026.py'), str(case)]),
        ('160EP生命周期', [str(REGRESSION / 'a7_iface_lifecycle_20261026.py'), str(case)]),
        ('MPI地址阶段', [str(REGRESSION / 'a5_mpi_phase_20261026.py'), str(case)])]
    results = []
    for index, (name, arguments) in enumerate(stages):
        log = out / ('regression%d.log' % index)
        env = dict(os.environ)
        env.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1',
                   PYTHONDONTWRITEBYTECODE='1')
        result = run_logged([sys.executable, '-u'] + arguments, env, log, 2400)
        text = log.read_text(errors='replace')
        if index == 0 and '功能矩阵=通过' not in text:
            raise RuntimeError('28项矩阵未通过，停止规模测试：' + str(log))
        results.append({'name': name, 'passed': True, 'log': str(log), **result})
        if index == 0:
            medians = re.findall(r'8B中位数\(us\)=(.*)', text)
            if medians:
                print('既有OSU 8B中位数(us)=' + medians[-1] + '；完整矩阵样本已保留', flush=True)
        print('回归=' + name + ' 通过 LOG=' + str(log), flush=True)
    return results


def fetch_trial(run, ranks, probe, hostname):
    # A fresh local directory receives only artifacts from this exact peer run.
    names = []
    for rank in ranks:
        names.extend(['rank%d.json' % rank, 'pid_rank%d.json' % rank])
        names.extend('rank%d_%s.maps' % (rank, phase) for phase in
                     ('after_init', 'after_first', 'after_steady'))
    if probe:
        data = ssh('set -eu\npython3 -c "$2" "$1"\n', run,
                   'import pathlib,sys,json;print(json.dumps([p.name for p in pathlib.Path(sys.argv[1]).glob("rank*_pid*.*")]))')
        names.extend(json.loads(data))
    if any('/' in name or name in ('', '.', '..') for name in names):
        raise RuntimeError('对端artifact名称越界')
    # scp supports multiple explicit source arguments without wildcards.
    source = [PEER + ':' + str(run / name) for name in sorted(set(names))]
    subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8'] +
                   source + [str(run) + '/'], check=True, timeout=120)
    return hostname


def trial(case, out, base_commands, inventories, mode, ppn, repeat, probe, timeout):
    name = '%s_p%d_r%d_%s' % (mode, ppn, repeat, 'resources' if probe else 'timing')
    run = out / name
    run.mkdir()
    ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', run)
    config_budget = budget(ppn)
    # Conservative lower-bound staging check, not a claim about device memory.
    current_inventory = inventory()
    write_json(run / 'live_inventory.json', current_inventory)
    for index, row in enumerate(current_inventory):
        if row['boot_id'] != inventories[index]['boot_id']:
            raise RuntimeError('运行中节点发生重启，停止本轮对比')
        fds = row['nofile_soft']
        if (fds != -1 and fds < 2 * ppn + 64) or row['available_cores'] < ppn:
            raise RuntimeError('当前核数或fd上限不足，停止：' + row['hostname'])
        if row['mem_available_kib'] < ppn * config_budget['rx_depth'] * 16 + 512 * 1024:
            raise RuntimeError('接收池保守预留内存不足；不自动提高系统限制')
    install = PRE_IFACE if mode == 'old_rm' else case / 'install'
    command, saved = base_commands['rc' if mode == 'rc' else 'rm']
    # saved RM/RC arrays were loaded against the selected prefix below.
    if mode == 'old_rm':
        command = [value.replace(str(case / 'install'), str(install)) for value in command]
        saved = {key: value.replace(str(case / 'install'), str(install)) for key, value in saved.items()}
    env = environment(saved, mode, install, config_budget)
    parsed_config(install, env, run, mode)
    rounds = max(10, 2000 // ppn)
    hostname = [row['hostname'] for row in inventories]
    nonce = uuid.uuid4().hex
    # Each unique process set is identified by a cryptographic random run token.
    config = {'nonce': nonce, 'run': str(run), 'environment': env,
              'resource_probe': probe, 'binary': str(out / 'mpi_scale'),
              'timeout': timeout, 'budget': config_budget,
              'observers': {hostname[0]: str(LAB / 'observer_build_d886fc3/liburma_resource_observer.so'),
                            hostname[1]: str(LAB / 'observer_runtime_d886fc3/liburma_resource_observer.so')},
              'arguments': [str(ppn), HOSTS[0], str(21000 + int(nonce[:4], 16) % 30000),
                            str(run), str(rounds), str(timeout - 15), *hostname]}
    config_path = run / 'rank_config.json'
    write_json(config_path, config)
    copy_remote([config_path], str(run) + '/')
    command = launcher(command, ppn, HOSTS, out / 'rank_exec.py', config_path)
    launch_env = {key: value for key, value in os.environ.items()
                  if not key.startswith('UCX_') and key not in ('LD_PRELOAD', 'LD_DEBUG')}
    # Preserve the known HMPI launcher installation, including its recorded env.
    launch_env.update(saved)
    launch_env.pop('LD_DEBUG', None)
    launch_env.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    result = {'name': name, 'mode': mode, 'ppn': ppn, 'repeat': repeat,
              'resource_probe': probe, 'configuration': config, 'passed': False}
    write_json(run / 'trial.json', result)
    failure = None
    try:
        result.update(run_logged(command, launch_env, run / 'launcher.log', timeout + 20))
        fetch_trial(run, range(ppn, 2 * ppn), probe, hostname[1])
        rows = [json.loads((run / ('rank%d.json' % rank)).read_text()) for rank in range(2 * ppn)]
        for row in rows:
            rank = row['rank']
            if row['hostname'] != hostname[rank // ppn]:
                raise RuntimeError('实际rank放置不符')
            pid = json.loads((run / ('pid_rank%d.json' % rank)).read_text())
            if pid['nonce'] != nonce or not pid['cpu_affinity']:
                raise RuntimeError('rank身份/绑核证据缺失')
            if not set(pid['cpu_affinity']) <= set(inventories[rank // ppn]['allowed_cpus']):
                raise RuntimeError('rank绑核不在可用范围')
            for phase in ('after_init', 'after_first', 'after_steady'):
                check_maps((run / ('rank%d_%s.maps' % (rank, phase))).read_text(), install)
        for host_index in range(2):
            affinities = [set(json.loads((run / ('pid_rank%d.json' % rank)).read_text())['cpu_affinity'])
                          for rank in range(host_index * ppn, (host_index + 1) * ppn)]
            if any(left & right for i, left in enumerate(affinities) for right in affinities[i + 1:]):
                raise RuntimeError('rank CPU绑定重叠，不接受同规模公平比较')
        text = (run / 'launcher.log').read_text(errors='replace')
        if len(re.findall(r'^A89_RANK_PASS rank=', text, re.M)) != 2 * ppn:
            raise RuntimeError('rank退出标记缺失')
        if re.search(r'UCX\s+(ERROR|FATAL)|A89_DATA_OR_RUNTIME_ERROR|Caught signal', text):
            raise RuntimeError('日志包含终态错误')
        result['metrics'] = summarize(rows, ppn)
        if probe:
            result['resources'] = resource_counts(run, range(2 * ppn),
                                                  2 if mode == 'rc' else 1,
                                                  str(install), config_budget['tx_depth'])
        result['passed'] = True
    except BaseException as exc:
        failure = exc
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        # Even successful jobs must have no token-owned application left behind.
        try:
            local = subprocess.check_output([sys.executable, str(out / 'rank_exec.py'),
                                             str(config_path), '--cleanup'], text=True, timeout=15)
            peer = ssh('exec python3 "$1" "$2" --cleanup\n', out / 'rank_exec.py', config_path)
            result['cleanup'] = {'local': json.loads(local), 'peer': json.loads(peer)}
            if any(value['terminated_own_pids'] for value in result['cleanup'].values()):
                result['passed'] = False
                result['error'] = result.get('error', '正常退出后仍有本轮进程残留')
        except Exception as exc:
            result['passed'] = False
            result['cleanup_error'] = str(exc)
        write_json(run / 'trial.json', result)
    if failure is not None and isinstance(failure, (KeyboardInterrupt, SystemExit)):
        raise failure
    return result


def comparison(rows, ppn):
    result = {'ppn': ppn, 'modes': {}}
    for mode in ('iface', 'old_rm', 'rc'):
        timings = [row for row in rows if row['mode'] == mode and not row['resource_probe']]
        probe = [row for row in rows if row['mode'] == mode and row['resource_probe']]
        result['modes'][mode] = {
            'metrics': {key: statistics_for([row['metrics'][key] for row in timings])
                        for key in timings[0]['metrics']},
            'resource_trial': probe[0]['name'],
            'resource_memory_mib': {key: probe[0]['metrics'][key] for key in
                                    ('init_rss_sum_mib', 'first_rss_sum_mib', 'steady_rss_sum_mib')},
            'jetty_peaks_per_rank': {rank: list(value['peaks_per_context'].values())
                                     for rank, value in probe[0]['resources'].items()}}
    new = result['modes']['iface']['metrics']
    for baseline in ('old_rm', 'rc'):
        old = result['modes'][baseline]['metrics']
        result['iface_vs_' + baseline] = {
            key: (old[key]['median'] - new[key]['median']) / old[key]['median'] * 100
            for key in ('init_max_ms', 'first_all_peers_max_ms', 'exchange8_max_rank_us',
                        'steady_rss_sum_mib')}
    return result


def validate_toolkit():
    manifest = json.loads((ROOT / 'toolkit_manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if digest(ROOT / name) != expected:
            raise RuntimeError('工具源码校验失败：' + name)
    for name, expected in manifest['regression_files'].items():
        if digest(REGRESSION / name) != expected:
            raise RuntimeError('回归工具源码不符：' + name)
    return manifest


def render_summary(out, report):
    lines = ['# A8/A9 本轮实测摘要', '',
             '产品：`' + PRODUCT_COMMIT + '`；C重传延期。全部样本与失败以results.json为准。', '',
             '8B交换耗时是Sendrecv交换，不能与OSU单向latency混用；变化正负均保留。', '',
             '| PPN | 模式 | Init最大ms中位数 | 首轮全对端ms中位数 | 8B交换us中位数[最小,最大] | 1024B有效MB/s中位数 | 稳定RSS合计MiB（资源轮） |',
             '|---|---|---|---|---|---|---|']
    for group in report['comparisons']:
        for mode, row in group['modes'].items():
            m = row['metrics']
            e = m['exchange8_max_rank_us']
            lines.append('| %d | %s | %.3f | %.3f | %.3f [%.3f, %.3f] | %.2f | %.2f |' % (
                group['ppn'], mode, m['init_max_ms']['median'],
                m['first_all_peers_max_ms']['median'], e['median'], e['minimum'], e['maximum'],
                m['burst1024_payload_MBps']['median'], row['resource_memory_mib']['steady_rss_sum_mib']))
    lines.extend(['', 'A9仅RM（无同规模RC基线）：', ''])
    for row in report.get('a9_levels', []):
        metrics = row['timings']
        lines.append('- PPN%d：8B交换 %.3fus；1024B有效吞吐 %.2fMB/s；资源详见%s。' % (
            row['ppn'], metrics['exchange8_max_rank_us']['median'],
            metrics['burst1024_payload_MBps']['median'], row['resource_trial']))
    lines.extend(['', 'A8正常功能门禁：' + str(report['a8_functional']),
                  'A9正常功能门禁：' + str(report['a9_functional']),
                  '性能结论：' + report['performance_acceptance'],
                  'A整体：未自动验收；源数据未回收前不写完成。'])
    if 'error' in report:
        lines.extend(['', '**本轮停止：** ' + report['error']])
    (out / 'summary.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description='A8/A9两节点同规模对照与RM正常扩展；完整保存失败')
    parser.add_argument('case', type=Path, nargs='?', default=DEFAULT_CASE)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--target-ppn', type=int, default=192)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--phase', choices=('all', 'a8'), default='all')
    parser.add_argument('--skip-regressions', action='store_true',
                        help='仅诊断时使用；完整验收永不因跳过回归变成通过')
    parser.add_argument('--plan', action='store_true', help='只校验工具并打印接收预算，不SSH/不启动MPI')
    args = parser.parse_args()
    toolkit = validate_toolkit()
    if args.repeats < 3 or args.timeout < 60 or not 20 <= args.target_ppn <= 192:
        parser.error('repeats至少3，timeout至少60，target-ppn在20..192内')
    if args.plan:
        print(json.dumps({'product_commit': PRODUCT_COMMIT,
                          'a8': [budget(p) for p in A8_LEVELS],
                          'a9_requested': [budget(p) for p in levels(args.target_ppn)],
                          'node_tests': 'NOT_RUN'}, ensure_ascii=False, indent=2))
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise RuntimeError('本工具应由root在node89执行')
    case = args.case.resolve()
    if case != DEFAULT_CASE:
        raise RuntimeError('本包固定已测CASE；不要用未验证的库替代')
    out = Path(tempfile.mkdtemp(prefix='a89_', dir=str(case)))
    print('A89_DIR=' + str(out), flush=True)
    report = {'toolkit': toolkit, 'toolkit_tree_base': '751c3cc801192ee2403bbd93e8ac026bf38c7945',
              'product_commit': PRODUCT_COMMIT, 'module_sha256': PRODUCT_MODULE,
              'trials': [], 'comparisons': [], 'a8_functional': False,
              'a9_functional': False, 'full_acceptance': False,
              'performance_acceptance': '待导师结合全部样本审查，未自设回退阈值',
              'reliability_C': 'DEFERRED', 'node_run_started': time.strftime('%F %T %Z')}
    def save():
        write_json(out / 'results.json', report)
        render_summary(out, report)
    save()
    try:
        build = json.loads((case / 'build_manifest.json').read_text())
        if build['source_commit'] != PRODUCT_COMMIT or build['module_sha256'] != PRODUCT_MODULE:
            raise RuntimeError('CASE构建身份不符')
        report['installations'] = {
            'candidate': module_check(case / 'install', PRODUCT_MODULE),
            'old_rm': module_check(PRE_IFACE, PRE_IFACE_MODULE),
            'old_minimal_control': module_check(MINIMAL, MINIMAL_MODULE)}
        old_build = json.loads((PRE_IFACE.parent / 'build_manifest.json').read_text())
        if old_build['source_commit'] != PRE_IFACE_COMMIT:
            raise RuntimeError('改造前RM构建来源不符')
        report['inventory'] = inventory()
        capacity = min(row['available_cores'] for row in report['inventory'])
        report['available_ppn'] = capacity
        if capacity < 20:
            raise RuntimeError('实际可用物理核不足PPN20，不能完成导师要求的A8')
        save()
        print('两端可用物理核=' + str([row['available_cores'] for row in report['inventory']]) +
              '；目标PPN=' + str(min(capacity, args.target_ppn)), flush=True)
        baseline = LAB / 'osu_minimal8_ff_14ns8'
        # Validate every saved command before spending time on regressions.
        # This includes an application-side /usr/bin/env after mpirun options.
        commands = {kind: saved_spec(baseline / (kind + '_run.json'), case / 'install')
                    for kind in ('rm', 'rc')}
        previews = {kind: launcher(command, 1, HOSTS, out / 'rank_exec.py',
                                    out / 'preview_config.json')
                    for kind, (command, _) in commands.items()}
        write_json(out / 'launcher_preflight.json', {
            'saved_record_sha256': {kind: digest(baseline / (kind + '_run.json'))
                                     for kind in ('rm', 'rc')},
            'preview_argv_not_executed': previews})
        print('启动命令解析门禁=通过（保留MPI/MCA，应用env写入rank配置）', flush=True)
        report['old8b_control'] = old8b_control(out, baseline)
        save()
        if not args.skip_regressions:
            report['regressions'] = regressions(case, out)
            save()
        else:
            report['regressions_skipped'] = True
        mpi = next(Path(value) for value in commands['rm'][0]
                   if Path(value).name in ('mpirun', 'mpiexec'))
        binary = out / 'mpi_scale'
        compile_command = [str(mpi.with_name('mpicc')), '-std=c99', '-O2', '-g',
                           '-Wall', '-Wextra', '-Werror', str(ROOT / 'mpi_scale.c'), '-o', str(binary)]
        report['compile_command'] = compile_command
        run_logged(compile_command, dict(os.environ), out / 'compile.log', 90)
        (out / 'rank_exec.py').write_bytes((ROOT / 'rank_exec.py').read_bytes())
        ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        copy_remote([binary, out / 'rank_exec.py'], str(out) + '/')
        if ssh('sha256sum "$1"\n', binary).split()[0] != digest(binary):
            raise RuntimeError('两端测试程序哈希不一致')
        if ssh('sha256sum \"$1\"\n', out / 'rank_exec.py').split()[0] != digest(out / 'rank_exec.py'):
            raise RuntimeError('两端rank包装器哈希不一致')
        report['binary_sha256'] = digest(binary)
        report['saved_launcher_records_sha256'] = {kind: digest(baseline / (kind + '_run.json'))
                                                   for kind in ('rm', 'rc')}
        # Verify both observer paths before running the full benchmark matrix.
        observer0 = LAB / 'observer_build_d886fc3/liburma_resource_observer.so'
        observer1 = LAB / 'observer_runtime_d886fc3/liburma_resource_observer.so'
        report['observer_identity'] = {'local': digest(observer0),
                                       'peer': ssh('sha256sum "$1"\n', observer1).split()[0]}
        save()
        for ppn in A8_LEVELS:
            group = []
            for repeat in range(args.repeats):
                order = ('iface', 'old_rm', 'rc') if repeat % 2 == 0 else ('rc', 'old_rm', 'iface')
                for mode in order:
                    row = trial(case, out, commands, report['inventory'], mode, ppn, repeat,
                                False, args.timeout)
                    report['trials'].append(row)
                    group.append(row)
                    save()
                    if not row['passed']:
                        raise RuntimeError('A8停止于失败样本：' + row['name'] + '；不会继续冲击更高RC规模')
            for mode in ('iface', 'old_rm', 'rc'):
                row = trial(case, out, commands, report['inventory'], mode, ppn, 0,
                            True, args.timeout)
                report['trials'].append(row)
                group.append(row)
                save()
                if not row['passed']:
                    raise RuntimeError('A8资源门禁失败：' + row['name'])
            compared = comparison(group, ppn)
            report['comparisons'].append(compared)
            save()
            latency = {m: round(compared['modes'][m]['metrics']['exchange8_max_rank_us']['median'], 3)
                       for m in ('iface', 'old_rm', 'rc')}
            reduction = compared['iface_vs_rc']['exchange8_max_rank_us']
            print('A8 PPN=%d 三模式数据/退出/绑核/库加载=通过 8B交换us=%s 对RC时延降低=%+.2f%%' %
                  (ppn, json.dumps(latency), reduction), flush=True)
            peaks = {m: sorted({v for values in compared['modes'][m]['jetty_peaks_per_rank'].values()
                                for v in values}) for m in ('iface', 'old_rm', 'rc')}
            memory = {m: round(compared['modes'][m]['resource_memory_mib']['steady_rss_sum_mib'], 2)
                      for m in ('iface', 'old_rm', 'rc')}
            print('资源 PPN=%d 每context Jetty峰值=%s 同阶段RSS合计MiB=%s' %
                  (ppn, json.dumps(peaks), json.dumps(memory)), flush=True)
        report['a8_functional'] = not args.skip_regressions
        save()
        if args.phase == 'all' and not report['a8_functional']:
            raise RuntimeError('跳过回归只属于诊断，禁止进入A9完整验收')
        if args.phase == 'all':
            requested = levels(capacity, args.target_ppn)
            report['a9_plan'] = requested
            if not requested:
                raise RuntimeError('可用规模没有超过PPN20；A9保持未验收')
            for ppn in requested:
                group = []
                for repeat in range(args.repeats):
                    row = trial(case, out, commands, report['inventory'], 'iface', ppn,
                                repeat, False, args.timeout)
                    report['trials'].append(row)
                    group.append(row)
                    save()
                    if not row['passed']:
                        raise RuntimeError('A9在本级失败后停止；不继续扩大：' + row['name'])
                resource = trial(case, out, commands, report['inventory'], 'iface', ppn,
                                 0, True, args.timeout)
                report['trials'].append(resource)
                save()
                if not resource['passed']:
                    raise RuntimeError('A9资源/回收失败，停止扩大：' + resource['name'])
                summary = {key: statistics_for([row['metrics'][key] for row in group])
                           for key in group[0]['metrics']}
                report.setdefault('a9_levels', []).append({'ppn': ppn, 'timings': summary,
                                                         'resource_trial': resource['name']})
                report['highest_rm_passed_ppn'] = ppn
                save()
                print('A9 RM PPN=%d 数据/回收=通过 8B交换=%.3fus 1024B有效载荷=%.2fMB/s；无同规模RC基线' %
                      (ppn, summary['exchange8_max_rank_us']['median'],
                       summary['burst1024_payload_MBps']['median']), flush=True)
            report['a9_functional'] = True
            save()
        print('A8_FUNCTIONAL=' + str(report['a8_functional']) +
              ' A9_FUNCTIONAL=' + str(report['a9_functional']), flush=True)
        print('性能结论=待审阅全部样本；本工具不把功能通过等同于无性能回退或A整体完成', flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
    except BaseException as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        print('A89_STOPPED=' + str(exc), file=sys.stderr, flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
