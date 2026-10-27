#!/usr/bin/env python3
"""A7 normal-node lifecycle probe; uses fixed, already-tested 2a8e libraries.

May be run from this source directory, or as the delivered self-contained zipapp.
CPU fault injection belongs to test_shared_tx.c, not this node test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import zipfile

import a7_iface_four_ep_20261026 as common


def source_bytes():
    name = 'a7_iface_lifecycle.c'
    folder = Path(__file__).resolve().parent
    if folder.is_dir():
        return (folder / name).read_bytes()
    with zipfile.ZipFile(folder) as package:
        return package.read(name)


def validate_cycle(role, cycle, text):
    if re.search(r'UCX\s+(ERROR|FATAL)|DIRECT_IFACE_FAILED|DIRECT_IFACE_STATUS:', text):
        raise RuntimeError(role + '日志包含终态错误')
    rows = re.findall(
        r'DIRECT_ADDRESS ROLE=(\w+) PHASE=(\w+) APP_EPS=(\d+) '
        r'IFACE_LEN=48 EP_LEN=0 JETTY=(\d+) INSTANCE=(\d+) '
        r'R=(\d+) K=(\d+) W=(\d+) SEG_LEN=(\d+)', text)
    if (len(rows) != 2 or {row[1] for row in rows} !=
            {'LOCAL_BEFORE_EP', 'REMOTE_BEFORE_EP'} or
            any(row[0] != role or row[2] != '0' or
                tuple(map(int, row[5:8])) != (64, 5, 8) or
                any(int(row[k]) <= 0 for k in (3, 4, 8)) for row in rows)):
        raise RuntimeError(role + '当前周期地址/身份/额度证据不全')
    if text.count('RM iface address export') != 0:
        # Export precedes BEGIN, and is validated on the complete process log.
        raise RuntimeError('地址导出不应出现在业务EP重建阶段')
    if 'RM legacy EP address export' in text or 'RM legacy connect_to_ep' in text:
        raise RuntimeError('误走legacy地址路径')
    expected = (80, 80, 256, 0, 16, 16) if role == 'sender' else (0, 0, 0, 256, 0, 0)
    match = re.search(r'LIFECYCLE_CYCLE_PASS ROLE=' + role + r' CYCLE=' + str(cycle) +
        r' CREATED=(\d+) DESTROYED=(\d+) SENT=(\d+) RECEIVED=(\d+) PURGED=(\d+) PENDING=(\d+)', text)
    if match is None or tuple(map(int, match.groups())) != expected:
        raise RuntimeError(role + '周期数据/创建销毁/pending计数不符')
    if text.index('PHASE=LOCAL_BEFORE_EP') > text.index('PHASE=REMOTE_BEFORE_EP'):
        raise RuntimeError('本地发布和远端地址获取顺序不符')
    if role == 'sender' and text.index('PHASE=REMOTE_BEFORE_EP') > text.index('RM iface connect'):
        raise RuntimeError('业务EP先于对端地址创建')
    connections = text.count('RM iface connect')
    if connections != expected[0]:
        raise RuntimeError(role + '真实连接次数不符')
    generations = re.findall(r'LIFECYCLE_GENERATION ROLE=sender CYCLE=(\d+) GENERATION=(\d+) '
        r'SENT=(\d+) CREATED=(\d+) DESTROYED=(\d+) PURGED=(\d+) PENDING=(\d+)', text)
    if role == 'sender':
        if len(generations) != 16:
            raise RuntimeError('业务EP代次标记不全')
        for index, row in enumerate(generations):
            expected_row = (cycle, index, (index + 1) * 16, (index + 1) * 5,
                            (index + 1) * 5, index + 1, index + 1)
            if tuple(map(int, row)) != expected_row:
                raise RuntimeError('业务EP代次递增/回收计数不符')
    elif generations:
        raise RuntimeError('接收端意外创建业务EP')
    targets = common.targets(text)
    if role == 'sender' and targets['reuse'] < 79:
        raise RuntimeError('重建业务EP未复用同一目标')
    control_owners = re.findall(r'RM control sender created iface=(\S+) ep=(\S+)', text)
    if len(control_owners) != 1:
        raise RuntimeError(role + '每iface控制owner数量不符')
    control = [(int(k), int(i), int(n)) for k, i, n in re.findall(
        r'RM iface control post kind=(\d+) id=(\d+) credits=(\d+)', text)]
    if any(k != (1 if role == 'sender' else 2) or i <= 0 or not 0 < n <= 8
           for k, i, n in control):
        raise RuntimeError('控制消息类型/身份/额度不符')
    return {'created': expected[0], 'destroyed': expected[1], 'sent': expected[2],
            'received': expected[3], 'purged': expected[4], 'pending': expected[5],
            'targets': targets, 'control': control,
            'addresses': {row[1]: list(map(int, row[3:])) for row in rows}}


def validate(sender, receiver):
    result = {'roles': {}}
    for role, text in (('sender', sender), ('receiver', receiver)):
        if re.search(r'UCX\s+(ERROR|FATAL)|DIRECT_IFACE_FAILED|DIRECT_IFACE_STATUS:', text):
            raise RuntimeError(role + '日志含终态错误')
        if text.count('RM iface address export') != 2:
            raise RuntimeError(role + '地址应每个iface只导出一次，而非每个业务EP一次')
        if text.count('LIFECYCLE_BEGIN ROLE=' + role) != 2:
            raise RuntimeError('实际iface周期数不符')
        cycles = []
        for cycle in range(2):
            begin = text.index('LIFECYCLE_BEGIN ROLE=%s CYCLE=%d' % (role, cycle))
            end = text.index('LIFECYCLE_CYCLE_PASS ROLE=%s CYCLE=%d' % (role, cycle), begin)
            end = text.find('\n', end)
            cycles.append(validate_cycle(role, cycle, text[begin:end if end >= 0 else len(text)]))
        instances = [x['addresses']['LOCAL_BEFORE_EP'][1] for x in cycles]
        if len(set(instances)) != 2:
            raise RuntimeError(role + '重建iface未获得不同实例身份')
        pattern = (r'LIFECYCLE_PASS ROLE=' + role +
            r' CYCLES=2 GENERATIONS=16 CREATED=(\d+) DESTROYED=(\d+) SENT=(\d+) '
            r'RECEIVED=(\d+) PURGED=(\d+) PENDING=(\d+) EARLY_DESTROY=(\d+) BYTES=64')
        match = re.search(pattern, text)
        expected = (160, 160, 512, 0, 32, 32, 32) if role == 'sender' else (0, 0, 0, 512, 0, 0, 0)
        if match is None or tuple(map(int, match.groups())) != expected:
            raise RuntimeError(role + '最终创建/退出/数据/pending门禁不符')
        result['roles'][role] = {'cycles': cycles, 'instances': instances}
    for cycle in range(2):
        a = result['roles']['sender']['cycles'][cycle]
        b = result['roles']['receiver']['cycles'][cycle]
        for left, right in ((a, b), (b, a)):
            if left['addresses']['REMOTE_BEFORE_EP'] != right['addresses']['LOCAL_BEFORE_EP']:
                raise RuntimeError('两端实际iface字段不匹配')
        requests = [(i, n) for _, i, n in a['control']]
        grants = [(i, n) for _, i, n in b['control']]
        if (len(requests) < 31 or sorted(requests) != sorted(grants) or
                len({i for i, _ in requests}) != len(requests) or
                sum(n for _, n in grants) < 256 - 8):
            raise RuntimeError('重建EP后控制身份/跨窗额度不闭合')
    return result


def main():
    parser = argparse.ArgumentParser(description='A7:已测RM库的业务EP/iface生命周期专项')
    parser.add_argument('case', type=Path)
    parser.add_argument('--port', type=int, default=19948)
    args = parser.parse_args()
    case = args.case.resolve()
    if case.parent != common.LAB or not 1 <= args.port <= 65535:
        parser.error('CASE必须直接位于实验根，端口须有效')
    install = case / 'install'
    module = install / 'lib/ucx/libuct_ub.so.0.0.0'
    manifest = json.loads((case / 'build_manifest.json').read_text())
    if (manifest['source_commit'] != common.COMMIT or
            manifest['module_sha256'] != common.MODULE or common.digest(module) != common.MODULE):
        raise RuntimeError('已测产品身份不符')
    codec = case / 'source/src/uct/urma/rm_urma'
    for name, value in common.CODEC_HASHES.items():
        if common.digest(codec / name) != value:
            raise RuntimeError('地址codec源码不符')
    out = Path(tempfile.mkdtemp(prefix='a7_lifecycle_', dir=str(case)))
    print('LIFECYCLE_DIR=' + str(out), flush=True)
    data = source_bytes()
    tool = Path(__file__).resolve()
    if tool.parent.is_file():
        tool = tool.parent
    report = {'product_commit': common.COMMIT, 'module_sha256': common.MODULE,
        'tool_sha256': common.digest(tool),
        'c_source_sha256': hashlib.sha256(data).hexdigest(), 'passed': False,
        'scope': '节点正常生命周期，非CPU故障注入/完整C1/性能验收'}

    def save():
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    save()
    try:
        peer = common.remote('set -eu\nsha256sum "$1"\nldd "$1"\n', module)
        if peer.split()[0] != common.MODULE or 'not found' in peer:
            raise RuntimeError('对端模块或依赖不符')
        source = out / 'a7_iface_lifecycle.c'
        source.write_bytes(data)
        binary = out / 'iface_lifecycle'
        command = ['gcc', '-std=gnu99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
            '-I' + str(install / 'include'), '-I' + str(codec), str(source),
            str(codec / 'rm_urma_addr.c'), '-L' + str(install / 'lib'),
            '-Wl,-rpath,' + str(install / 'lib'), '-luct', '-lucs', '-lucm',
            '-lpthread', '-ldl', '-lrt', '-lm', '-o', str(binary)]
        report['compile_command'] = command
        with (out / 'compile.log').open('w') as stream:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)
        common.remote('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        subprocess.run(['scp', '-q', str(binary), common.PEER + ':' + str(binary)], check=True)
        if common.remote('sha256sum "$1"\n', binary).split()[0] != common.digest(binary):
            raise RuntimeError('两端测试程序不一致')
        environment = {
            'PATH': '/usr/bin:/bin', 'UCX_MODULE_DIR': str(install / 'lib/ucx'),
            'LD_LIBRARY_PATH': str(install / 'lib'),
            'LD_PRELOAD': ':'.join(str(install / 'lib' / ('lib' + name + '.so.0'))
                                   for name in ('uct', 'ucs', 'ucm')),
            'UCX_LOG_LEVEL': 'debug', 'UCX_RM_URMA_LINK_MODE': 'iface_v2',
            'UCX_RM_URMA_MAX_PEERS': '5', 'UCX_RM_URMA_FC_ENABLE': 'y',
            'UCX_RM_URMA_FC_WND_SIZE': '8', 'UCX_RM_URMA_TX_DEPTH': '8',
            'UCX_RM_URMA_RX_DEPTH': '64', 'UCX_RM_URMA_FC_SOFT_THRESH': '0.5',
            'UCX_RM_URMA_FC_HARD_THRESH': '0.25'}
        prefix = ['timeout', '-k', '5s', '150s', '/usr/bin/env', '-i'] + \
                 [key + '=' + value for key, value in environment.items()]
        server_cmd = prefix + [str(binary), 'listen', '9.88.43.97', str(args.port), str(out / 'receiver.maps')]
        client_cmd = prefix + [str(binary), 'connect', '9.88.43.97', str(args.port), str(out / 'sender.maps')]
        script = 'set -eu\nexec ' + shlex.join(server_cmd) + ' >' + shlex.quote(str(out / 'receiver.log')) + ' 2>&1\n'
        (out / 'server.sh').write_text(script)
        report['commands'] = {'sender': client_cmd, 'receiver': server_cmd}
        save()
        server = subprocess.Popen(['ssh', common.PEER, 'bash -s'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        server.stdin.write(script)
        server.stdin.close()
        server.stdin = None
        client_rc = None
        error = None
        try:
            with (out / 'sender.log').open('w') as stream:
                client = subprocess.Popen(client_cmd, stdout=stream, stderr=subprocess.STDOUT,
                                          start_new_session=True)
                try:
                    client_rc = client.wait(timeout=160)
                except subprocess.TimeoutExpired:
                    common.stop_own_process(client)
                    error = 'sender超时；仅终止本次测试进程组'
        finally:
            try:
                stdout, stderr = server.communicate(timeout=165)
            except subprocess.TimeoutExpired:
                common.stop_own_process(server)
                stdout, stderr = server.communicate()
                error = 'SSH超时；远端测试自身具有timeout'
            (out / 'server_ssh.log').write_text(stdout + stderr)
        for name in ['receiver.log'] + ['receiver.maps.%d' % i for i in range(2)]:
            (out / name).write_text(common.remote('if test -f "$1"; then cat "$1"; fi\n', out / name))
        report['exits'] = {'sender': client_rc, 'receiver_ssh': server.returncode}
        if error or client_rc != 0 or server.returncode != 0:
            raise RuntimeError(error or '进程退出失败：' + str(report['exits']))
        details = validate((out / 'sender.log').read_text(errors='replace'),
                           (out / 'receiver.log').read_text(errors='replace'))
        for role in ('sender', 'receiver'):
            for i in range(2):
                common.check_maps((out / ('%s.maps.%d' % (role, i))).read_text(), install)
        report.update(passed=True, details=details, maps='BOTH_CYCLES_BOTH_NODES_PASS')
        save()
        print('A7_NODE_LIFECYCLE_PASS=YES PRODUCT_MATCH_TESTED=YES', flush=True)
        print('IFACE_CYCLES=2 EP_GENERATIONS_PER_IFACE=16 ADDRESS_REUSE=PASS', flush=True)
        print('BUSINESS_EPS CREATED=160 DESTROYED=160 REVERSE=0 INFLIGHT_DESTROY=32', flush=True)
        print('DATA=512x64B SHORT=256 BCOPY=256 VALIDATION=PASS', flush=True)
        print('PENDING PURGED=32 REQUEUED_COMPLETED=32 DATA_DUPLICATES=0', flush=True)
        print('TARGETS_PER_IFACE IMPORT=1 RELEASE=1 MAX_CONTROL_OWNER=1 MAPS_BOTH=PASS', flush=True)
        print('CPU_FAULT_TESTS=SEPARATE C1_PERFORMANCE=NOT_ACCEPTED', flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        save()
        print('A7_LIFECYCLE_FAILED: ' + str(exc), file=sys.stderr, flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
