#!/usr/bin/env python3
"""Reliable SEND collectives, bounded exits, original OSU timing/resources."""
import argparse
from contextlib import contextmanager
import csv
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'send_integration'))
import run_integration as integration
sys.path.insert(0, str(ROOT))
import validation_policy as policy
import failure

osu = integration.osu
CASE = integration.DEFAULT_CASE


def config_capture(install, env, directory, mode):
    """Check the actual config on each node for all three explicitly named modes."""
    if install != CASE / 'install' or mode not in ('iface', 'rc'):
        raise ValueError('unbound configuration source')
    query = ('import json,os,subprocess,sys\n'
             'e={k:v for k,v in os.environ.items() if not k.startswith(("UCX_","MPI_UCX_"))}\n'
             'e.update(json.loads(sys.argv[2]));d={}\n'
             'for key,argv in (("config",[sys.argv[1]+"/bin/ucx_info","-c"]),'
             '("device",[sys.argv[1]+"/bin/ucx_info","-d","-t",sys.argv[3]])):\n'
             ' p=subprocess.run(argv,env=e,text=True,capture_output=True,timeout=40)\n'
             ' if p.returncode: raise RuntimeError(key+":"+p.stderr[-400:])\n'
             ' d[key]=p.stdout\n'
             'print(json.dumps(d))\n')
    import subprocess
    tl = 'rc_urma' if mode == 'rc' else 'rm_urma'
    command = [sys.executable, '-c', query, str(install), json.dumps(env), tl]
    local = json.loads(subprocess.check_output(command, text=True, timeout=90))
    remote = json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3" "$4"\n', query,
                                     install, json.dumps(env), tl, timeout=100))
    result = {}
    for host, output in (('node89', local), ('node97', remote)):
        (directory / (host + '_ucx_config.txt')).write_text(output['config'])
        (directory / (host + '_ucx_device.txt')).write_text(output['device'])
        # Same validator covers all RM SEND controls and ordinary protocol choices.
        result[host] = integration.matrix.check_config(output['config'], env,
                                                       integration.boundary_config.bytes_value)
        parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', output['config'], re.M))
        for key, expected in env.items():
            if key.startswith('UCX_RC_URMA_'):
                actual = parsed.get(key, '')
                if key.endswith('SEG_SIZE'):
                    same = integration.boundary_config.bytes_value(actual) == int(expected)
                elif key.endswith('_THRESH'):
                    from decimal import Decimal
                    same = Decimal(actual) == Decimal(expected)
                else:
                    same = actual.lower() == expected.lower()
                if not same:
                    raise ValueError('effective RC configuration differs: ' + key)
        if mode == 'iface':
            cap = integration.boundary_config.parse_capability(output['device'])
            expected = 3989 if env['UCX_RM_URMA_LINK_MODE'] == 'reliable_v3' else 4093
            if cap != expected:
                raise ValueError('AM_BCOPY capability differs: ' + host)
            if re.search(r'^\s*#?\s*(?:put_short|put_bcopy|put_zcopy|get_bcopy|get_zcopy|am_zcopy)\s*:',
                         output['device'], re.M):
                raise ValueError('RM native RMA/AM_ZCOPY unexpectedly enabled')
    osu.base.write_json(directory / 'effective_configuration.json', result)
    return result


def profile_environment(case, ppn, saved, profile, path, run, wr_library=None, inject=False):
    if profile not in policy.MODES:
        raise ValueError('unknown comparison profile')
    # Pin all three profiles to identical candidate cores, device and queue budgets.
    env = integration.effective_environment(case, ppn, saved, wr_library, inject, path, run)
    if profile != 'reliable_v3':
        env['UCX_RM_URMA_LINK_MODE'] = 'iface_v2'
    env['UCX_RC_URMA_SEG_SIZE'] = '8192'
    if profile == 'rc':
        env['UCX_TLS'] = 'rc_urma,um_urma:aux,self'
        values = {'FC_ENABLE':'y', 'FC_WND_SIZE':'8', 'TX_DEPTH':'64',
                  'RX_DEPTH':str(osu.budget(ppn)['rx_depth']),
                  'FC_SOFT_THRESH':'0.5', 'FC_HARD_THRESH':'0.25'}
        env.update({'UCX_RC_URMA_' + key:value for key,value in values.items()})
    return env


