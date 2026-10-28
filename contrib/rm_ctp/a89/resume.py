"""Bounded reuse of the one observed pre-observer-fix checkpoint.

Never promotes a failed resource probe. Old raw files stay read-only. Cached
unobserved timings retain their original run/configuration and provenance.
"""
import copy
import hashlib
import json
from pathlib import Path
import time

from support import (PRODUCT_COMMIT, PRODUCT_MODULE, PRE_IFACE, digest,
                     check_maps, summarize)

SOURCE_MANIFEST = 'fbe71a04c639a49513919a4a5b4a126ab3afc3e3566199cffe30c9d9048a6334'
SOURCE_DIRECTORY = 'a89_xtypmczp'


class CheckpointExpired(ValueError):
    """Age alone permits a fresh full run, never unchecked reuse."""



def manifest_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_checkpoint(path, case, current, toolkit, baseline, repeats):
    path = Path(path).resolve()
    case = Path(case).resolve()
    if path != case / SOURCE_DIRECTORY / 'results.json' or repeats != 3:
        raise ValueError('恢复仅支持本次a89_xtypmczp检查点及原三轮采样')
    age = time.time() - path.stat().st_mtime
    if age < 0 or age > 3600:
        raise CheckpointExpired('检查点已超过一小时，本轮完整重跑门禁和计时')
    previous = json.loads(path.read_text())
    if (previous['product_commit'] != PRODUCT_COMMIT or previous['module_sha256'] != PRODUCT_MODULE or
            manifest_digest(previous['toolkit']) != SOURCE_MANIFEST):
        raise ValueError('原检查点产品或工具来源不符')
    if previous['toolkit']['regression_files'] != toolkit['regression_files']:
        raise ValueError('回归工具已变更，不能复用旧结果')
    if previous['toolkit']['files']['mpi_scale.c'] != toolkit['files']['mpi_scale.c']:
        raise ValueError('基准程序已变更，不能复用原计时')
    for old, new in zip(previous['inventory'], current['inventory']):
        if any(old[key] != new[key] for key in ('hostname', 'boot_id', 'allowed_cpus')):
            raise ValueError('节点重启或CPU许可变化，不能复用原检查点')
    if len(previous['inventory']) != 2 or len(current['inventory']) != 2:
        raise ValueError('原检查点节点覆盖不完整')
    for mode in ('candidate', 'old_rm', 'old_minimal_control'):
        old = previous['installations'][mode]
        new = current['installations'][mode]
        if any(old[key] != new[key] for key in ('install', 'module_sha256', 'base_libraries_sha256')):
            raise ValueError('库身份变化，不能复用原结果')
    for kind in ('rm', 'rc'):
        if previous['saved_launcher_records_sha256'][kind] != digest(baseline / (kind + '_run.json')):
            raise ValueError('保存的MPI命令变化，不能复用')
    if not previous.get('error', '').endswith('A8资源门禁失败：iface_p1_r0_resources'):
        raise ValueError('不是已确认的PPN1观察器包装失败')
    expected_names = ('28项正常矩阵', '四EP无反向业务EP', '160EP生命周期', 'MPI地址阶段')
    markers = ('功能矩阵=通过', 'DIRECT_UCT_FOUR_EP_PASS=YES',
               'A7_NODE_LIFECYCLE_PASS=YES', 'MPI_ADDRESS_PHASE_PASS=YES')
    regressions = previous['regressions']
    if len(regressions) != 4:
        raise ValueError('原四组回归不完整')
    log_hashes = {}
    for index, (row, name, marker) in enumerate(zip(regressions, expected_names, markers)):
        log = path.parent / ('regression%d.log' % index)
        if (row['name'] != name or row['passed'] is not True or row['exit'] != 0 or
                Path(row['log']).resolve() != log or marker not in log.read_text(errors='replace')):
            raise ValueError('原回归日志或通过标记不符')
        log_hashes[log.name] = digest(log)
    binary = path.parent / 'mpi_scale'
    if digest(binary) != previous['binary_sha256']:
        raise ValueError('原基准二进制发生变化')
    expected = {'%s_p1_r%d_timing' % (mode, repeat)
                for mode in ('iface', 'old_rm', 'rc') for repeat in range(3)}
    timing = {}
    failed = []
    for row in previous['trials']:
        if row['resource_probe']:
            failed.append(copy.deepcopy(row))
            continue
        if row['name'] not in expected or row['name'] in timing or row['passed'] is not True:
            raise ValueError('原计时覆盖或通过状态不符')
        directory = path.parent / row['name']
        raw = json.loads((directory / 'trial.json').read_text())
        if raw != row or row['ppn'] != 1:
            raise ValueError('原计时trial.json不一致')
        if any(value['terminated_own_pids'] for value in row['cleanup'].values()):
            raise ValueError('原计时进程未正常退出')
        data = [json.loads((directory / ('rank%d.json' % rank)).read_text()) for rank in (0, 1)]
        if summarize(data, 1) != row['metrics']:
            raise ValueError('原计时rank数据不一致')
        install = PRE_IFACE if row['mode'] == 'old_rm' else case / 'install'
        for rank in (0, 1):
            for phase in ('after_init', 'after_first', 'after_steady'):
                check_maps((directory / ('rank%d_%s.maps' % (rank, phase))).read_text(), install)
        copied = copy.deepcopy(row)
        copied['reused_from_report'] = str(path)
        copied['source_report_sha256'] = digest(path)
        timing[row['name']] = copied
    if set(timing) != expected or len(failed) != 1 or failed[0]['passed'] is not False:
        raise ValueError('原样本不是九轮通过计时及一轮失败资源检查')
    if not failed[0].get('error', '').startswith('ValueError: missing Jetty/wrong depth/leak:'):
        raise ValueError('原资源失败类型不同，不能按包装缺陷恢复')
    if any(value['terminated_own_pids'] for value in failed[0]['cleanup'].values()):
        raise ValueError('原资源任务仍有退出异常，拒绝恢复')
    return {'timings': timing, 'regressions': copy.deepcopy(regressions),
            'binary': binary, 'compile_command': previous['compile_command'],
            'provenance': {'source_report': str(path), 'source_report_sha256': digest(path),
                           'source_toolkit_sha256': SOURCE_MANIFEST, 'age_seconds': age,
                           'regression_log_sha256': log_hashes,
                           'original_error': previous['error'], 'failed_trials_preserved': failed,
                           'scope': '同一检查点通过的四组回归与九个无观察器计时；资源轮全部重跑'}}
