#!/usr/bin/env python3
"""MPI OSC integration on the already tested 989 product; no transport edits."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(ROOT))
from evidence import BENCHMARKS, osu_output, probe_output, require_paths, scan_ucx, correlate_mpi_data

spec = importlib.util.spec_from_file_location('mpi_rma_previous', PROJECT / 'native_rma/run_candidate.py')
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)
base = prior.base
osu = prior.osu
DEFAULT_CASE = base.B_ROOT / 'r1_shared_989fd7186_xncBHX'
PRODUCT_COMMIT = '989fd71865046bde988603c97416dc133c582e88'
PRODUCT_MODULE = '7b08a804a02d67fdc965b0055a55459ebf0dc4af198cbe4096ccf91019fb2530'


def write(path, value):
    osu.base.write_json(path, value)


def clean_environment(source):
    return {k: v for k, v in source.items()
            if not k.startswith(('UCX_', 'MPI_UCX_', 'OMPI_MCA_'))
            and k not in ('LD_PRELOAD', 'LD_DEBUG')}


def settings(saved, case, mode, diagnostic, zcopy='1024'):
    env = clean_environment(saved)
    env.update(osu.environment(clean_environment(saved), 'iface' if mode == 'rm' else 'rc',
                               case / 'install', osu.budget(1)))
    # The current MPI/UCX config reader uses MPI_UCX_ plus ordinary UCX_ defaults.
    # Pin both to prevent a pre-existing MPI-specific override selecting old libs.
    env.update(UCX_TLS=('rm_urma' if mode == 'rm' else 'rc_urma') + ',self',
               UCX_RM_URMA_NATIVE_RMA='y', UCX_RM_URMA_NATIVE_READ='n',
               UCX_RM_URMA_NATIVE_WRITE='n', UCX_RM_URMA_MAX_PEERS='5',
               UCX_BCOPY_THRESH='0', UCX_ZCOPY_THRESH=zcopy,
               UCX_LOG_LEVEL='trace' if diagnostic else 'warn',
               UCX_WARN_UNUSED_ENV_VARS='n', OMPI_MCA_pml='ucx', OMPI_MCA_osc='ucx',
               OMPI_MCA_coll=osu.COLL, OMPI_MCA_coll_tuned_use_dynamic_rules='0')
    env.update({'MPI_' + key: value for key, value in list(env.items()) if key.startswith('UCX_')})
    return env


def fixed_launcher(command):
    result = []
    index = 0
    keys = {'pml', 'osc', 'coll', 'coll_tuned_use_dynamic_rules'}
    while index < len(command):
        if command[index] in ('--mca', '-mca', '--gmca', '-gmca') and index + 2 < len(command):
            if command[index + 1] in keys:
                index += 3
                continue
        result.append(command[index])
        index += 1
    result[1:1] = ['--mca', 'pml', 'ucx', '--mca', 'osc', 'ucx',
                   '--mca', 'coll', osu.COLL, '--mca', 'coll_tuned_use_dynamic_rules', '0']
    return result


def identity(case):
    build = base.validate_build(case)
    if build['source_commit'] != PRODUCT_COMMIT or build['module_sha256'] != PRODUCT_MODULE:
        raise ValueError('this tool requires the already accepted 989 product; do not substitute old CASE')
    parent = case / 'native_rma_accept_7ssm55bv/results.json'
    report = json.loads(parent.read_text())
    if (report['source_commit'] != PRODUCT_COMMIT or report['module_sha256'] != PRODUCT_MODULE or
            report['status'] != 'BASIC_UCT_AND_PUBLIC_UCP_RMA_NORMAL_PASS_MPI_OSU_PENDING'):
        raise ValueError('prior accepted product checkpoint mismatch')
    if len(report['trials']) != 3 or not all(row['passed'] for row in report['trials']):
        raise ValueError('prior UCT/UCP checkpoint incomplete')
    normal_path = Path(report['normal']['results_json']).resolve()
    if normal_path.parent.parent != case:
        raise ValueError('normal checkpoint outside the accepted CASE')
    normal = json.loads(normal_path.read_text())
    if (normal['source_commit'] != PRODUCT_COMMIT or normal['module_sha256'] != PRODUCT_MODULE or
            len(normal['cases']) != 28 or not all(row['passed'] for row in normal['cases'])):
        raise ValueError('prior normal 28 checkpoint incomplete')
    return build, {'path': str(parent), 'sha256': osu.digest(parent),
                   'normal_path': str(normal_path), 'normal_sha256': osu.digest(normal_path),
                   'reuse_reason': 'transport and installed product unchanged; no repeated native regression'}


def peer_cli(command, env, out, name):
    result = osu.capture_cli(command, env, out, name + '_node89', timeout=30)
    script = ('import json,os,subprocess\ne=dict(os.environ);e.update(json.loads(' +
              repr(json.dumps(env)) + '))\np=subprocess.run(' + repr(command) +
              ',env=e,text=True,capture_output=True,timeout=30)\n'
              'print(json.dumps({"exit":p.returncode,"stdout":p.stdout,"stderr":p.stderr}))\n')
    peer = json.loads(osu.base.ssh('python3 -c "$1"\n', script, timeout=40))
    (out / (name + '_node97.txt')).write_text(peer['stdout'] + peer['stderr'])
    if result.returncode or peer['exit']:
        raise ValueError('preflight command failed: ' + name)
    return result.stdout + result.stderr, peer['stdout'] + peer['stderr']


def same_file(path):
    path = Path(path)
    local = osu.digest(path)
    remote = osu.base.ssh('sha256sum "$1"\n', path).split()[0]
    if local != remote:
        raise ValueError('two-node file identity mismatch: ' + str(path))
    return {'path': str(path), 'sha256': local}


def preflight(case, out, commands):
    saved = commands['rm'][1]
    env = clean_environment(os.environ)
    env.update(clean_environment(saved))
    env.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1',
               OMPI_MCA_pml='ob1', OMPI_MCA_osc='pt2pt', OMPI_MCA_coll=osu.COLL)
    inventory = osu.base.inventory()
    if any(row['available_cores'] < 1 for row in inventory):
        raise ValueError('no usable physical core')
    launcher = next(Path(item) for item in commands['rm'][0]
                    if Path(item).name in ('mpirun', 'mpiexec'))
    info = launcher.with_name('ompi_info')
    components = peer_cli([str(info), '--parsable', '--param', 'osc', 'all'], env, out, 'osc_components')
    if not all(re.search(r'^mca:osc:ucx:', text, re.M) for text in components):
        raise ValueError('osc_ucx unavailable on one or both MPI installations')
    peer_cli([str(info), '--version'], env, out, 'hmpi_version')
    osu.collective_preflight(out, info, env, env)
    binaries = {}
    mpi_paths = set()
    for name, (relative, _, _, _) in BENCHMARKS.items():
        path = osu.OSU / relative
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError('original OSU RMA binary not installed: ' + str(path))
        entry = same_file(path)
        help_texts = peer_cli([str(path), '-h'], env, out, name + '_help')
        for text in help_texts:
            if any(option not in text for option in ('-m,', '-i,', '-x,', '-s,', '-w,')):
                raise ValueError('OSU RMA CLI option missing: ' + name)
        versions = peer_cli([str(path), '-v'], env, out, name + '_version')
        if not all(re.search(r'(?<!\d)7\.5\.2(?!\d)', text) for text in versions):
            raise ValueError('OSU version is not 7.5.2')
        dependencies = peer_cli(['ldd', str(path)], env, out, name + '_ldd')
        paths = [osu.mpi_dependency(text) for text in dependencies]
        if paths[0] != paths[1]:
            raise ValueError('MPI dependency paths differ between nodes')
        entry['mpi_library'] = same_file(paths[0])
        mpi_paths.add(str(paths[0]))
        binaries[name] = entry
    if len(mpi_paths) != 1:
        raise ValueError('OSU binaries use different MPI libraries')
    mpi_library = Path(next(iter(mpi_paths)))
    layouts = peer_cli([str(info), '--parsable', '--path', 'pkglibdir'], env, out, 'hmpi_component_path')
    component_dirs = []
    for text in layouts:
        match = re.search(r'^path:pkglibdir:(/.+)$', text, re.M)
        if not match:
            raise ValueError('cannot resolve MPI component directory from actual ompi_info')
        component_dirs.append(Path(match[1].strip()))
    if component_dirs[0] != component_dirs[1]:
        raise ValueError('MPI component directories differ between nodes')
    component = component_dirs[0] / 'mca_osc_ucx.so'
    if not component.is_file():
        raise ValueError('dynamic OSC UCX module absent at reported pkglibdir; retain actual layout logs')
    component_identity = same_file(component)
    component_dependencies = peer_cli(['ldd', str(component)],
        dict(env, LD_LIBRARY_PATH=str(case / 'install/lib') + ':' + env.get('LD_LIBRARY_PATH', '')),
        out, 'osc_ucx_ldd')
    for text in component_dependencies:
        if 'not found' in text:
            raise ValueError('unresolved OSC UCX dynamic dependency')
        for library in ('ucp', 'uct', 'ucs'):
            match = re.search(r'lib' + library + r'\.so[^\s]*\s+=>\s+(/\S+)', text)
            if not match or not Path(match[1]).resolve().is_relative_to(case / 'install/lib'):
                raise ValueError('OSC dependency does not resolve to accepted candidate: ' + library)
    for source, target, flags in ((ROOT / 'mpi_probe.c', out / 'mpi_probe', []),
                                  (osu.ROOT / 'identity.c', out / 'libosu_identity.so', ['-shared', '-fPIC']),
                                  (ROOT / 'mpi_path.c', out / 'libmpi_path.so',
                                   ['-shared', '-fPIC', '-I' + str(case / 'install/include'), '-ldl'])):
        command = [str(launcher.with_name('mpicc')), '-std=c11', '-O2', '-g', '-Wall',
                   '-Wextra', '-Werror'] + flags + [str(source), '-o', str(target)]
        osu.base.run_logged(command, env, out / (target.name + '_compile.log'), 90)
        linked = osu.mpi_dependency(subprocess.check_output(['ldd', str(target)], env=env, text=True))
        if linked != mpi_library:
            raise ValueError('compiled MPI probe dependency differs from original OSU')
    shutil.copy2(osu.ROOT / 'osu_rank.py', out / 'osu_rank.py')
    shutil.copy2(osu.A89 / 'rank_exec.py', out / 'a89_rank_exec.py')
    osu.base.ssh('set -eu\nmkdir -p "$1"\n', out)
    helpers = [out / name for name in ('mpi_probe', 'libosu_identity.so', 'libmpi_path.so', 'osu_rank.py', 'a89_rank_exec.py')]
    osu.base.copy_remote(helpers, str(out) + '/')
    helper_identities = [same_file(path) for path in helpers]
    return {'inventory': inventory, 'benchmarks': binaries, 'mpi_library': str(mpi_library),
            'osc_ucx_module': component_identity, 'helpers': helper_identities,
            'node_MPI_source_equivalence': 'NOT_CLAIMED_RUNTIME_TESTS_REQUIRED',
            'candidate': osu.base.module_check(case / 'install', PRODUCT_MODULE)}


def collect_peer(run):
    # PPN1 only: small fixed file set; no hundreds-of-path scp expansion.
    code = ('import json,pathlib\nr=pathlib.Path(' + repr(str(run)) + ')\na=[]\n'
            'for p in sorted(r.iterdir()):\n'
            ' if p.name.startswith(("rank1", "pid_rank1", "ucx_node97_")):\n'
            '  if p.is_symlink() or not p.is_file() or p.stat().st_size>512*1048576: '
            'raise ValueError("invalid remote artifact")\n'
            '  a.append(p.name)\nprint(json.dumps(a))\n')
    names = json.loads(osu.base.ssh('python3 -c "$1"\n', code))
    if len(names) > 20 or any(Path(name).name != name for name in names):
        raise ValueError('unexpected remote artifact set')
    for name in names:
        path = run / name
        if path.exists():
            raise ValueError('unexpected local peer artifact already exists: ' + name)
    if names:
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8'] +
                       [osu.base.PEER + ':' + str(run / name) for name in names] + [str(run) + '/'],
                       check=True, timeout=150)
    return [same_file(run / name) for name in names]


def trial(case, out, commands, evidence, name, mode, binary, arguments, args,
          probe=None, diagnostic=True, benchmark=None, size=None, zcopy='1024'):
    run = out / name
    run.mkdir()
    osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', out, run)
    command, saved = commands[mode]
    env = settings(saved, case, mode, diagnostic, zcopy)
    env['LD_PRELOAD'] += ':' + str(out / 'libosu_identity.so')
    env['HUCX_OSU_PROBE_DIR'] = str(run)
    if diagnostic:
        env['LD_PRELOAD'] = str(out / 'libmpi_path.so') + ':' + env['LD_PRELOAD']
        env['UCX_LOG_FILE'] = str(run / 'ucx_%h_%p.log')
        env['MPI_UCX_LOG_FILE'] = env['UCX_LOG_FILE']
    config = {'nonce': uuid.uuid4().hex, 'run': str(run), 'environment': env,
              'resource_probe': False, 'binary': str(binary), 'arguments': arguments,
              'timeout': args.timeout, 'observers': {}}
    config_path = run / 'rank_config.json'
    write(config_path, config)
    osu.base.copy_remote([config_path], str(run) + '/')
    command = fixed_launcher(osu.launcher(command, 1, osu.base.HOSTS, out / 'osu_rank.py', config_path))
    launch_env = clean_environment(os.environ)
    launch_env.update(clean_environment(saved))
    launch_env.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    row = {'name': name, 'mode': mode, 'passed': False, 'diagnostic': diagnostic,
           'configuration': config, 'benchmark': benchmark, 'size': size}
    try:
        row.update(osu.base.run_logged(command, launch_env, run / 'launcher.log', args.timeout + 20))
    except BaseException as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        try:
            local = subprocess.check_output([sys.executable, str(out / 'osu_rank.py'),
                                            str(config_path), '--cleanup'], text=True, timeout=15)
            peer = osu.base.ssh('python3 "$1" "$2" --cleanup\n', out / 'osu_rank.py', config_path)
            row['cleanup'] = {'local': json.loads(local), 'peer': json.loads(peer)}
            if any(value['terminated_own_pids'] for value in row['cleanup'].values()):
                raise ValueError('application processes remained after launcher exit')
        except Exception as exc:
            row['cleanup_error'] = str(exc)
        try:
            row['peer_artifacts'] = collect_peer(run)
        except Exception as exc:
            row['artifact_error'] = str(exc)
    try:
        if row.get('error') or row.get('cleanup_error') or row.get('artifact_error'):
            raise ValueError('runtime/cleanup/artifact gate failed; see trial.json and launcher.log')
        text = (run / 'launcher.log').read_text(errors='replace')
        row['output'] = probe_output(text, probe) if probe else osu_output(text, benchmark, size)
        row['rank_evidence'] = osu.inspect_ranks(run, 1, evidence['inventory'], case / 'install',
                                               config['nonce'], evidence['mpi_library'])
        scans = []
        for rank in range(2):
            maps = (run / ('rank%d_before_finalize.maps' % rank)).read_text()
            modules = [line.split()[-1] for line in maps.splitlines() if '/mca_osc_ucx.so' in line]
            if not modules or set(modules) != {evidence['osc_ucx_module']['path']}:
                raise ValueError('actual application did not map the pinned OSC UCX component')
            pid = row['rank_evidence']['pids'][rank]
            if diagnostic:
                path = run / ('ucx_%s_%d.log' % (evidence['inventory'][rank]['hostname'], pid))
                scan = scan_ucx(path)
                scan.update(rank=rank, sha256=osu.digest(path), path=str(path))
                if mode == 'rm' and probe != 'atomics':
                    path_log = run / ('rank%d_ucp_path.jsonl' % rank)
                    # OSU one-sided data is issued by rank0; the MPI probe tests both origins.
                    if probe or rank == 0:
                        expected = ('put', 'get') if probe else (BENCHMARKS[benchmark][3],)
                        scan['business_path'] = correlate_mpi_data(path_log, scan, rank, pid, expected)
                        scan['business_log_sha256'] = osu.digest(path_log)
                scans.append(scan)
        if diagnostic and mode == 'rm':
            kinds = () if probe == 'atomics' else ('put', 'get') if probe else (BENCHMARKS[benchmark][3],)
            require_paths(scans, kinds, atomic=probe == 'atomics')
            if probe == 'fence':
                needed = ('put_bcopy', 'get_bcopy') if zcopy == 'inf' else ('put_zcopy', 'get_zcopy')
                require_paths(scans, needed)
        if not diagnostic:
            for rank in range(2):
                maps = (run / ('rank%d_before_finalize.maps' % rank)).read_text()
                if 'libmpi_path.so' in maps:
                    raise ValueError('diagnostic path interceptor loaded during timing')
        row['native_evidence'] = scans
        row['passed'] = True
        if probe:
            print('MPI_GATE MODE=%s SYNC=%s DATA/GUARDS/MAPS/OSC/EXIT=PASS' % (mode.upper(), probe), flush=True)
        else:
            print('OSU=%s MODE=%s SIZE=%d SYNC=fence EXIT/MAPS/OSC%s=PASS DATA_CHECK=separate_gate' %
                  (benchmark, mode.upper(), size, '/NATIVE' if mode == 'rm' and diagnostic else ''), flush=True)
    except BaseException as exc:
        row.setdefault('error', type(exc).__name__ + ': ' + str(exc))
    write(run / 'trial.json', row)
    if not row['passed']:
        raise RuntimeError('stopped at %s; details=%s' % (name, run / 'trial.json'))
    return row


def main():
    parser = argparse.ArgumentParser(description='已验989产品的MPI窗口与原版OSU单边接入；默认仅计划')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--case', type=Path, default=DEFAULT_CASE)
    parser.add_argument('--timing', action='store_true', help='功能取证通过后追加无trace三轮计时')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    if args.timing and not args.run:
        parser.error('--timing requires --run')
    if not 60 <= args.timeout <= 600:
        parser.error('timeout must be 60..600 seconds')
    if not args.run and not args.preflight_only:
        print('PLAN: PPN1 two nodes; accepted 989 product unchanged; RC fence control; '
              'RM BCOPY/ZCOPY fence + existing SW_AMO + lock_all; original OSU PUT/GET latency/bw '
              '8/4096/65536B fence/create; no built-in OSU validation claim; timing opt-in; no rebuild')
        return
    case = args.case.resolve()
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89')
    build, checkpoint = identity(case)
    out = Path(tempfile.mkdtemp(prefix='mpi_rma_', dir=case))
    report = {'status': 'RUNNING', 'source_commit': PRODUCT_COMMIT,
              'module_sha256': PRODUCT_MODULE, 'product_unchanged': True,
              'prior_checkpoint': checkpoint, 'trials': [], 'full_B4_accepted': False,
              'C_retry_accepted': False, 'tool_source_sha256': osu.digest(ROOT / 'run.py')}
    print('MPI_RMA_DIR=' + str(out), flush=True)
    try:
        commands = {mode: osu.saved_spec(base.BASELINE_LAB / 'osu_minimal8_ff_14ns8' /
                                        (mode + '_run.json'), case / 'install') for mode in ('rm', 'rc')}
        report['preflight'] = preflight(case, out, commands)
        print('预检=PASS 双端产品/OSU/HMPI组件/探针编译；最高PPN1；旧门禁按同产品检查点保留', flush=True)
        if args.preflight_only:
            report['status'] = 'PREFLIGHT_PASS_NO_TWO_NODE_MPI_RUN'
            return
        evidence = report['preflight']
        for name, mode, sync, threshold in (
                ('rc_fence', 'rc', 'fence', '1024'),
                ('rm_fence_bcopy', 'rm', 'fence', 'inf'),
                ('rm_fence_zcopy', 'rm', 'fence', '1024'),
                ('rm_software_atomics', 'rm', 'atomics', '1024'),
                ('rm_lock_all', 'rm', 'lock_all', '1024')):
            report['trials'].append(trial(case, out, commands, evidence, name, mode,
                                          out / 'mpi_probe', [sync], args,
                                          probe=sync, zcopy=threshold))
            write(out / 'results.json', report)
        # Diagnostic rounds establish original benchmark execution and native path.
        # OSU 7.5.2 one-sided benchmarks reject --validation; never pass -c.
        for size in (8, 4096, 65536):
            for benchmark in BENCHMARKS:
                argv = ['-m', '%d:%d' % (size, size), '-i', '20', '-x', '5', '-s', 'fence', '-w', 'create']
                for mode in ('rc', 'rm'):
                    name = '%s_%s_b%d_path' % (benchmark, mode, size)
                    report['trials'].append(trial(case, out, commands, evidence, name, mode,
                        Path(evidence['benchmarks'][benchmark]['path']), argv, args,
                        benchmark=benchmark, size=size))
                    write(out / 'results.json', report)
        report['status'] = 'MPI_SELECTED_NORMAL_AND_OSU_NATIVE_PATH_PASS_TIMING_NOT_RUN'
        if args.timing:
            for repeat in range(3):
                for size in (8, 4096, 65536):
                    for benchmark in BENCHMARKS:
                        argv = ['-m', '%d:%d' % (size, size), '-i', '1000', '-x', '100', '-s', 'fence', '-w', 'create']
                        # Alternate mode order to retain time/order effects in raw results.
                        modes = ('rc', 'rm') if repeat % 2 == 0 else ('rm', 'rc')
                        for mode in modes:
                            name = '%s_%s_b%d_timing%d' % (benchmark, mode, size, repeat)
                            report['trials'].append(trial(case, out, commands, evidence, name, mode,
                                Path(evidence['benchmarks'][benchmark]['path']), argv, args,
                                diagnostic=False, benchmark=benchmark, size=size))
                            write(out / 'results.json', report)
            report['status'] = 'MPI_SELECTED_NORMAL_OSU_NATIVE_PATH_AND_THREE_TIMINGS_PASS_REVIEW_REQUIRED'
            import statistics
            lines = ['# 原版OSU单边读写样本', '',
                     '| 用例 | 字节 | RC中位数 | RM中位数 | 单位 |',
                     '| --- | ---: | ---: | ---: | --- |']
            for size in (8, 4096, 65536):
                for benchmark, (_, _, metric, _) in BENCHMARKS.items():
                    medians = []
                    for mode in ('rc', 'rm'):
                        values = [row['output'][metric] for row in report['trials']
                                  if row['benchmark'] == benchmark and row['size'] == size
                                  and row['mode'] == mode and not row['diagnostic']]
                        if len(values) != 3:
                            raise ValueError('incomplete timing samples')
                        medians.append(statistics.median(values))
                    lines.append('| %s | %d | %.3f | %.3f | %s |' %
                                 (benchmark, size, *medians, 'us' if metric == 'latency_us' else 'MB/s'))
            lines += ['', '两节点各一rank；fence/create；诊断和计时分轮。全部样本、异常及回退见results.json。',
                      '原版单边OSU无内置数据校验；正确性来自先行MPI guard专项；不代表全部同步方式或完整B4验收。']
            (out / 'comparison.md').write_text('\n'.join(lines) + '\n')
        print('所列MPI窗口及原版OSU路径=PASS；产品与默认开关未改，完整B4/性能审阅/C仍待', flush=True)
    except BaseException as exc:
        report['status'] = 'STOPPED_INCOMPLETE'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        print('MPI_RMA_STOPPED=' + report['error'][:700], flush=True)
        raise SystemExit(1)
    finally:
        write(out / 'results.json', report)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
