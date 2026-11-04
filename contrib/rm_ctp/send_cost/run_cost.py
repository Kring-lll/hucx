#!/usr/bin/env python3
"""Same-session original OSU baseline and separate elapsed-scope diagnosis."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import build as diagnostic_build
import instrument
import profile_read
sys.path.insert(0, str(ROOT.parent/'send_inline'))
import run_candidate as inline
previous, integration, osu = inline.previous, inline.integration, inline.osu
MODES = ('rc', 'iface_v2', 'reliable_v3')
WORKLOADS = (('osu_multi_lat', 1), ('osu_alltoall', 4))


def plan():
    return dict(base_commit=instrument.BASE_COMMIT, accepted_product_changed=False,
                diagnostic_rebuild=True, max_ppn=4, bytes=8,
                accepted_validation=6, accepted_timing=30, diagnostic_validation=6,
                diagnostic_disabled=6, diagnostic_accum=6, total_mpi=54,
                profiling='UCX selected ALWAYS accum scopes; no perf/PMU',
                data_interval='MPI_Init complete to MPI_Finalize entry, includes warmup/control',
                formal_timing='accepted uninstrumented install only, five runs per group',
                diagnostic_time='inclusive elapsed, contains nesting, instrumentation and descheduling',
                default_action='PLAN_ONLY', node_acceptance='PENDING')


def schedule():
    rows = [('accepted','validation',0,m) for m in MODES]
    rows += [('on','validation',0,m) for m in MODES]
    for repeat in range(5):
        order = MODES[repeat%3:]+MODES[:repeat%3]
        rows += [('accepted','timing',repeat,m) for m in order]
    rows += [(stage,'diagnostic',0,m) for stage in ('off','on') for m in MODES]
    return rows


def publish(out, report):
    osu.base.write_json(out/'results.json', report)
    lines = ['# 可靠SEND成本诊断', '',
             '原版OSU不改动。accepted组可作本批正常计时；诊断库所有耗时仅作诊断。',
             'scope耗时包含子scope、插桩及线程被调度出去的时间，不是纯CPU时间，不能相加算百分比。',
             '统计Init后至Finalize前，包含预热和基准控制；同一试验值是迭代平均，不是单包p99。',
             '关闭/开启采集各一轮，仅显示观测扰动，不能据此作稳定性或精确净成本结论。',
             'STATUS='+report['status'], '']
    for group in report.get('groups', []):
        lines.append('## %s PPN%d 8B' % (group['benchmark'], group['ppn']))
        for label, value in group['profiles'].items():
            lines.append('%s accepted原值us=%s median=%.3f；diag off/on=%s/%sus' %
                         (label, value['accepted_samples_us'], value['accepted_median_us'],
                          value['diagnostic_off_us'], value['diagnostic_on_us']))
            lines.append('诊断计数='+json.dumps(value['events'], ensure_ascii=False, sort_keys=True))
            lines.append('接收成功post平均WR数='+str(value['rx_buffers_per_successful_post']))
            lines.append('各rank最大范围内的scope均值ns（含嵌套，不能相加）='+
                         json.dumps(value['scope_means_ns_by_name'], sort_keys=True))
        lines.append('')
    (out/'diagnostic_summary.md').write_text('\n'.join(lines)+'\n')


def transfer_diagnostic(case):
    base = osu.base
    archive = case/'install.tar.gz'
    base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', case)
    base.copy_remote([archive], str(case)+'/')
    if base.ssh('sha256sum "$1"\n', archive).split()[0] != instrument.digest(archive):
        raise ValueError('diagnostic archive copy differs')
    base.ssh('set -eu\ntest ! -e "$1/install"\ntar -xzf "$1/install.tar.gz" -C "$1"\n', case, timeout=90)


@contextmanager
def bound(case, build):
    with inline.bound(case, build):
        saved = osu.base.parsed_config
        def config(install, env, directory, mode):
            clean = dict(env, UCX_PROFILE_MODE='', UCX_PROFILE_FILE='')
            return saved(install, clean, directory, mode)
        osu.base.parsed_config = config
        osu.BENCHMARKS = {name:previous.policy.BENCHMARKS[name] for name, _ in WORKLOADS}
        try:
            yield
        finally:
            osu.base.parsed_config = saved


def prepare(case, build, directory, diagnostic):
    directory.mkdir()
    osu.base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', directory)
    with bound(case, build):
        osu.validate_package()
        commands = {mode:osu.saved_spec(osu.LAB/'osu_minimal8_ff_14ns8'/(mode+'_run.json'), case/'install')
                    for mode in ('rm', 'rc')}
        evidence = osu.preflight(directory, commands)
    paths = [directory/'libosu_identity.so']
    for source, name in ((osu.ROOT/'osu_rank.py', 'osu_rank.py'),
                         (osu.A89/'rank_exec.py', 'a89_rank_exec.py')):
        target = directory/name
        shutil.copy2(source, target)
        paths.append(target)
    # Real WR validation uses the already reviewed inline observer only in validation.
    probe = directory/'libsend_wr_probe.so'
    command = ['gcc', '-std=gnu11', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
               '-shared', '-fPIC', '-I/usr/include/ub/umdk/urma',
               str(ROOT.parent/'send_inline/send_probe.c'), '-o', str(probe), '-ldl', '-pthread']
    osu.base.run_logged(command, integration.runtime_environment(commands['rm'][1]),
                        directory/'compile_wr.log', 90)
    paths.append(probe)
    if diagnostic:
        phase = directory/'libsend_cost_phase.so'
        command = ['gcc', '-std=gnu11', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                   '-shared', '-fPIC', '-DPROFILE_PHASE_ONLY', str(ROOT/'phase.c'),
                   '-ldl', '-o', str(phase)]
        osu.base.run_logged(command, integration.runtime_environment(commands['rm'][1]),
                            directory/'compile_phase.log', 60)
        paths.append(phase)
    integration.copy_checked(paths, directory)
    return commands, evidence


def fetch_artifacts(run, names):
    if len(names) != len(set(names)) or any(not re.fullmatch(
            r'(?:profile_rank\d+_pid\d+(?:_ready|_end)?\.json|ucx_[A-Za-z0-9_.-]+_\d+\.prof)', name)
            for name in names):
        raise ValueError('unsafe diagnostic names')
    code = '''import hashlib,json,sys
from pathlib import Path
r=Path(sys.argv[1]); answer={}
for name in json.loads(sys.argv[2]):
 p=r/name
 if p.is_symlink() or not p.is_file() or p.stat().st_size>16777216: raise ValueError("diagnostic missing/type/size: "+name)
 answer[name]={"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"size":p.stat().st_size}
print(json.dumps(answer))
'''
    values = json.loads(osu.base.ssh('python3 -c "$1" "$2" "$3"\n', code, run, json.dumps(names)))
    for name, value in values.items():
        target = run/name
        if target.exists() or target.is_symlink():
            raise ValueError('refuse to overwrite evidence')
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                        osu.base.PEER+':'+str(target), str(target)], check=True, timeout=45)
        if target.stat().st_size != value['size'] or instrument.digest(target) != value['sha256']:
            raise ValueError('diagnostic transfer differs')
    return values


def collect(directory, row, case, enabled):
    run = directory/row['name']
    pids = row['rank_evidence']['pids']
    ppn = row['ppn']
    nonce = row['configuration']['nonce']
    hosts = [v['hostname'] for v in row['inventory_for_cost']]
    def pid(rank):
        return pids[rank] if rank in pids else pids[str(rank)]
    names = []
    for rank in range(ppn, 2*ppn):
        names += ['profile_rank%d_pid%d%s.json' % (rank, pid(rank), suffix)
                  for suffix in ('_ready', '_end', '')]
        if enabled:
            names += ['ucx_%s_%d.prof' % (hosts[1], pid(rank))]
    row['peer_cost_artifacts'] = fetch_artifacts(run, names)
    values = []
    for rank in range(2*ppn):
        prefix = 'profile_rank%d_pid%d' % (rank, pid(rank))
        ready, end, final = [json.loads((run/(prefix+suffix+'.json')).read_text())
                             for suffix in ('_ready', '_end', '')]
        for value in (ready, end, final):
            if (value['rank'], value['pid'], value['nonce']) != (rank, pid(rank), nonce):
                raise ValueError('phase actual process identity differs')
        if (not final['finalized'] or ready['begin_ns'] != end['begin_ns'] or
                end['at_ns'] <= ready['at_ns'] or final['at_ns'] != end['at_ns'] or
                final['events'] != end['events'] or set(final['events']) != set(instrument.EVENTS)):
            raise ValueError('phase boundary/event coverage differs')
        for phase in ('after_init', 'before_finalize'):
            if str(directory/'libsend_cost_phase.so') not in (run/('rank%d_%s.maps' % (rank, phase))).read_text():
                raise ValueError('phase DSO missing from actual rank maps')
        profile = run/('ucx_%s_%d.prof' % (hosts[int(rank>=ppn)], pid(rank)))
        if enabled:
            final['accum'] = profile_read.read(profile, pid(rank), hosts[int(rank>=ppn)], case/'install')
            final['accum_sha256'] = instrument.digest(profile)
            if final['events']['rx_post_errors']:
                raise ValueError('diagnostic receive submission failed')
        elif any(final['events'].values()) or profile.exists():
            raise ValueError('disabled diagnostic unexpectedly collected')
        values.append(final)
    row['cost_evidence'] = values


def trial(case, build, directory, commands, evidence, item, phase, index, stage):
    profile = item['profile']
    mode = 'rc' if profile == 'rc' else 'rm'
    run = directory/('%s_%s_p%d_%s_%d' % (item['benchmark'], mode, item['ppn'], phase, index))
    diagnostic = stage != 'accepted'
    enabled = stage == 'on'
    row = None
    with bound(case, build):
        saved = osu.environment
        def environment(prior, actual_mode, install, budget):
            if (actual_mode != ('rc' if mode=='rc' else 'iface') or install != case/'install'
                    or budget != osu.budget(item['ppn'])):
                raise ValueError('cost runner binding differs')
            env = previous.profile_environment(case, item['ppn'], prior, profile, 'eager', run,
                        directory/'libsend_wr_probe.so' if phase=='validation' and mode=='rm' else None)
            env.update(UCX_PROFILE_MODE='accum' if enabled else '',
                       UCX_PROFILE_FILE=str(run/'ucx_%h_%p.prof'))
            if diagnostic:
                env['LD_PRELOAD'] = str(directory/'libsend_cost_phase.so')+':'+env['LD_PRELOAD']
                env['HUCX_SEND_COST_MODE'] = 'on' if enabled else 'off'
            return env
        osu.environment = environment
        try:
            row = osu.one_trial(directory, commands, evidence, item['benchmark'], mode, item['ppn'],
                                phase, index, argparse.Namespace(iterations=1000, warmup=100, timeout=240), message_bytes=8)
            row.update(profile=profile, stage=stage, diagnostic_only=diagnostic,
                       module_sha256=build['module_sha256'], install=str(case/'install'),
                       inventory_for_cost=evidence['inventory'])
            if not row['passed']:
                raise RuntimeError(row.get('error', row.get('cleanup_error', 'trial failed')))
            if phase=='validation' and mode=='rm':
                integration.wr_probe.fetch_peer_summaries(run, row)
                row['send_observations'] = integration.wr_probe.inspect_trial(run, row, directory/'libsend_wr_probe.so')
                if profile=='reliable_v3':
                    inline.add_inline_evidence(row, directory, 123, False)
            if diagnostic:
                collect(directory, row, case, enabled)
                if enabled and profile=='reliable_v3' and not all(
                        v['events']['data_received'] > 0 and v['events']['ack_received'] > 0
                        for v in row['cost_evidence']):
                    raise ValueError('reliable cost hooks not exercised')
        except Exception as exc:
            if row is None:
                raise
            row.update(passed=False, error=type(exc).__name__+': '+str(exc))
        finally:
            osu.environment = saved
            if row is not None:
                osu.base.write_json(run/'trial.json', row)
    return row


def summarize(rows, benchmark, ppn):
    result = dict(benchmark=benchmark, ppn=ppn, profiles={})
    for mode in MODES:
        chosen = [r for r in rows if r['benchmark']==benchmark and r['ppn']==ppn and r['profile']==mode]
        if len(chosen)!=9 or not all(r['passed'] for r in chosen):
            raise ValueError('cost group incomplete; retain all samples')
        baseline = [r['output']['value'] for r in chosen if r['stage']=='accepted' and r['phase']=='timing']
        off = [r for r in chosen if r['stage']=='off' and r['phase']=='diagnostic']
        on = [r for r in chosen if r['stage']=='on' and r['phase']=='diagnostic']
        if len(baseline)!=5 or len(off)!=1 or len(on)!=1:
            raise ValueError('cost phase coverage differs')
        totals = {key:sum(v['events'][key] for v in on[0]['cost_evidence']) for key in instrument.EVENTS}
        scopes = {}
        for rank in on[0]['cost_evidence']:
            for scope in rank['accum']['scopes']:
                scopes.setdefault(scope['name'], []).append(scope['mean_ns'])
        posts = totals['rx_post_calls']-totals['rx_post_errors']
        result['profiles'][mode] = dict(accepted_samples_us=baseline,
                accepted_median_us=statistics.median(baseline),
                diagnostic_off_us=off[0]['output']['value'], diagnostic_on_us=on[0]['output']['value'],
                events=totals, rx_buffers_per_successful_post=totals['rx_buffers_posted']/posts if posts else None,
                scope_means_ns_by_name={key:[min(v), max(v)] for key,v in scopes.items()},
                scope_times_not_additive=True)
    return result


def execute(requested):
    accepted_build = diagnostic_build.validate_accepted()
    out = Path(tempfile.mkdtemp(prefix='send_cost_accept_', dir=instrument.ACCEPTED))
    print('SEND_COST_DIR='+str(out), flush=True)
    report = dict(plan=plan(), status='PREFLIGHT', accepted_commit=instrument.BASE_COMMIT,
                  accepted_module=instrument.BASE_MODULE, trials=[], groups=[])
    publish(out, report)
    try:
        receipt = ROOT.parents[2]/'tool_receipt.json'
        if not receipt.is_file():
            raise ValueError('execute the checked node ZIP; tool receipt missing')
        report['tool_receipt'] = dict(path=str(receipt), sha256=instrument.digest(receipt),
                                      identity=json.loads(receipt.read_text()))
        # A preflight checks the accepted install and archive without compiling.
        if instrument.digest(diagnostic_build.archive_path()) != instrument.ARCHIVE_SHA:
            raise ValueError('original accepted archive missing/changed')
        if not requested:
            report['status']='LOCAL_IDENTITY_PREFLIGHT_PASS_NOT_BUILT_OR_RUN'
            return report
        report['sdk_headers'] = inline.sdk_headers()
        # Build in a new directory before placing anything on the peer.
        case, build = diagnostic_build.build()
        report.update(diagnostic_case=str(case), diagnostic_build=build)
        transfer_diagnostic(case)
        osu.base.ssh('set -eu\nmkdir -p "$1"\ntest ! -e "$2"\nmkdir "$2"\n', out.parent, out)
        accepted = prepare(instrument.ACCEPTED, accepted_build, out/'accepted', False)
        diagnostic = prepare(case, build, out/'diagnostic', True)
        report['preflight'] = dict(accepted=accepted[1], diagnostic=diagnostic[1])
        publish(out, report)
        for benchmark, ppn in WORKLOADS:
            for stage, phase, repeat, profile in schedule():
                root = out/('accepted' if stage=='accepted' else 'diagnostic')
                actual_case, actual_build, tools = ((instrument.ACCEPTED, accepted_build, accepted)
                                                   if stage=='accepted' else (case, build, diagnostic))
                row = trial(actual_case, actual_build, root, *tools,
                            dict(benchmark=benchmark, ppn=ppn, profile=profile), phase,
                            len(report['trials']), stage)
                row['comparison_repeat']=repeat
                report['trials'].append(row)
                publish(out, report)
                if not row['passed']:
                    raise RuntimeError(row['name']+': '+row.get('error', 'incomplete'))
                print('COST OSU=%s PPN=%d MODE=%s STAGE=%s PHASE=%s EXIT=PASS' %
                      (benchmark,ppn,profile,stage,phase), flush=True)
            group = summarize(report['trials'], benchmark, ppn)
            report['groups'].append(group)
            publish(out, report)
            print('GROUP='+json.dumps(group, ensure_ascii=False, sort_keys=True), flush=True)
        if len(report['trials'])!=54:
            raise ValueError('unexpected task count')
        report['status']='ACCEPTED_OSU_AND_DIAGNOSTIC_SCOPES_CAPTURED_REVIEW_REQUIRED'
        print('原版OSU基线/诊断开关/累计scope采集完成；根因及优化效果待审阅', flush=True)
        return report
    except BaseException as exc:
        report.update(status='STOPPED_INCOMPLETE', error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        publish(out, report)
        print('RESULTS_JSON='+str(out/'results.json'), flush=True)
        print('DIAGNOSTIC_SUMMARY='+str(out/'diagnostic_summary.md'), flush=True)


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    if not (args.run or args.preflight_only):
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if os.geteuid()!=0 or socket.gethostname()!='node89':
        raise ValueError('run as root on node89 only')
    try:
        execute(args.run)
    except Exception as exc:
        print('SEND_COST_STOPPED='+type(exc).__name__+': '+str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__=='__main__':
    main()