@contextmanager
def registered_runner(case, build):
    previous = osu.BENCHMARKS, osu.arguments, osu.parse_output
    with integration.bind_runner(case, build):
        original_config = osu.base.parsed_config
        osu.BENCHMARKS = policy.BENCHMARKS
        osu.arguments = policy.arguments
        osu.parse_output = policy.parse_output
        osu.base.parsed_config = config_capture
        try:
            yield
        finally:
            osu.base.parsed_config = original_config
            osu.BENCHMARKS, osu.arguments, osu.parse_output = previous


def run_trial(case, out, commands, evidence, helpers, item, phase, index, repeat=0, inject=False):
    profile = item['profile']
    mode = 'rc' if profile == 'rc' else 'rm'
    ppn, size, benchmark = item['ppn'], item['size'], item['benchmark']
    # Unique repeat/index in a single root keeps the proven runner's file paths intact.
    name = '%s_%s_p%d_%s_%d' % (benchmark, mode, ppn, phase, index)
    if size != 8:
        name += '_b%d' % size
    run = out / name
    previous = osu.environment
    wr_library = helpers['wr_probe'] if phase == 'validation' and profile != 'rc' else None

    def environment(saved, actual_mode, install, configuration):
        if actual_mode != ('rc' if mode == 'rc' else 'iface') or install != case / 'install':
            raise ValueError('runner mode/install binding differs')
        if configuration != osu.budget(ppn):
            raise ValueError('runner queue budget differs')
        return profile_environment(case, ppn, saved, profile, item['path'], run, wr_library, inject)

    osu.environment = environment
    iterations = 20 if phase == 'validation' else 1000
    warmup = 2 if phase == 'validation' else 100
    args = argparse.Namespace(iterations=iterations, warmup=warmup, timeout=240)
    row = dict(item, name=name, passed=False)
    try:
        row = osu.one_trial(out, commands, evidence, benchmark, mode, ppn, phase, index, args,
                            message_bytes=size)
        row.update(profile=profile, size=size, path=item['path'], repeat=repeat, run=str(run),
                   software_injection=inject)
        if not row['passed']:
            raise RuntimeError(row.get('error', row.get('cleanup_error', 'OSU failed')))
        if phase == 'validation' and profile != 'rc':
            integration.wr_probe.fetch_peer_summaries(run, row)
            row['send_observations'] = integration.wr_probe.inspect_trial(run, row, helpers['wr_probe'])
            # AM configuration alone is not protocol path proof for collectives.
            if inject:
                pids = row['rank_evidence']['pids']
                names = ['inject_rank%d_pid%d.json' % (rank,pids[rank]) for rank in range(ppn,2*ppn)]
                integration.fetch_peer_json(run, names)
                faults = [json.loads((run / ('inject_rank%d_pid%d.json' % (rank,pids[rank]))).read_text())
                          for rank in range(2*ppn)]
                row['software_fault_evidence'] = integration.matrix.inspect_injection(
                    faults, pids, row['configuration']['nonce'])
                integration.collect_diagnostics(out, row, evidence, injection=True)
        elif phase == 'timing':
            env = row['configuration']['environment']
            if any(value in env.get('LD_PRELOAD','') for value in ('probe.so','drop.so','failure.so','observer.so')):
                raise ValueError('diagnostic/resource/fault observer leaked into timing')
            if env['UCX_LOG_LEVEL'] != 'warn' or row['configuration']['resource_probe']:
                raise ValueError('timing diagnostics unexpectedly enabled')
        row['passed'] = True
    except Exception as exc:
        row.update(passed=False, error=type(exc).__name__ + ': ' + str(exc))
    finally:
        osu.environment = previous
        run.mkdir(exist_ok=True)
        osu.base.write_json(run / 'trial.json', row)
    return row


