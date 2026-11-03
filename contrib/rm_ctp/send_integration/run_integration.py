#!/usr/bin/env python3
"""SEND-only integration on the accepted 97b install; never rebuild transport."""
import argparse
from contextlib import contextmanager
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
import uuid

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
EXPERIMENT = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-c-send-reliability-dev')
DEFAULT_CASE = EXPERIMENT / 'r1_shared_97b503287_f1PZta'
sys.path[:0] = [str(ROOT), str(PROJECT / 'osu_compare'), str(PROJECT / 'wr_probe'),
                str(PROJECT / 'segment_default'), str(PROJECT / 'boundary'),
                str(PROJECT / 'am_rndv')]
import matrix
import run as osu
import run_probe as wr_probe
import policy
import boundary_config
import protocol as am_protocol
import split_logs


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            hasher.update(block)
    return hasher.hexdigest()


def validate_case(case):
    case = Path(case).resolve()
    if case != DEFAULT_CASE:
        raise ValueError('use the fixed accepted 97b CASE; do not guess another build')
    build = json.loads((case / 'build_manifest.json').read_text())
    module = case / 'install/lib/ucx/libuct_ub.so.0.0.0'
    if (build.get('source_commit') != matrix.COMMIT or
            build.get('module_sha256') != matrix.MODULE or digest(module) != matrix.MODULE or
            Path(build['case_dir']).resolve() != case):
        raise ValueError('tested source/module/build-directory identity differs')
    manifest = json.loads((ROOT / 'product_identity.json').read_text())
    if manifest['source_commit'] != matrix.COMMIT or len(manifest['files']) != 15:
        raise ValueError('pinned product source identity is incomplete')
    for name, expected in manifest['files'].items():
        if not name.startswith('src/') or digest(case / 'source' / name) != expected:
            raise ValueError('tested transport source changed: ' + name)
    return build


def runtime_environment(saved):
    combined = dict(os.environ)
    combined.update(saved)
    result = {key: value for key, value in combined.items()
              if not key.startswith(('UCX_', 'MPI_UCX_', 'HUCX_SEND_', 'HUCX_OSU_'))
              and key not in ('LD_PRELOAD', 'LD_DEBUG', 'RM_RESOURCE_OBSERVER_DIR', 'A89_RUN_NONCE')}
    result.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1',
                  OMPI_MCA_pml=osu.PML, OMPI_MCA_coll=osu.COLL,
                  OMPI_MCA_coll_tuned_use_dynamic_rules='0')
    return result


def effective_environment(case, ppn, saved, wr_library=None, injection=False, path='eager', run=None):
    env = {key: value for key, value in runtime_environment(saved).items()
           if key.startswith(('OMPI_', 'OPAL_')) or key in ('LD_LIBRARY_PATH', 'PATH')}
    runtime_path = env.get('LD_LIBRARY_PATH', '')
    env.update(matrix.reliable_environment(case, ppn, injection, path))
    env['LD_LIBRARY_PATH'] = ':'.join(dict.fromkeys(
        env['LD_LIBRARY_PATH'].split(':') + runtime_path.split(':'))).rstrip(':')
    preloads = [str(wr_library)] if wr_library else []
    if injection:
        if wr_library is None:
            raise ValueError('injection requires the paired WR observer')
        preloads.insert(0, str(wr_library.with_name('libsend_drop.so')))
        env['HUCX_SEND_TEST_MODULE'] = str(case / 'install/lib/ucx/libuct_ub.so.0.0.0')
    preloads += [str(case / 'install/lib' / ('lib' + name + '.so.0'))
                 for name in ('ucp', 'uct', 'ucs', 'ucm')]
    env['LD_PRELOAD'] = ':'.join(preloads)
    env['UCX_LOG_LEVEL'] = 'debug' if injection else ('req' if path == 'am_rndv' else 'warn')
    if run is not None:
        env.update(UCX_LOG_FILE=str(run / 'ucx_%h_%p.log'),
                   UCX_LOG_FILE_SIZE='inf', UCX_LOG_FILE_ROTATE='0')
    return env


