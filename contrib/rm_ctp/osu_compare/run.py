#!/usr/bin/env python3
"""Original OSU binaries, bounded two-node RM/RC comparisons, no product edits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
A89 = ROOT.parent / 'a89'
sys.path.insert(0, str(A89))
import run_scale as base
from artifacts import fetch
from support import DEFAULT_CASE, LAB, PRODUCT_COMMIT, PRODUCT_MODULE, budget, digest, environment, launcher, saved_spec
from analysis import BENCHMARKS, compare, inspect_jettys, inspect_ranks, parse_output, write_tables

OSU = LAB / 'osu_build_7.5.2_20261020_AOlmOB/install/libexec/osu-micro-benchmarks/mpi'
PML = 'ucx'
COLL = 'tuned,basic,libnbc,self'


def validate_package():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if digest(ROOT / name) != expected:
            raise ValueError('OSU toolkit file hash mismatch: ' + name)
    for name, expected in manifest['a89_dependencies'].items():
        if digest(A89 / name) != expected:
            raise ValueError('reused A89 helper hash mismatch: ' + name)
    return manifest


def fixed_mca(command):
    """Keep the measured launcher, but select identical UCX/tuned paths once."""
    result = []
    index = 0
    while index < len(command):
        if command[index] in ('--mca', '-mca') and index + 2 < len(command):
            if command[index + 1] in ('pml', 'coll', 'coll_tuned_use_dynamic_rules'):
                index += 3
                continue
        result.append(command[index])
        index += 1
    result[1:1] = ['--mca', 'pml', PML, '--mca', 'coll', COLL,
                   '--mca', 'coll_tuned_use_dynamic_rules', '0']
    return result


def arguments(benchmark, iterations, warmup, validation, message_bytes=8):
    result = ['--message-size', '%d:%d' % (message_bytes, message_bytes),
              '--iterations', str(iterations), '--warmup', str(warmup)]
    if benchmark == 'osu_alltoall':
        result.append('--full')
    if validation:
        result.append('--validation')
    return result


def mpi_dependency(text):
    match = re.search(r'^\s*libmpi\.so[^\s]*\s+=>\s+(/\S+)', text, re.M)
    if not match or 'not found' in text:
        raise ValueError('MPI dependency missing/unresolved')
    return Path(match[1]).resolve()


def check_collective_components(text):
    available = set(re.findall(r'^mca:coll:([^:]+):(?:version|param):', text, re.M))
    required = set(COLL.split(','))
    missing = sorted(required - available)
    if missing:
        raise ValueError('required MPI collective components unavailable: ' + ','.join(missing))
    return sorted(available)


def capture_cli(command, env, out, name, timeout=25):
    """Persist the actual singleton help/component environment and exit status."""
    record = {'command': command, 'environment': {k: v for k, v in env.items()
              if k.startswith(('LD_', 'OPAL_', 'OMPI_', 'UCX_')) or k == 'PATH'}}
    path = out / (name + '.json')
    base.write_json(path, record)
    try:
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        def decode(value):
            return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
        (out / (name + '.txt')).write_text(decode(exc.stdout) + decode(exc.stderr))
        record.update(timed_out=True, timeout_seconds=timeout)
        base.write_json(path, record)
        raise RuntimeError('CLI timeout; details=' + str(path)) from exc
    text = result.stdout + result.stderr
    (out / (name + '.txt')).write_text(text)
    record.update(exit=result.returncode, stdout_stderr_sha256=hashlib.sha256(text.encode()).hexdigest())
    base.write_json(path, record)
    return result


def collective_preflight(out, info, env, runtime):
    """Check mandatory blocking and nonblocking modules on both MPI installs."""
    command = [str(info), '--parsable', '--param', 'coll', 'all']
    local = capture_cli(command, env, out, 'hmpi_coll_components_node89')
    if local.returncode:
        raise ValueError('local MPI collective component query failed')
    local_text = local.stdout + local.stderr
    local_available = check_collective_components(local_text)
    code = ('import os,subprocess,json\ne=dict(os.environ);e.update(json.loads(' + repr(json.dumps(runtime)) + '))\n'
            'p=subprocess.run(' + repr(command) + ',env=e,text=True,capture_output=True,timeout=25)\n'
            'print(json.dumps({"exit":p.returncode,"stdout":p.stdout,"stderr":p.stderr}))\n')
    peer = json.loads(base.ssh('python3 -c "$1"\n', code, timeout=35))
    peer_text = peer['stdout'] + peer['stderr']
    (out / 'hmpi_coll_components_node97.txt').write_text(peer_text)
    base.write_json(out / 'hmpi_coll_components_node97.json',
                    {'command': command, 'environment': runtime, **peer})
    if peer['exit']:
        raise ValueError('peer MPI collective component query failed')
    peer_available = check_collective_components(peer_text)
    return {'required': COLL.split(','), 'node89_available': local_available,
            'node97_available': peer_available}


def preflight(out, commands):
    """No two-node benchmark here: module/CLI/loader/CPU checks and a probe build."""
    evidence = {'candidate': base.module_check(DEFAULT_CASE / 'install', PRODUCT_MODULE),
                'inventory': base.inventory(), 'benchmarks': {}}
    if min(row['available_cores'] for row in evidence['inventory']) < 1:
        raise ValueError('no usable CPU core')
    saved = commands['rm'][1]
    help_env = {k: v for k, v in os.environ.items() if not k.startswith('UCX_')}
    help_env.update(saved)
    help_env.pop('LD_PRELOAD', None)
    help_env.pop('LD_DEBUG', None)
    help_env.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1',
                    OMPI_MCA_pml='ob1', OMPI_MCA_coll=COLL)
    # Keep identical runtime dependency resolution on the peer without a shell profile assumption.
    runtime = {key: value for key, value in help_env.items()
               if key.startswith(('LD_', 'OPAL_', 'OMPI_')) or key == 'PATH'}
    mpi_launcher = next(Path(v) for v in commands['rm'][0] if Path(v).name in ('mpirun', 'mpiexec'))
    evidence['collective_components'] = collective_preflight(
        out, mpi_launcher.with_name('ompi_info'), help_env, runtime)
    for benchmark, (relative, _, _) in BENCHMARKS.items():
        path = OSU / relative
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError('OSU benchmark missing: ' + str(path))
        record = {'path': str(path), 'sha256': digest(path)}
        peer_sha = base.ssh('sha256sum "$1"\n', path).split()[0]
        if peer_sha != record['sha256']:
            raise ValueError('OSU binary differs between nodes: ' + benchmark)
        for option, name in (('-h', 'help'), ('-v', 'version')):
            result = capture_cli([str(path), option], help_env, out, benchmark + '_' + name)
            text = result.stdout + result.stderr
            (out / (benchmark + '_' + name + '.txt')).write_text(text)
            if result.returncode:
                raise ValueError('OSU ' + name + ' failed: ' + benchmark +
                                 ' EXIT=' + str(result.returncode) + ' LOG=' + str(out / (benchmark + '_' + name + '.txt')))
            record[name + '_sha256'] = hashlib.sha256(text.encode()).hexdigest()
            if option == '-h':
                required = ['--message-size', '--iterations', '--warmup', '--validation']
                if benchmark == 'osu_alltoall':
                    required.append('--full')
                if any(item not in text for item in required):
                    raise ValueError('OSU required options missing: ' + benchmark)
            elif not re.search(r'(?<!\d)7\.5\.2(?!\d)', text):
                raise ValueError('OSU version is not 7.5.2')
        local_ldd = subprocess.check_output(['ldd', str(path)], env=help_env, text=True, timeout=20)
        code = ('import os,subprocess,json\ne=dict(os.environ);e.update(json.loads(' + repr(json.dumps(runtime)) + '))\n'
                'p=subprocess.run(["ldd",' + repr(str(path)) + '],env=e,text=True,capture_output=True,timeout=20)\n'
                'print(p.stdout);print(p.stderr);raise SystemExit(p.returncode)\n')
        peer_ldd = base.ssh('python3 -c "$1"\n', code)
        mpi = mpi_dependency(local_ldd)
        peer_mpi = mpi_dependency(peer_ldd)
        if mpi != peer_mpi or base.ssh('sha256sum "$1"\n', mpi).split()[0] != digest(mpi):
            raise ValueError('OSU HMPI dependency path/hash differs between nodes')
        record.update(local_ldd=local_ldd, peer_ldd=peer_ldd,
                      mpi_library=str(mpi), mpi_library_sha256=digest(mpi))
        evidence['benchmarks'][benchmark] = record
    mpi_paths = {r['mpi_library'] for r in evidence['benchmarks'].values()}
    if len(mpi_paths) != 1:
        raise ValueError('OSU benchmarks use different MPI libraries')
    mpi_launcher = next(Path(v) for v in commands['rm'][0] if Path(v).name in ('mpirun', 'mpiexec'))
    probe = out / 'libosu_identity.so'
    compile_command = [str(mpi_launcher.with_name('mpicc')), '-std=c99', '-O2', '-g',
                       '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                       str(ROOT / 'identity.c'), '-o', str(probe)]
    base.run_logged(compile_command, help_env, out / 'compile_identity.log', 60)
    probe_mpi = mpi_dependency(subprocess.check_output(['ldd', str(probe)], env=help_env, text=True))
    if str(probe_mpi) not in mpi_paths:
        raise ValueError('metadata probe linked to a different MPI')
    evidence.update(identity_compile_command=compile_command, identity_sha256=digest(probe),
                    saved_launcher_sha256={kind: digest(LAB / 'osu_minimal8_ff_14ns8' / (kind + '_run.json'))
                                           for kind in ('rm', 'rc')},
                    observer={'local': digest(LAB / 'observer_build_d886fc3/liburma_resource_observer.so'),
                              'peer': base.ssh('sha256sum "$1"\n',
                                      LAB / 'observer_runtime_d886fc3/liburma_resource_observer.so').split()[0]},
                    coll_components=COLL, pml=PML, coll_tuned_dynamic_rules=0)
    # Capture available/tuned MPI settings instead of guessing numeric algorithm IDs.
    info = mpi_launcher.with_name('ompi_info')
    result = subprocess.run([str(info), '--param', 'coll', 'tuned', '--level', '9'],
                            env=help_env, text=True, capture_output=True, timeout=25)
    (out / 'hmpi_coll_tuned_info.txt').write_text(result.stdout + result.stderr)
    if result.returncode or 'tuned' not in result.stdout:
        raise ValueError('HMPI tuned collective component unavailable')
    return evidence


def one_trial(out, commands, evidence, benchmark, mode, ppn, phase, repeat, args,
              message_bytes=8, segment_size=None):
    name = '%s_%s_p%d_%s_%d' % (benchmark, mode, ppn, phase, repeat)
    if message_bytes != 8:
        name += '_b%d' % message_bytes
    run = out / name
    run.mkdir()
    base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', run)
    current = base.inventory()
    configuration = budget(ppn)
    for old, now in zip(evidence['inventory'], current):
        if any(old[k] != now[k] for k in ('hostname', 'boot_id', 'allowed_cpus')):
            raise ValueError('node reboot/CPU permission changed; stop fair comparison')
        if now['available_cores'] < ppn or now['mem_available_kib'] < ppn * configuration['rx_depth'] * 16 + 512 * 1024:
            raise ValueError('current CPU/memory staging budget insufficient')
    command, saved = commands[mode]
    env = environment(saved, 'iface' if mode == 'rm' else 'rc', DEFAULT_CASE / 'install', configuration)
    if segment_size is not None:
        if segment_size != 4096 or mode != 'rm':
            raise ValueError('only the bounded RM 4K segmentation mode is supported')
        env['UCX_RM_URMA_SEG_SIZE'] = str(segment_size)
    env.update(OMPI_MCA_pml=PML, OMPI_MCA_coll=COLL, OMPI_MCA_coll_tuned_use_dynamic_rules='0',
               HUCX_OSU_PROBE_DIR=str(run))
    env['LD_PRELOAD'] += ':' + str(out / 'libosu_identity.so')
    # Config parsed on both nodes; leave logs beside every trial for review.
    if segment_size is None:
        base.parsed_config(DEFAULT_CASE / 'install', env, run, 'iface' if mode == 'rm' else 'rc')
    else:
        from boundary_config import parsed_config_with_units
        parsed_config_with_units(DEFAULT_CASE / 'install', env, run, 'iface')
    nonce = uuid.uuid4().hex
    validation = phase == 'validation'
    iterations = min(args.iterations, 1000) if validation else args.iterations
    warmup = min(args.warmup, 100) if validation else args.warmup
    hosts = [row['hostname'] for row in evidence['inventory']]
    config = {'nonce': nonce, 'run': str(run), 'environment': env,
              'resource_probe': phase == 'resources', 'binary': str(OSU / BENCHMARKS[benchmark][0]),
              'timeout': args.timeout,
              'arguments': arguments(benchmark, iterations, warmup, validation,
                                     message_bytes),
              'observers': {hosts[0]: str(LAB / 'observer_build_d886fc3/liburma_resource_observer.so'),
                            hosts[1]: str(LAB / 'observer_runtime_d886fc3/liburma_resource_observer.so')}}
    path = run / 'rank_config.json'
    base.write_json(path, config)
    base.copy_remote([path], str(run) + '/')
    command = fixed_mca(launcher(command, ppn, base.HOSTS, out / 'osu_rank.py', path))
    launch_env = {k: v for k, v in os.environ.items() if not k.startswith('UCX_')
                  and k not in ('LD_PRELOAD', 'LD_DEBUG')}
    launch_env.update(saved)
    launch_env.pop('LD_PRELOAD', None)
    launch_env.pop('LD_DEBUG', None)
    launch_env.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    row = {'name': name, 'benchmark': benchmark, 'mode': mode, 'ppn': ppn, 'phase': phase,
           'repeat': repeat, 'passed': False, 'configuration': config,
           'iterations': iterations, 'warmup': warmup, 'budget': configuration}
    base.write_json(run / 'trial.json', row)
    try:
        row.update(base.run_logged(command, launch_env, run / 'launcher.log', args.timeout + 20))
        fetch(run, range(ppn, 2 * ppn), phase == 'resources', base.PEER, schema='osu')
        row['output'] = parse_output((run / 'launcher.log').read_text(errors='replace'),
                                     benchmark, validation, iterations, message_bytes)
        row['rank_evidence'] = inspect_ranks(run, ppn, evidence['inventory'],
                       DEFAULT_CASE / 'install', nonce, evidence['benchmarks'][benchmark]['mpi_library'])
        if phase == 'resources':
            row['jettys'] = inspect_jettys(run, ppn, 1 if mode == 'rm' else 2,
                                          row['rank_evidence']['pids'], configuration['tx_depth'])
        elif list(run.glob('rank*_pid*.jsonl')):
            raise ValueError('non-resource trial unexpectedly loaded URMA observer')
        row['passed'] = True
    except BaseException as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        try:
            local = subprocess.check_output([sys.executable, str(out / 'osu_rank.py'), str(path), '--cleanup'],
                                             text=True, timeout=15)
            peer = base.ssh('python3 "$1" "$2" --cleanup\n', out / 'osu_rank.py', path)
            row['cleanup'] = {'local': json.loads(local), 'peer': json.loads(peer)}
            if any(value['terminated_own_pids'] for value in row['cleanup'].values()):
                row['passed'] = False
                row['error'] = row.get('error', 'application processes remained after launcher exit')
        except Exception as exc:
            row['passed'] = False
            row['cleanup_error'] = str(exc)
        base.write_json(run / 'trial.json', row)
    return row


def main():
    parser = argparse.ArgumentParser(description='标准OSU 8B新版RM/RC对照；默认只预检，不启动双节点测试')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true', help='执行占用双节点的标准OSU对照')
    action.add_argument('--preflight-only', action='store_true', help='只检查CLI/依赖/库/CPU，并编译小型身份探针')
    action.add_argument('--plan', action='store_true', help='本地校验包及计划，不SSH/不编译/不启动MPI')
    parser.add_argument('--ppn', type=int, nargs='+', default=[1, 8, 16, 20])
    parser.add_argument('--benchmarks', nargs='+', choices=list(BENCHMARKS), default=list(BENCHMARKS))
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--iterations', type=int, default=5000)
    parser.add_argument('--warmup', type=int, default=1000)
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    if (sorted(set(args.ppn)) != args.ppn or any(p < 1 or p > 20 for p in args.ppn) or
            args.repeats < 3 or args.iterations < 1000 or args.warmup < 100 or args.timeout < 60):
        parser.error('PPN须递增唯一且1..20，repeats>=3，iterations>=1000，warmup>=100，timeout>=60')
    manifest = validate_package()
    plan = {'source_base': '944d0a4e2d4d2291006b760cf03827f000f426dd',
            'product_commit': PRODUCT_COMMIT, 'module_sha256': PRODUCT_MODULE,
            'osu_version': '7.5.2', 'benchmarks': args.benchmarks, 'message_bytes': 8,
            'ppn': args.ppn, 'repeats': args.repeats, 'iterations': args.iterations,
            'warmup': args.warmup, 'pml': PML, 'coll_components': COLL,
            'timing_validation': False, 'separate_validation_and_resources': True,
            'node_tests': 'NOT_RUN'}
    if args.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    if socket.gethostname() != 'node89' or os.geteuid() != 0:
        raise ValueError('本工具由root在node89执行；当前不自动登录节点')
    build = json.loads((DEFAULT_CASE / 'build_manifest.json').read_text())
    if build['source_commit'] != PRODUCT_COMMIT or build['module_sha256'] != PRODUCT_MODULE:
        raise ValueError('fixed tested CASE identity mismatch')
    out = Path(tempfile.mkdtemp(prefix='osu_compare_', dir=DEFAULT_CASE))
    print('OSU_COMPARE_DIR=' + str(out), flush=True)
    report = {'manifest': manifest, 'plan': plan, 'trials': [], 'comparisons': [],
              'status': 'PREFLIGHT', 'product_unchanged': True, 'full_feature_acceptance': False,
              'a9': 'DEFERRED_BY_USER_NODE_SHARING', 'reliability_C': 'DEFERRED',
              'started': time.strftime('%F %T %Z')}

    def save():
        base.write_json(out / 'results.json', report)
        write_tables(out, report)

    save()
    try:
        commands = {kind: saved_spec(LAB / 'osu_minimal8_ff_14ns8' / (kind + '_run.json'),
                                     DEFAULT_CASE / 'install') for kind in ('rm', 'rc')}
        report['preflight'] = preflight(out, commands)
        save()
        print('OSU预检=通过 VERSION=7.5.2 BINARIES_BOTH_MATCH=YES OPTIONS=PASS LIBRARIES=PASS', flush=True)
        if not args.run:
            report['status'] = 'PREFLIGHT_PASS_NOT_MEASURED'
            save()
            print('本次未启动双节点性能测试；正式执行需在节点空闲窗口追加 --run', flush=True)
        else:
            if max(args.ppn) > min(r['available_cores'] for r in report['preflight']['inventory']):
                raise ValueError('requested PPN exceeds allowed physical cores')
            # Share only our helper and wrapper; original OSU binaries remain untouched.
            shutil.copy2(ROOT / 'osu_rank.py', out / 'osu_rank.py')
            shutil.copy2(A89 / 'rank_exec.py', out / 'a89_rank_exec.py')
            base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
            for path in (out / 'osu_rank.py', out / 'a89_rank_exec.py', out / 'libosu_identity.so'):
                base.copy_remote([path], str(out) + '/')
                if base.ssh('sha256sum "$1"\n', path).split()[0] != digest(path):
                    raise ValueError('peer helper/wrapper hash mismatch')
            report['status'] = 'RUNNING'
            report['plan']['node_tests'] = 'RUNNING'
            save()
            for ppn in args.ppn:
                for benchmark in args.benchmarks:
                    # First validate both modes before accepting any timing at this scale.
                    schedule = [('validation', 0, mode) for mode in ('rc', 'rm')]
                    for repeat in range(args.repeats):
                        order = ('rc', 'rm') if repeat % 2 == 0 else ('rm', 'rc')
                        schedule.extend(('timing', repeat, mode) for mode in order)
                    schedule.extend(('resources', 0, mode) for mode in ('rc', 'rm'))
                    for phase, repeat, mode in schedule:
                        row = one_trial(out, commands, report['preflight'], benchmark,
                                         mode, ppn, phase, repeat, args)
                        report['trials'].append(row)
                        save()
                        if not row['passed']:
                            raise RuntimeError('STOP ' + row['name'] + ': ' +
                                               row.get('error', row.get('cleanup_error', 'unknown'))[:600])
                    compared = compare(report['trials'], benchmark, ppn, args.repeats)
                    report['comparisons'].append(compared)
                    save()
                    rc, rm = compared['modes']['rc'], compared['modes']['rm']
                    print('%s PPN=%d RC=%.3fus RM=%.3fus RM耗时降低=%+.2f%% DATA=PASS EXIT=PASS' %
                          (benchmark, ppn, rc['latency_us']['median'], rm['latency_us']['median'],
                           compared['rm_latency_reduction_pct']), flush=True)
                    print('资源 Jetty每进程峰值RC/RM=%s/%s；Finalize前RSS两节点合计RC/RM=%.2f/%.2fMiB' %
                          (rc['jetty_process_peak_range'], rm['jetty_process_peak_range'],
                           rc['before_finalize_rss_sum_mib']['median'],
                           rm['before_finalize_rss_sum_mib']['median']), flush=True)
            report['status'] = 'REQUESTED_OSU_MATRIX_PASS_REVIEW_ALL_SAMPLES'
            report['plan']['node_tests'] = 'COMPLETED_REQUESTED_MATRIX'
            save()
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
        print('TABLE_MD=' + str(out / 'comparison.md'), flush=True)
        print('TABLE_CSV=' + str(out / 'comparison.csv'), flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        print('OSU_COMPARE_STOPPED=' + report['error'][:800], file=sys.stderr, flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