def write_tables(out, report):
    fields = ['benchmark','ppn','bytes','profile','unit','median','minimum','maximum',
              'rss_median_mib','rss_min_mib','rss_max_mib','jetty_process_min','jetty_process_max']
    lines = ['# 可靠SEND 原版OSU对照', '',
             '固定97b产品，同库RC、iface_v2、reliable_v3；固定eager配置，仅覆盖表中负载。',
             '每项先数据校验，再5次独立计时和1次独立资源观测；顺序轮换，不筛除异常值。',
             '计时无WR/故障/Jetty观察器、无REQ/DEBUG日志；三模式均有相同MPI身份探针，',
             '只在Init/Finalize边界记录maps与RSS，不包装数据操作或MPI_Wtime。',
             'RSS为计时轮各rank Finalize前RSS相加的中位数[最小,最大]，两节点合计MiB；',
             '采样不同步，不能解释为设备内存或同一时刻全节点占用。Jetty为独立资源轮各rank峰值范围。',
             'RC使用原8KiB片长，RM使用4KiB；TX/RX/流控窗口/绑核/消息/OSU参数一致。',
             'MB/s为原OSU定义，不把双向带宽重复翻倍；没有真实网络故障性能结论。',
             '失败时保留已完成表行与全部原始任务，空缺不填优势。', '',
             '| OSU | PPN | 字节 | 模式 | 中位数[范围] | 单位 | RSS MiB中位数[范围] | Jetty/进程峰值范围 |',
             '|---|---:|---:|---|---|---|---|---|']
    with (out / 'comparison.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for group in report.get('comparisons', []):
            for profile, value in group['profiles'].items():
                metric, rss = value['metric'], value['rss_mib']
                low, high = value['jetty_peak_range']
                writer.writerow(dict(benchmark=group['benchmark'],ppn=group['ppn'],bytes=group['size'],
                    profile=profile,unit=policy.BENCHMARKS[group['benchmark']][2],
                    median=metric['median'],minimum=metric['minimum'],maximum=metric['maximum'],
                    rss_median_mib=rss['median'],rss_min_mib=rss['minimum'],rss_max_mib=rss['maximum'],
                    jetty_process_min=low,jetty_process_max=high))
                lines.append('| %s | %d | %d | %s | %.3f[%.3f,%.3f] | %s | %.2f[%.2f,%.2f] | %d..%d |' %
                             (group['benchmark'],group['ppn'],group['size'],profile,metric['median'],
                              metric['minimum'],metric['maximum'],policy.BENCHMARKS[group['benchmark']][2],
                              rss['median'],rss['minimum'],rss['maximum'],low,high))
    (out / 'comparison.md').write_text('\n'.join(lines) + '\n')


def prior_checkpoint(case):
    path = case / 'send_integration_fixed_x10vh01o/results.json'
    expected = '2f41dd86ca79136b38c993dd38921703f5f09fd73e5fb61e84852c629847ae3a'
    if integration.digest(path) != expected:
        raise ValueError('accepted 40-task checkpoint changed; preserve previous evidence')
    value = json.loads(path.read_text())
    if (value['source_commit'] != integration.matrix.COMMIT or
            value['module_sha256'] != integration.matrix.MODULE or
            value['status'] != 'SELECTED_RELIABLE_SEND_INTEGRATION_PASS' or
            len(value['trials']) != 40 or not all(row['passed'] for row in value['trials'])):
        raise ValueError('accepted integration identity/status/tasks differ')
    return dict(results_json=str(path), sha256=expected, tasks=40, rerun=False)


def execute(case, out, build, run_requested, timing_requested=False):
    report = dict(plan=policy.plan(), source_commit=integration.matrix.COMMIT,
                  module_sha256=integration.matrix.MODULE, status='PREFLIGHT', trials=[],
                  failures=[], comparisons=[], transport_changed=False, timing_requested=timing_requested)

    def save():
        osu.base.write_json(out / 'results.json', report)
        write_tables(out, report)

    def accept(row, category='trials'):
        report[category].append(row)
        save()
        if not row['passed']:
            raise RuntimeError(row['name'] + ': ' + row.get('error', row.get('cleanup_error','failed')))

    receipt = ROOT.parents[2] / 'tool_receipt.json'
    if receipt.is_file():
        report['tool_receipt'] = {'path': str(receipt), 'sha256': integration.digest(receipt),
                                  'identity': json.loads(receipt.read_text())}
    save()
    with registered_runner(case, build):
        try:
            report['prior_integration'] = prior_checkpoint(case)
            osu.validate_package()
            commands = {mode:osu.saved_spec(osu.LAB / 'osu_minimal8_ff_14ns8' / (mode + '_run.json'),
                                            case / 'install') for mode in ('rm','rc')}
            evidence = osu.preflight(out, commands)
            for benchmark in ('osu_allgather', 'osu_bw', 'osu_bibw'):
                text = (out / (benchmark + '_help.txt')).read_text()
                required = '--full' if benchmark == 'osu_allgather' else '--window-size'
                if required not in text:
                    raise ValueError('OSU required option missing: ' + benchmark + ' ' + required)
            report['preflight'] = evidence
            report['mode_configuration'] = {}
            for profile in policy.MODES:
                config_dir = out / ('config_' + profile)
                config_dir.mkdir()
                env = profile_environment(case,4,commands['rm'][1],profile,'eager',config_dir)
                report['mode_configuration'][profile] = config_capture(case / 'install',env,config_dir,
                    'rc' if profile == 'rc' else 'iface')
            # one_trial creates only its own direct child; stage its root first on peer.
            osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', out.parent, out)
            helpers = integration.compile_helpers(case, out, commands)
            binary, fault_library = failure.compile_probe(integration, case, out, commands)
            shutil.copy2(osu.ROOT / 'osu_rank.py', out / 'osu_rank.py')
            shutil.copy2(osu.A89 / 'rank_exec.py', out / 'a89_rank_exec.py')
            integration.copy_checked([out / 'osu_rank.py',out / 'a89_rank_exec.py',out / 'libosu_identity.so'],out)
            report['helper_sha256'] = {name:integration.digest(path) for name,path in helpers.items()}
            report['helper_sha256'].update(failure_probe=integration.digest(binary),failure_injector=integration.digest(fault_library))
            report['status'] = 'PREFLIGHT_PASS'
            save()
            print('预检=PASS 固定97b双端库/原版OSU7.5.2/三个模式/CPU资源；最高PPN4', flush=True)
            if not run_requested:
                print('本次未启动双节点MPI或UCT；正式运行需 --run', flush=True)
                return report
            for item in policy.functional_cases():
                row = run_trial(case,out,commands,evidence,helpers,item,'validation',len(report['trials']))
                accept(row)
                print('集合 OSU=%s MODE=%s PPN=%d SIZE=%d DATA/MAPS/EXIT=PASS WR=%s' %
                      (item['benchmark'],item['profile'],item['ppn'],item['size'],
                       '未观察RC' if item['profile']=='rc' else '4096B以内'), flush=True)
            injected = dict(benchmark='osu_multi_lat',profile='reliable_v3',ppn=1,size=65536,path='eager')
            row = run_trial(case,out,commands,evidence,helpers,injected,'validation',len(report['trials']),inject=True)
            accept(row)
            print('原版OSU软件丢内部DATA/ACK/重复=PASS ACTUAL_RETRY_POSTS=%d；不是硬件丢包' %
                  row['production_retransmit_posts'], flush=True)
            for item in policy.failure_cases():
                row = failure.run(integration,case,out,commands,binary,fault_library,item)
                accept(row,'failures')
                print('退出 LIMIT=%s PROFILE=%s 双端退出/回调/交付/close=PASS RETRIES=%d' %
                      (item['limit'],item['fault'],row['evidence']['production_retry_posts']),flush=True)
            report['status'] = 'SELECTED_COLLECTIVE_SOFTWARE_FAULT_AND_BOUNDED_EXIT_PASS'
            save()
            if not timing_requested:
                print('功能门禁=PASS；本次未请求计时/资源轮，正式性能待验',flush=True)
                return report
            for case_item in policy.timing_cases():
                print('计时组开始 OSU=%s PPN=%d SIZE=%d：三模式各1校验＋5计时＋1资源' %
                      (case_item['benchmark'],case_item['ppn'],case_item['size']),flush=True)
                # Data controls for the exact performance workloads/modes precede timing.
                for profile in policy.MODES:
                    item = dict(case_item, profile=profile)
                    accept(run_trial(case,out,commands,evidence,helpers,item,'validation',len(report['trials'])))
                for repeat in range(policy.REPEATS):
                    order = policy.MODES[repeat % 3:] + policy.MODES[:repeat % 3]
                    for profile in order:
                        item = dict(case_item,profile=profile)
                        accept(run_trial(case,out,commands,evidence,helpers,item,'timing',len(report['trials']),repeat))
                for profile in policy.MODES:
                    item = dict(case_item,profile=profile)
                    accept(run_trial(case,out,commands,evidence,helpers,item,'resources',len(report['trials'])))
                group = policy.compare(report['trials'][len(policy.functional_cases())+1:],case_item)
                report['comparisons'].append(group)
                save()
                values = {p:round(v['metric']['median'],3) for p,v in group['profiles'].items()}
                memory = {p:round(v['rss_mib']['median'],2) for p,v in group['profiles'].items()}
                print('正式OSU=%s PPN=%d SIZE=%d %s=%s RSS_MiB=%s' %
                      (case_item['benchmark'],case_item['ppn'],case_item['size'],
                       policy.BENCHMARKS[case_item['benchmark']][2],json.dumps(values),json.dumps(memory)),flush=True)
                print('可靠模式变化百分比=' + json.dumps(group.get('reliable_changes_pct',{})) +
                      '；metric正值：us变慢、MB/s变快；rss正值：占用增加',flush=True)
            report['status'] = 'SELECTED_SEND_FUNCTIONAL_AND_OSU_TIMING_RESOURCES_PASS_REVIEW_REQUIRED'
            save()
            return report
        except BaseException as exc:
            report.update(status='STOPPED_INCOMPLETE',error=type(exc).__name__ + ': ' + str(exc))
            save()
            raise


def main():
    parser = argparse.ArgumentParser(description='可靠SEND集合/失败退出/正式OSU；默认仅计划，最高PPN4')
    parser.add_argument('case', type=Path, nargs='?', default=CASE)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--timing', action='store_true')
    args = parser.parse_args()
    if args.timing and not args.run:
        parser.error('--timing requires --run')
    if not (args.run or args.preflight_only):
        print(json.dumps(policy.plan(),ensure_ascii=False,indent=2))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('only root on node89 may explicitly execute')
    case = args.case.resolve()
    build = integration.validate_case(case)
    out = Path(tempfile.mkdtemp(prefix='send_validation_',dir=case))
    print('SEND_VALIDATION_DIR=' + str(out),flush=True)
    try:
        execute(case,out,build,args.run,args.timing)
    except Exception as exc:
        print('SEND_VALIDATION_STOPPED=' + type(exc).__name__ + ': ' + str(exc)[:900],flush=True)
        raise SystemExit(1)
    finally:
        for label,name in (('RESULTS_JSON','results.json'),('TABLE_MD','comparison.md'),('TABLE_CSV','comparison.csv')):
            print(label + '=' + str(out / name),flush=True)


if __name__ == '__main__':
    main()