def capture_configuration(install, env, directory, mode):
    if mode != 'iface' or install != osu.DEFAULT_CASE / 'install':
        raise ValueError('reused OSU runner has an unbound candidate/mode')
    local = boundary_config.query_local(install, env)
    # Do not require the tool source tree on the peer: execute the exact same
    # small query implementation, passed through stdin by the existing SSH helper.
    query = ('import json,os,subprocess,sys\n'
             'env={k:v for k,v in os.environ.items() if not k.startswith(("UCX_","MPI_UCX_"))}\n'
             'env.update(json.loads(sys.argv[2])); result={}\n'
             'for name,argv in (("config",[sys.argv[1]+"/bin/ucx_info","-c"]),'
             '("device",[sys.argv[1]+"/bin/ucx_info","-d","-t","rm_urma"])):\n'
             ' p=subprocess.run(argv,env=env,text=True,capture_output=True,timeout=40)\n'
             ' if p.returncode: raise RuntimeError(name+":"+p.stderr[-400:])\n'
             ' result[name]=p.stdout\n'
             'print(json.dumps(result))\n')
    remote = json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3"\n', query,
                                     install, json.dumps(env), timeout=100))
    checked = {}
    for host, output in (('node89', local), ('node97', remote)):
        (directory / (host + '_ucx_config.txt')).write_text(output['config'])
        (directory / (host + '_ucx_device.txt')).write_text(output['device'])
        checked[host] = matrix.check_config(output['config'], env, boundary_config.bytes_value)
        capability = boundary_config.parse_capability(output['device'])
        if capability != matrix.AM_BCOPY:
            raise ValueError('reliable AM_BCOPY differs: ' + host)
        if re.search(r'^\s*#?\s*(?:put_short|put_bcopy|put_zcopy|get_bcopy|get_zcopy|am_zcopy)\s*:',
                     output['device'], re.M):
            raise ValueError('native RMA/AM_ZCOPY unexpectedly advertised in reliable mode')
    osu.base.write_json(directory / 'effective_configuration.json', checked)
    return checked


@contextmanager
def bind_runner(case, build):
    previous_config = osu.base.parsed_config
    with policy.bind_candidate(osu, case, build):
        osu.base.parsed_config = capture_configuration
        try:
            yield
        finally:
            osu.base.parsed_config = previous_config


@contextmanager
def reliable_osu_profile(case, ppn, wr_library, run, path='eager'):
    previous = osu.environment

    def environment(saved, mode, install, configuration):
        # one_trial calls the existing interface with 'iface', not 'rm'.
        if mode != 'iface' or install != case / 'install' or configuration != osu.budget(ppn):
            raise ValueError('unexpected OSU mode/install/budget')
        return effective_environment(case, ppn, saved, wr_library, path=path, run=run)

    osu.environment = environment
    try:
        yield
    finally:
        osu.environment = previous


def copy_checked(paths, target):
    osu.base.copy_remote(paths, str(target) + '/')
    for source in paths:
        path = Path(target) / Path(source).name
        if osu.base.ssh('sha256sum "$1"\n', path).split()[0] != digest(source):
            raise ValueError('peer tool copy differs: ' + path.name)


