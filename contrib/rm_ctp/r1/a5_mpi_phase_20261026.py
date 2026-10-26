#!/usr/bin/env python3
"""Read existing HMPI/PML diagnostics alongside application phase markers.

A fixed two-rank diagnostic, not a performance test or a network packet trace.
No product/library instrumentation, hardware changes, or additional protocol.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import zipfile

import a7_iface_four_ep_20261026 as common


def c_source():
    folder = Path(__file__).resolve().parent
    if folder.is_dir():
        return (folder / 'a5_mpi_phase.c').read_bytes()
    with zipfile.ZipFile(folder) as archive:
        return archive.read('mpi_phase.c')


def inspect(rank, text):
    if re.search(r'UCX\s+(ERROR|FATAL)|MPI_PHASE_FAILED|MPI_ERR_|Caught signal|Assertion .*failed', text):
        raise RuntimeError('rank%d日志含错误' % rank)
    stages = ['BEFORE_MPI_INIT', 'AFTER_MPI_INIT', 'BEFORE_FIRST_DATA',
              'AFTER_FIRST_DATA', 'BEFORE_SECOND_DATA', 'AFTER_SECOND_DATA',
              'BEFORE_MPI_FINALIZE', 'AFTER_MPI_FINALIZE']
    markers = list(re.finditer(r'MPI_PHASE RANK=(\d+) PID=(\d+) STAGE=(\w+)', text))
    if [m[3] for m in markers] != stages or any(int(m[1]) != rank for m in markers):
        raise RuntimeError('rank%d阶段标记缺失/重复/顺序不符' % rank)
    boundaries = {m[3]: m.start() for m in markers}

    def phase(position):
        if boundaries['BEFORE_MPI_INIT'] < position < boundaries['AFTER_MPI_INIT']:
            return 'MPI_INIT'
        if boundaries['AFTER_MPI_INIT'] <= position < boundaries['BEFORE_FIRST_DATA']:
            return 'AFTER_INIT_BEFORE_DATA'
        if boundaries['BEFORE_FIRST_DATA'] <= position < boundaries['AFTER_FIRST_DATA']:
            return 'FIRST_DATA'
        if boundaries['BEFORE_SECOND_DATA'] <= position < boundaries['AFTER_SECOND_DATA']:
            return 'SECOND_DATA'
        return 'OTHER'

    packs = list(re.finditer(r'Pack (?:(?:local|remote) )?worker address, size (\d+)', text))
    if not packs or any(int(m[1]) == 0 or phase(m.start()) != 'MPI_INIT' for m in packs):
        raise RuntimeError('rank%d缺少MPI_Init内worker地址打包的既有HMPI日志' % rank)
    got = [m for m in re.finditer(r'Got proc (\d+) address, size (\d+)', text)
           if int(m[1]) == 1 - rank]
    connecting = [m for m in re.finditer(r'connecting to proc\. (\d+)', text)
                  if int(m[1]) == 1 - rank]
    if not got or not connecting or min(m.start() for m in got) > min(m.start() for m in connecting):
        raise RuntimeError('rank%d缺少获取对端地址后调用建链的既有HMPI日志' % rank)
    if any(int(m[2]) == 0 or phase(m.start()) not in ('MPI_INIT', 'AFTER_INIT_BEFORE_DATA', 'FIRST_DATA')
           for m in got):
        raise RuntimeError('对端地址获取阶段/长度不符')
    counts = {}
    for label, token in [('iface_exports', 'RM iface address export'),
                         ('iface_connects', 'RM iface connect')]:
        events = list(re.finditer(re.escape(token), text))
        if not events:
            raise RuntimeError('rank%d缺少%s' % (rank, token))
        counts[label] = {key: sum(phase(m.start()) == key for m in events)
                        for key in ('MPI_INIT', 'AFTER_INIT_BEFORE_DATA', 'FIRST_DATA', 'SECOND_DATA', 'OTHER')}
    if counts['iface_exports']['SECOND_DATA'] or any(phase(m.start()) == 'SECOND_DATA' for m in got):
        raise RuntimeError('第二轮通信出现新增地址导出/获取，不能判定复用通过')
    if 'RM legacy EP address export' in text or 'RM legacy connect_to_ep' in text:
        raise RuntimeError('新模式误走旧EP地址路径')
    if not re.search(r'tag\(rm_urma/', text):
        raise RuntimeError('缺少RM tag lane证据')
    ready = re.findall(r'RM iface_v2 ready .*? R=(\d+) K=(\d+) W=(\d+)', text)
    if not ready or any(tuple(map(int, row)) != (64, 5, 8) for row in ready):
        raise RuntimeError('实际模式与R64/K5/W8不符')
    if not re.search(r'MPI_PHASE_PASS RANK=%d ROUNDS=2 BYTES=8 VALIDATION=PASS' % rank, text):
        raise RuntimeError('rank%d数据/Finalize成功标记缺失' % rank)
    return {'rank': rank, 'worker_pack_phases': [phase(m.start()) for m in packs],
            'worker_address_lengths': [int(m[1]) for m in packs],
            'peer_address_get_phases': [phase(m.start()) for m in got],
            'peer_address_lengths': [int(m[2]) for m in got],
            'peer_connect_phases': [phase(m.start()) for m in connecting],
            'transport_api_counts_not_network_packets': counts,
            'second_round_address_reuse': True, 'data_validation': 'PASS'}


def main():
    parser = argparse.ArgumentParser(description='固定已测RM库的MPI地址发布/获取阶段取证')
    parser.add_argument('case', type=Path)
    args = parser.parse_args()
    case = args.case.resolve()
    if case.parent != common.LAB:
        parser.error('必须使用固定实验根下的CASE')
    install = case / 'install'
    module = install / 'lib/ucx/libuct_ub.so.0.0.0'
    manifest = json.loads((case / 'build_manifest.json').read_text())
    if manifest['source_commit'] != common.COMMIT or common.digest(module) != common.MODULE:
        raise RuntimeError('已测产品身份不符')
    source_root = case / 'source/contrib/rm_ctp/r1'
    hashes = {'accept.py': 'd3a50da339f7a84f092ae08f04ad6e526a27db20ac0c5cb5641bb2c7f5459ba6',
              'accept_iface.py': 'a6fd4eedec1aa42d75feef463f78b9520a668f75a2fb9fbdd568b5a5a16e9fe4'}
    for name, expected in hashes.items():
        if common.digest(source_root / name) != expected:
            raise RuntimeError('固定launcher辅助源码不符：' + name)
    sys.path.insert(0, str(source_root))
    import accept as base
    import accept_iface as iface
    command, environment, index = base.saved_spec('rm', install, common.LAB / 'osu_minimal8_ff_14ns8')
    mpi = next(Path(x) for x in command if Path(x).name in ('mpirun', 'mpiexec'))
    out = Path(tempfile.mkdtemp(prefix='a5_mpi_phase_', dir=str(case)))
    print('MPI_PHASE_DIR=' + str(out), flush=True)
    data = c_source()
    report = {'product_commit': common.COMMIT, 'module_sha256': common.MODULE,
              'source_sha256': hashlib.sha256(data).hexdigest(), 'passed': False,
              'scope': 'MPI阶段观测；非时延基准/网络包计数/CTP故障契约'}
    tool = Path(__file__).resolve()
    report['tool_sha256'] = common.digest(tool.parent if tool.parent.is_file() else tool)

    def save():
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    save()
    try:
        if common.remote('sha256sum "$1"\n', module).split()[0] != common.MODULE:
            raise RuntimeError('对端模块不符')
        source = out / 'mpi_phase.c'
        binary = out / 'mpi_phase'
        source.write_bytes(data)
        with (out / 'compile.log').open('w') as stream:
            subprocess.run([str(mpi.with_name('mpicc')), '-std=c99', '-O2', '-Wall', '-Wextra',
                            '-Werror', str(source), '-o', str(binary)], stdout=stream,
                           stderr=subprocess.STDOUT, check=True)
        wrapper = out / 'rank_wrapper.sh'
        wrapper.write_text('#!/bin/bash\nset -eu\n'
            'rank=${OMPI_COMM_WORLD_RANK:?missing MPI rank}\n'
            'case "$rank" in 0|1) ;; *) exit 2 ;; esac\n'
            'exec ' + shlex.quote(str(binary)) + ' ' + shlex.quote(str(out)) +
            ' >' + shlex.quote(str(out)) + '/rank"$rank".log 2>&1\n')
        wrapper.chmod(0o755)
        common.remote('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        subprocess.run(['scp', '-q', str(binary), str(wrapper), common.PEER + ':' + str(out) + '/'], check=True)
        peer_hashes = common.remote('sha256sum "$1" "$2"\n', binary, wrapper).splitlines()
        if [line.split()[0] for line in peer_hashes] != [common.digest(binary), common.digest(wrapper)]:
            raise RuntimeError('两端程序/启动脚本不一致')
        config = iface.values('iface', True)
        config.update(OMPI_MCA_pml_ucx_verbose='2', OMPI_MCA_opal_common_ucx_verbose='2')
        launch, environment = iface.configured(command[:index] + [str(wrapper)], environment, config)
        report['command'] = launch
        save()
        run_error = None
        try:
            run = base.run(launch, environment, out / 'launcher.log', timeout=90)
            report['launcher'] = run
        except Exception as exc:
            run_error = str(exc)
        for name in ('rank1.log', 'rank1.maps'):
            (out / name).write_text(common.remote('if test -f "$1"; then cat "$1"; fi\n', out / name))
        if run_error:
            raise RuntimeError(run_error)
        details = []
        for rank in range(2):
            text = (out / ('rank%d.log' % rank)).read_text(errors='replace')
            details.append(inspect(rank, text))
            maps = (out / ('rank%d.maps' % rank)).read_text()
            common.check_maps(maps, install)
            if not any(str(install / 'lib') + '/' in line and 'libucp' in line for line in maps.splitlines()):
                raise RuntimeError('实际UCP maps不是候选安装')
        report.update(passed=True, ranks=details, maps='BOTH_PASS')
        save()
        print('MPI_ADDRESS_PHASE_PASS=YES DATA_8B_TWO_ROUNDS=PASS MAPS_BOTH=PASS', flush=True)
        for row in details:
            print('RANK=%d PUBLISH=%s PEER_GET=%s CONNECT=%s' %
                  (row['rank'], row['worker_pack_phases'], row['peer_address_get_phases'], row['peer_connect_phases']), flush=True)
            print('RANK=%d API_COUNTS=%s' %
                  (row['rank'], json.dumps(row['transport_api_counts_not_network_packets'])), flush=True)
        print('SECOND_ROUND_ADDRESS_REUSE=PASS LEGACY_EP_CALLBACKS=0', flush=True)
        print('SCOPE=MPI_PHASE_ONLY NETWORK_PACKETS_AND_PERFORMANCE_NOT_MEASURED', flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        save()
        print('MPI_PHASE_FAILED: ' + str(exc), file=sys.stderr, flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
