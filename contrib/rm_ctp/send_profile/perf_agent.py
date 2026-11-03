#!/usr/bin/env python3
"""Bounded, unprivileged attach to one verified application PID; no sysctl."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def ticks(pid):
    return Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]


def identity(pid, binary, nonce, run):
    first = ticks(pid)
    env = Path('/proc/%d/environ' % pid).read_bytes().split(b'\0')
    if (os.readlink('/proc/%d/exe' % pid) != str(Path(binary).resolve()) or
            ('A89_RUN_NONCE=' + nonce).encode() not in env or
            ('HUCX_OSU_PROBE_DIR=' + str(run)).encode() not in env or ticks(pid) != first):
        raise ValueError('actual application PID identity changed')
    return first


def record_command(pid, target, seconds):
    return ['perf', 'record', '-q', '--no-inherit', '--timestamp', '-o', str(target),
            '-e', 'cpu-clock:u', '-F', '99', '--call-graph', 'dwarf,8192',
            '--clockid', 'mono', '-p', str(pid), '--', 'sleep', str(seconds)]


def preflight(out):
    """Exercise actual attach/options. Failure disables sampling, not counters."""
    out = Path(out)
    out.mkdir(exist_ok=True)
    result = {'available': False, 'event': 'cpu-clock:u', 'frequency': 99,
              'sysctl_modified': False, 'hostname': os.uname().nodename}
    if not shutil.which('perf'):
        result['reason'] = 'perf executable missing'
        return result
    child = subprocess.Popen([sys.executable, '-c',
        'import time\ne=time.monotonic()+2\nwhile time.monotonic()<e: pass'])
    try:
        command = record_command(child.pid, out/'smoke.data', .25)
        with (out/'smoke.log').open('x') as log:
            record = subprocess.run(command, stdout=log, stderr=log, timeout=5)
        result.update(command=command, exit=record.returncode)
        if record.returncode:
            result['reason'] = 'perf attach unavailable; see smoke.log'
        else:
            command = ['perf','script','--ns','-i',str(out/'smoke.data'),
                       '-F','pid,time,event,ip,sym,dso']
            value = subprocess.run(command, text=True, capture_output=True, timeout=5)
            (out/'smoke.script').write_text(value.stdout)
            (out/'smoke.script.stderr').write_text(value.stderr)
            samples = parse_samples(value.stdout)
            if value.returncode or not samples:
                result['reason'] = 'perf script format or smoke samples unavailable'
            else:
                result['available'] = True
    except (OSError, subprocess.TimeoutExpired) as exc:
        result['reason'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
    return result


def parse_samples(text):
    """Default perf script grouped callchains; retain pid, monotonic ns, leaf."""
    samples = []
    current = None
    for line in text.splitlines():
        if not line.strip():
            current = None
            continue
        match = re.match(r'^\s*(\d+)(?:/\d+)?\s+(\d+)\.(\d+):\s+cpu-clock(?::u)?:\s*(.*)$', line)
        if match:
            ns = int(match[2])*1000000000 + int(match[3].ljust(9,'0')[:9])
            current = {'pid':int(match[1]),'ns':ns,'frames':[]}
            samples.append(current)
            rest = match[4]
        elif current is not None:
            rest = line.strip()
        else:
            continue
        frame = re.match(r'^(?:0x)?[0-9a-fA-F]+\s+(.+?)\s+\((.*)\)\s*$', rest)
        if frame:
            current['frames'].append({'symbol':frame[1],'dso':frame[2]})
    return samples


def summarize(text, pid, begin, end):
    samples = [s for s in parse_samples(text) if s['pid']==pid and begin<=s['ns']<=end]
    flat = Counter()
    inclusive = Counter()
    unknown = 0
    for sample in samples:
        frames = sample['frames']
        if not frames:
            unknown += 1
            continue
        leaf = frames[0]
        flat[(leaf['dso'],leaf['symbol'])] += 1
        if leaf['symbol'] in ('[unknown]','0x0') or leaf['dso']=='[unknown]':
            unknown += 1
        joined = ' '.join(frame['symbol'] for frame in frames)
        for category, pattern in (
            ('reliable', r'uct_rm_urma_reliable|uct_rm_urma_ep_reliable'),
            ('provider_post', r'urma_post|udma.*post'),
            ('poll_progress', r'poll_jfc|poll_rx|poll_tx|worker_progress|opal_progress'),
            ('allocation_copy', r'malloc|calloc|free|mpool|memcpy|memmove'),
            ('mpi_collective', r'alltoall|Alltoall|Barrier|barrier|ompi_coll')):
            if re.search(pattern, joined, re.I):
                inclusive[category] += 1
    n = len(samples)
    has_ucx = any(re.search(r'libuc[pts]|libuct_ub|liburma|libudma', dso)
                  for dso, symbol in flat)
    status = 'SAMPLES_CAPTURED_REVIEW_REQUIRED' if n>=50 and has_ucx and unknown<=n/4 else 'INSUFFICIENT_SAMPLES'
    return {'status':status,'samples':n,'unknown_leaf_samples':unknown,
            'phase_begin_ns':begin,'phase_end_ns':end,
            'flat_top':[{'dso':dso,'symbol':symbol,'samples':count,'percent':100*count/n}
                        for (dso,symbol),count in flat.most_common(30)],
            'inclusive_category_samples':dict(inclusive),
            'inclusive_categories_overlap':True}


def agent(config, rank):
    run = Path(config['run'])
    result = {'rank':rank,'nonce':config['nonce'],'status':'WAITING_ACTUAL_PID'}
    process = None
    try:
        deadline = time.monotonic()+config['timeout']+5
        while time.monotonic()<deadline:
            paths = list(run.glob('profile_rank%d_pid*_ready.json' % rank))
            if paths:
                if len(paths)!=1:
                    raise ValueError('phase marker PID ambiguous')
                # Atomicity: writer closes a tiny file; retry only incomplete JSON.
                try:
                    ready = json.loads(paths[0].read_text())
                except json.JSONDecodeError:
                    time.sleep(.02)
                    continue
                break
            time.sleep(.02)
        else:
            raise TimeoutError('actual application ready marker not produced')
        pid = ready['pid']
        if ready['rank']!=rank or ready['nonce']!=config['nonce']:
            raise ValueError('phase identity mismatch')
        start = identity(pid,config['binary'],config['nonce'],run)
        result.update(pid=pid,start_ticks=start)
        end_path = run / ('profile_rank%d_pid%d_end.json' % (rank,pid))
        # This is a post-Init sampling window, not exact OSU MPI_Wtime scope.
        time.sleep(.25)
        identity(pid,config['binary'],config['nonce'],run)
        if end_path.exists():
            raise RuntimeError('application finished before sampling; preserve short trial')
        command = record_command(pid,run/('perf_rank%d.data' % rank),3)
        with (run/('perf_rank%d.log' % rank)).open('x') as log:
            process = subprocess.Popen(command,stdout=log,stderr=log,start_new_session=True)
            perf_start = ticks(process.pid)
            until = time.monotonic()+5
            while process.poll() is None and time.monotonic()<until:
                if end_path.exists():
                    if ticks(process.pid)==perf_start:
                        os.killpg(process.pid,signal.SIGINT)
                    break
                time.sleep(.03)
            code = process.wait(timeout=5)
        result.update(command=command,exit=code,status='RECORDED')
        # Wait until the application ends its phase before accepting coverage.
        while not end_path.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        end = json.loads(end_path.read_text())
        if end['pid']!=pid or end['nonce']!=config['nonce'] or end['begin_ns']!=ready['begin_ns']:
            raise ValueError('end phase identity mismatch')
        command = ['perf','script','--ns','-i',str(run/('perf_rank%d.data' % rank)),
                   '-F','pid,time,event,ip,sym,dso']
        with (run/('perf_rank%d.script' % rank)).open('x') as output, \
                (run/('perf_rank%d.script.stderr' % rank)).open('x') as errors:
            parsed = subprocess.run(command,stdout=output,stderr=errors,timeout=15)
        if parsed.returncode:
            raise RuntimeError('perf script failed')
        result['analysis'] = summarize((run/('perf_rank%d.script' % rank)).read_text(),pid,
                                     ready['begin_ns']+250000000,end['at_ns'])
        result['status'] = result['analysis']['status']
        # Return130/-SIGINT is valid only for our deliberate end-marker stop.
        if code not in (0,130,-signal.SIGINT):
            result['status']='PERF_RECORD_FAILED'
    except Exception as exc:
        result.update(status='SAMPLING_UNAVAILABLE',error=type(exc).__name__+': '+str(exc))
    finally:
        if process is not None and process.poll() is None:
            # Child Popen handle still owns this exact PID; never signal a target app.
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        write(run/('perf_rank%d.json' % rank),result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path',type=Path)
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument('--rank',type=int)
    args = parser.parse_args()
    if args.preflight:
        print(json.dumps(preflight(args.path)))
    else:
        agent(json.loads(args.path.read_text()),args.rank)


if __name__=='__main__':
    main()