def compile_helpers(case, out, commands):
    mpi = next(Path(v) for v in commands['rm'][0] if Path(v).name in ('mpirun', 'mpiexec'))
    mpicc = str(mpi.with_name('mpicc'))
    sources = {'mpi_probe': ROOT / 'mpi_probe.c', 'injector': ROOT / 'drop_receive_mpi.c',
               'wr_probe': PROJECT / 'wr_probe/send_probe.c'}
    files = {'mpi_probe': out / 'mpi_probe', 'injector': out / 'libsend_drop.so',
             'wr_probe': out / 'libsend_wr_probe.so'}
    jobs = {
        'mpi_probe': [mpicc, '-std=c99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                      str(sources['mpi_probe']), '-o', str(files['mpi_probe'])],
        'injector': ['gcc', '-std=gnu99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                     '-fPIC', '-shared', str(sources['injector']), '-ldl', '-o', str(files['injector'])],
        'wr_probe': ['gcc', '-std=gnu11', '-O2', '-g', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                     '-I/usr/include/ub/umdk/urma', str(sources['wr_probe']), '-o',
                     str(files['wr_probe']), '-ldl', '-pthread']}
    env = runtime_environment(commands['rm'][1])
    for name, command in jobs.items():
        osu.base.run_logged(command, env, out / ('compile_' + name + '.log'), 90)
    # Both nodes have the same architecture, HMPI and UCX libraries (preflight).
    # Compile once, copy exact bytes, then verify hashes and runtime dependencies.
    # Comparing independent -g builds from different source paths is invalid.
    copy_checked(list(files.values()), out)
    for name, path in files.items():
        local = subprocess.check_output(['ldd', str(path)], env=env, text=True, timeout=20)
        script = ('import os,subprocess,json,sys\n'
                  'e=dict(os.environ);e.update(json.loads(sys.argv[2]))\n'
                  'p=subprocess.run(["ldd",sys.argv[1]],env=e,text=True,capture_output=True,timeout=20)\n'
                  'print(p.stdout+p.stderr);raise SystemExit(p.returncode)\n')
        peer = osu.base.ssh('python3 -c "$1" "$2" "$3"\n', script, path,
                            json.dumps({key: value for key, value in env.items()
                                        if key.startswith(('LD_', 'OMPI_', 'OPAL_')) or key == 'PATH'}), timeout=30)
        (out / (name + '_ldd_node89.txt')).write_text(local)
        (out / (name + '_ldd_node97.txt')).write_text(peer)
        if 'not found' in local + peer:
            raise ValueError('helper dependency missing: ' + name)
    return files


def fetch_peer_json(run, names):
    code = ('import json,pathlib,sys\nroot=pathlib.Path(sys.argv[1]);result={}\n'
            'for name in json.loads(sys.argv[2]):\n'
            ' p=root/name\n'
            ' if p.is_symlink() or not p.is_file() or p.stat().st_size>8388608: '
            'raise ValueError("missing/nonregular/oversized evidence: "+name)\n'
            ' result[name]=p.read_text()\n'
            'print(json.dumps(result))\n')
    values = json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3"\n', code,
                                     run, json.dumps(names), timeout=60))
    if set(values) != set(names):
        raise ValueError('peer evidence file coverage mismatch')
    for name, value in values.items():
        target = run / name
        if target.exists():
            raise ValueError('peer evidence would overwrite existing file: ' + name)
        target.write_text(value)


