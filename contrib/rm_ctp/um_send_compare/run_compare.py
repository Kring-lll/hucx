#!/usr/bin/env python3
"""Three real transport profiles; original OSU; no product rebuild or overwrite."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
from types import SimpleNamespace

import policy


def config_summary(run, profile):
    result = {}
    prefix = 'UCX_'+profile.upper()+'_URMA_'
    for host in ('node89','node97'):
        values = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$',
                                (run/(host+'_ucx_config.txt')).read_text(), re.M))
        keys = ('SEG_SIZE','TX_DEPTH','RX_DEPTH','MAX_WINDOW','TIMEOUT','MAX_INLINE_SIZE')
        result[host] = {k:values[prefix+k] for k in keys if prefix+k in values}
        if 'SEG_SIZE' not in result[host]:
            raise ValueError('actual transport SEG_SIZE missing: '+host)
    if result['node89'] != result['node97']:
        raise ValueError('transport configuration differs between nodes')
    return result


def run_trial(engine, out, commands, evidence, item, index):
    group, benchmark, ppn, size, phase, repeat, profile = item
    osu = engine['osu']
    engine['PROFILE'] = profile
    engine['OBSERVE'] = engine['FAULT'] = False
    mode = 'rc' if profile == 'rc' else 'rm'
    args = SimpleNamespace(iterations=10000 if benchmark == 'osu_multi_lat' else 1000,
                           warmup=1000 if benchmark == 'osu_multi_lat' else 100, timeout=120)
    if phase == 'validation':
        args.iterations, args.warmup = 100, 10
    row = osu.one_trial(out, commands, evidence, benchmark, mode, ppn, phase, index, args, message_bytes=size)
    row.update(group=group, profile=profile, message_bytes=size, repeat=repeat)
    run = out/row['name']
    try:
        policy.inspect_trial(row, profile, phase)
        row['effective_config'] = config_summary(run, profile)
        for rank in range(2*ppn):
            for point in ('after_init','before_finalize'):
                text = (run/('rank%d_%s.maps' % (rank,point))).read_text()
                if any(name in text for name in ('libum_send_probe.so','libsend_probe.so','failure.so')):
                    raise ValueError('actual application mapped a diagnostic/fault observer')
                has_resource = 'liburma_resource_observer.so' in text
                if has_resource != (phase == 'resources'):
                    raise ValueError('actual application resource-observer phase mismatch')
        if phase == 'resources' and len(row['jettys']) != 2*ppn:
            raise ValueError('resource rank coverage incomplete')
    except Exception as exc:
        row.update(passed=False, error=type(exc).__name__+': '+str(exc))
    osu.base.write_json(run/'trial.json', row)
    return row


def main():
    parser = argparse.ArgumentParser(description='RC/RM/UM original OSU: 3 gates, 6 groups, separate timing/resources')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true')
    action.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--timing', action='store_true')
    args = parser.parse_args()
    if not (args.run or args.preflight_only):
        print(json.dumps(policy.plan(args.timing), ensure_ascii=False, indent=2));return
    if os.geteuid() != 0 or socket.gethostname() != 'node89':
        raise ValueError('explicit execution limited to root on node89')
    import bootstrap
    guard, engine, prior, metadata = bootstrap.load()
    case = guard.CASE
    osu = engine['osu']; base = osu.base
    out = Path(tempfile.mkdtemp(prefix='um_osu_three_', dir=case))
    print('THREE_OSU_DIR='+str(out), flush=True)
    report = {'source_commit':guard.COMMIT,'module_sha256':guard.MODULE_SHA,
              'tool':metadata,'checkpoint':prior,'plan':policy.plan(args.timing),
              'status':'RUNNING','gates':[],'trials':[],'comparisons':[]}
    def save():
        base.write_json(out/'results.json', report)
        policy.write_tables(out, report)
    save()
    original = {name:getattr(osu,name) for name in
                ('DEFAULT_CASE','PRODUCT_COMMIT','PRODUCT_MODULE','BENCHMARKS','arguments','parse_output','environment','budget','inspect_jettys')}
    original_config = base.parsed_config
    try:
        engine['OBSERVE'] = engine['FAULT'] = False
        engine['deploy'](case, engine['validate_source'](case))
        osu.DEFAULT_CASE, osu.PRODUCT_COMMIT, osu.PRODUCT_MODULE = case, guard.COMMIT, guard.MODULE_SHA
        osu.BENCHMARKS, osu.arguments, osu.parse_output = policy.BENCHMARKS, policy.arguments, policy.parse_output
        osu.environment = engine['environment']; base.parsed_config = engine['parsed_config']
        osu.budget = lambda ppn: {'ppn':ppn,'tx_depth':64,'rx_depth':512,'window':32}
        def jettys(run, ppn, unused, pids, depth):
            return original['inspect_jettys'](run,ppn,policy.MODES[engine['PROFILE']],pids,depth)
        osu.inspect_jettys = jettys
        engine['PROFILE'] = 'rm'
        commands = {}
        for mode in ('rc','rm'):
            command,saved = osu.saved_spec(osu.LAB/'osu_minimal8_ff_14ns8'/(mode+'_run.json'),case/'install')
            commands[mode] = command, engine['environment'](saved,mode,case/'install',{})
        evidence = osu.preflight(out,commands);report['preflight'] = evidence
        if min(v['available_cores'] for v in evidence['inventory']) < 4:
            raise ValueError('PPN4 requires four permitted cores on each node')
        for name in ('osu_bw','osu_bibw'):
            help_text = (out/(name+'_help.txt')).read_text()
            if any(k not in help_text for k in ('--window-size','--buffer-num')):
                raise ValueError('original OSU bandwidth options missing')
        base.ssh('set -eu\ntest ! -e "$1"\nmkdir "$1"\n',out)
        for source,name in ((osu.ROOT/'osu_rank.py','osu_rank.py'),(osu.A89/'rank_exec.py','a89_rank_exec.py')):
            shutil.copy2(source,out/name)
        helpers = [out/n for n in ('osu_rank.py','a89_rank_exec.py','libosu_identity.so')]
        base.copy_remote(helpers,str(out)+'/')
        for path in helpers:
            if base.ssh('sha256sum "$1"\n',path).split()[0] != bootstrap.digest(path):
                raise ValueError('peer helper changed: '+path.name)
        report['helper_sha256'] = {p.name:bootstrap.digest(p) for p in helpers}
        save()
        print('预检=PASS 固定a4d双端库/原版OSU7.5.2/最高PPN4；UM必测，产品不重建',flush=True)
        if args.preflight_only:
            report['status']='PREFLIGHT_ONLY_NO_MPI';save();return
        index = 0
        for profile in policy.PROFILES:
            row = run_trial(engine,out,commands,evidence,(-1,'osu_multi_lat',1,8,'validation',0,profile),index)
            report['gates'].append(row);index += 1;save()
            print('GATE PROFILE=%s 8B PASS=%s'%(profile,row['passed']),flush=True)
            if not row['passed']:
                raise RuntimeError('mandatory '+profile+' gate: '+row.get('error','failed'))
        for item in policy.schedule(args.timing):
            row = run_trial(engine,out,commands,evidence,item,index);index += 1
            report['trials'].append(row);save()
            print('OSU=%s PROFILE=%s PPN=%d BYTES=%d PHASE=%s REPEAT=%d PASS=%s' % (
                item[1],item[6],item[2],item[3],item[4],item[5],row['passed']),flush=True)
            if not row['passed']:
                raise RuntimeError(row.get('error','OSU failed')+' LOG='+str(out/row['name']/'launcher.log'))
            if args.timing and item[4] == 'resources' and item[6] == policy.PROFILES[-1]:
                group = policy.summarize(report['trials'],item[0])
                report['comparisons'].append(group);save()
                print('GROUP='+json.dumps(group,sort_keys=True),flush=True)
        report['status'] = ('SELECTED_THREE_PROFILE_OSU_COMPARISON_COMPLETE_REVIEW_REQUIRED' if args.timing
                            else 'THREE_PROFILE_SELECTED_VALIDATION_PASS_NO_TIMING')
        save()
        print('完成；仅所选正常SEND性能/资源，故障恢复性能、完整C/D/规模/READ_WRITE未验收',flush=True)
    except Exception as exc:
        report.update(status='STOPPED_INCOMPLETE',error=type(exc).__name__+': '+str(exc));save()
        print('THREE_OSU_STOPPED='+report['error'],flush=True)
        raise
    finally:
        for name,value in original.items():setattr(osu,name,value)
        base.parsed_config = original_config
        print('RESULTS_JSON='+str(out/'results.json'),flush=True)
        print('TABLE_MD='+str(out/'comparison.md'),flush=True)
        print('TABLE_CSV='+str(out/'comparison.csv'),flush=True)
        if bootstrap.digest(case/'source'/guard.RELATIVE_ENTRY) != guard.ENTRY_SHA or bootstrap.digest(case/'install/lib/ucx/libuct_ub.so.0.0.0') != guard.MODULE_SHA:
            raise RuntimeError('original candidate source/module changed')


if __name__ == '__main__':
    main()