def collect_diagnostics(out, row, evidence, injection=False, am_case=None):
    retries = {rank: 0 for rank in range(2 * row['ppn'])}
    identity_pattern = re.compile(r'\[([A-Za-z0-9_.-]+):(\d+)\s*:\d+\]')
    identities = {rank: {'host': evidence['inventory'][rank // row['ppn']]['hostname'], 'pid': pid}
                  for rank, pid in row['rank_evidence']['pids'].items()}
    by_process = {(value['host'], value['pid']): rank for rank, value in identities.items()}

    def parse(lines):
        def checked_lines():
            for line in lines:
                match = identity_pattern.search(line)
                if match and 'RM reliable retry peer=' in line:
                    rank = by_process.get((match[1], int(match[2])))
                    if rank is not None:
                        retries[rank] += 1
                yield line
        iterator = checked_lines()
        if am_case is not None:
            return am_protocol.inspect_trace(iterator, am_case, identities)
        for _ in iterator:
            pass
        return {'status': 'PASS', 'scope': 'complete application logs checked'}

    result, receipts = split_logs.collect(out, row, evidence, osu.base, parse_lines=parse)
    row['diagnostic_log_files'] = receipts
    if am_case is not None:
        row['protocol_path'] = result
    if injection:
        if any(value <= 0 for value in retries.values()):
            raise ValueError('actual application production retry posts missing on one side')
        row['production_retransmit_posts_by_rank'] = retries
        row['production_retransmit_posts'] = sum(retries.values())


def run_osu(case, out, commands, evidence, helpers, ppn, size, index, path):
    name = 'osu_multi_lat_rm_p%d_validation_%d' % (ppn, index)
    if size != 8:
        name += '_b%d' % size
    run = out / name
    arguments = argparse.Namespace(iterations=20, warmup=2, timeout=240)
    row = {'name': name, 'ppn': ppn, 'passed': False, 'path_profile': path}
    try:
        with reliable_osu_profile(case, ppn, helpers['wr_probe'], run, path):
            row = osu.one_trial(out, commands, evidence, 'osu_multi_lat', 'rm', ppn,
                                'validation', index, arguments, message_bytes=size)
        if not row['passed'] or not row.get('output', {}).get('validation_pass'):
            raise ValueError(row.get('error', 'standard OSU validation failed'))
        wr_probe.fetch_peer_summaries(run, row)
        row['send_observations'] = wr_probe.inspect_trial(run, row, helpers['wr_probe'])
        if path == 'am_rndv':
            am_case = {'benchmark': 'osu_multi_lat', 'mode': 'rm', 'ppn': ppn,
                       'sizes': (size,), 'threshold': 16384,
                       'path': 'am_rndv' if size >= 16384 else 'eager'}
            collect_diagnostics(out, row, evidence, am_case=am_case)
        row['path_profile'] = path
    except Exception as exc:
        row['passed'] = False
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    run.mkdir(exist_ok=True)
    osu.base.write_json(run / 'trial.json', row)
    return row


def cleanup_trial(out, config_path, row):
    try:
        local = subprocess.check_output([sys.executable, str(out / 'osu_rank.py'),
                                         str(config_path), '--cleanup'], text=True, timeout=15)
        peer = osu.base.ssh('python3 "$1" "$2" --cleanup\n', out / 'osu_rank.py', config_path, timeout=20)
        row['cleanup'] = {'local': json.loads(local), 'peer': json.loads(peer)}
        if any(value['terminated_own_pids'] for value in row['cleanup'].values()):
            row['passed'] = False
            row['error'] = row.get('error', 'application processes remained after launcher exit')
    except Exception as exc:
        row['passed'] = False
        row['cleanup_error'] = type(exc).__name__ + ': ' + str(exc)


def run_custom(case, out, commands, evidence, helpers, ppn, injection):
    name = 'mpi_alternating_p%d_%s' % (ppn, 'injection' if injection else 'normal')
    run = out / name
    run.mkdir()
    osu.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', run)
    env = effective_environment(case, ppn, commands['rm'][1], helpers['wr_probe'], injection, run=run)
    env['HUCX_OSU_PROBE_DIR'] = str(run)
    env['LD_PRELOAD'] += ':' + str(out / 'libosu_identity.so')
    nonce = uuid.uuid4().hex
    config = {'nonce': nonce, 'run': str(run), 'environment': env,
              'resource_probe': False, 'binary': str(helpers['mpi_probe']),
              'arguments': ['--injection'] if injection else [], 'timeout': 240, 'observers': {}}
    config_path = run / 'rank_config.json'
    osu.base.write_json(config_path, config)
    row = {'name': name, 'ppn': ppn, 'injection': injection, 'passed': False, 'configuration': config}
    try:
        capture_configuration(case / 'install', env, run, 'iface')
        copy_checked([config_path], run)
        command = osu.fixed_mca(osu.launcher(commands['rm'][0], ppn, osu.base.HOSTS,
                                             out / 'osu_rank.py', config_path))
        row['command'] = command
        row.update(osu.base.run_logged(command, runtime_environment(commands['rm'][1]), run / 'launcher.log', 260))
        names = []
        for rank in range(ppn, 2 * ppn):
            names += ['rank%d.json' % rank, 'pid_rank%d.json' % rank, 'mpi_rank%d.json' % rank,
                      'rank%d_after_init.maps' % rank, 'rank%d_before_finalize.maps' % rank]
        fetch_peer_json(run, names)
        row['rank_evidence'] = osu.inspect_ranks(run, ppn, evidence['inventory'], case / 'install',
                                                nonce, evidence['benchmarks']['osu_multi_lat']['mpi_library'])
        pids = row['rank_evidence']['pids']
        probes = [json.loads((run / ('mpi_rank%d.json' % rank)).read_text()) for rank in range(2 * ppn)]
        row['mpi_content'] = matrix.inspect_probe(probes, ppn, pids, nonce, injection)
        wr_probe.fetch_peer_summaries(run, row)
        row['send_observations'] = wr_probe.inspect_trial(run, row, helpers['wr_probe'])
        if max(value['max_send_bytes'] for value in row['send_observations']) != 4096:
            raise ValueError('custom probe did not exercise full-length reliable SEND')
        if injection:
            peer_names = ['inject_rank%d_pid%d.json' % (rank, pids[rank]) for rank in range(ppn, 2 * ppn)]
            fetch_peer_json(run, peer_names)
            faults = [json.loads((run / ('inject_rank%d_pid%d.json' % (rank, pids[rank]))).read_text())
                      for rank in range(2 * ppn)]
            row['software_injection'] = matrix.inspect_injection(faults, pids, nonce)
            collect_diagnostics(out, row, evidence, injection=True)
        row['passed'] = True
    except Exception as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        cleanup_trial(out, config_path, row)
        osu.base.write_json(run / 'trial.json', row)
    return row


def execute(case, out, build, run_requested):
    report = {'plan': matrix.plan(), 'source_commit': matrix.COMMIT, 'module_sha256': matrix.MODULE,
              'status': 'PREFLIGHT', 'trials': [], 'transport_changed': False, 'performance_accepted': False}

    def save():
        osu.base.write_json(out / 'results.json', report)

    def accept(row):
        report['trials'].append(row)
        save()
        if not row['passed']:
            raise RuntimeError(row['name'] + ': ' + row.get('error', row.get('cleanup_error', 'failed')))

    save()
    with bind_runner(case, build):
        try:
            osu.validate_package()
            commands = {'rm': osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8/rm_run.json', case / 'install')}
            evidence = osu.preflight(out, commands)
            report['preflight'] = evidence
            probe_env = effective_environment(case, 1, commands['rm'][1])
            report['capability_and_config'] = capture_configuration(case / 'install', probe_env, out, 'iface')
            osu.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
            helpers = compile_helpers(case, out, commands)
            shutil.copy2(osu.ROOT / 'osu_rank.py', out / 'osu_rank.py')
            shutil.copy2(osu.A89 / 'rank_exec.py', out / 'a89_rank_exec.py')
            copy_checked([out / 'osu_rank.py', out / 'a89_rank_exec.py', out / 'libosu_identity.so'], out)
            report['helpers_sha256'] = {name: digest(path) for name, path in helpers.items()}
            report['status'] = 'PREFLIGHT_PASS'
            save()
            print('预检=PASS 固定97b双端库/有效配置/AM_BCOPY3989/单次SEND4096；最高PPN4', flush=True)
            if not run_requested:
                print('本次没有启动双节点MPI；正式运行需 --run', flush=True)
                return report
            for ppn in matrix.PPNS:
                for size in matrix.SIZES:
                    row = run_osu(case, out, commands, evidence, helpers, ppn, size, len(report['trials']), 'eager')
                    accept(row)
                    print('OSU_MULTI_LAT EAGER PPN=%d SIZE=%d DATA/MAPS/WR/EXIT=PASS' % (ppn, size), flush=True)
                row = run_custom(case, out, commands, evidence, helpers, ppn, False)
                accept(row)
                print('MPI大小交替 PPN=%d 双方向/计数/内容/顺序/完整guard/退出=PASS' % ppn, flush=True)
            for ppn, size in matrix.AM_CASES:
                row = run_osu(case, out, commands, evidence, helpers, ppn, size, len(report['trials']), 'am_rndv')
                accept(row)
                print('OSU_MULTI_LAT AM切换 PPN=%d SIZE=%d DATA/MAPS/WR/ACTUAL_PATH/EXIT=PASS' % (ppn, size), flush=True)
            row = run_custom(case, out, commands, evidence, helpers, 1, True)
            accept(row)
            report['status'] = 'SELECTED_RELIABLE_SEND_INTEGRATION_PASS'
            report['production_retry_posts'] = row['production_retransmit_posts']
            save()
            print('软件丢内部片/丢ACK/重复交付译码=PASS；不是硬件丢包验收', flush=True)
            print('所选SEND集成=%d/%d PASS；性能及完整C/D待验' % (len(report['trials']), matrix.plan()['total_mpi_tasks']), flush=True)
            return report
        except BaseException as exc:
            report['status'] = 'STOPPED_INCOMPLETE'
            report['error'] = type(exc).__name__ + ': ' + str(exc)
            save()
            raise


def main():
    parser = argparse.ArgumentParser(description='reliable_v3 SEND集成；默认仅计划，40项任务，最高PPN4')
    parser.add_argument('case', type=Path, nargs='?', default=DEFAULT_CASE)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    if not (args.run or args.preflight_only):
        print(json.dumps(matrix.plan(), ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may explicitly execute')
    case = args.case.resolve()
    build = validate_case(case)
    out = Path(tempfile.mkdtemp(prefix='send_integration_fixed_', dir=case))
    print('SEND_INTEGRATION_DIR=' + str(out), flush=True)
    try:
        execute(case, out, build, args.run)
    except Exception as exc:
        print('SEND_INTEGRATION_STOPPED=' + type(exc).__name__ + ': ' + str(exc)[:900], flush=True)
        raise SystemExit(1)
    finally:
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
