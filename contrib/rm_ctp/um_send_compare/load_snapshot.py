"""Read-only launcher-boundary load evidence, never a timed-loop sampler."""
import json
import os
from pathlib import Path
import socket
import time


def snapshot(proc=Path('/proc')):
    row={'hostname':socket.gethostname(),'wall_time_ns':time.time_ns(),
         'monotonic_ns':time.monotonic_ns(),'clock_ticks':os.sysconf('SC_CLK_TCK'),
         'loadavg':(proc/'loadavg').read_text().strip(),'cpus':{},'processes':{},'warnings':[]}
    text=(proc/'stat').read_text()
    for line in text.splitlines():
        fields=line.split()
        if fields[0].startswith('cpu'):
            row['cpus'][fields[0]]=[int(v) for v in fields[1:9]]
        elif fields[0] in ('ctxt','procs_running','procs_blocked'):
            row[fields[0]]=int(fields[1])
    row['memory']={s.split(':',1)[0]:s.split(':',1)[1].strip()
                   for s in (proc/'meminfo').read_text().splitlines()
                   if s.startswith(('MemAvailable:','SwapFree:','Dirty:'))}
    row['pressure']={}
    for name in ('cpu','memory','io'):
        try:row['pressure'][name]=(proc/'pressure'/name).read_text().strip()
        except OSError as exc:row['warnings'].append('pressure '+name+': '+type(exc).__name__)
    try:row['softirqs']=(proc/'softirqs').read_text()
    except OSError as exc:row['warnings'].append('softirqs: '+type(exc).__name__)
    for p in proc.iterdir():
        if not p.name.isdigit():continue
        try:
            s=(p/'stat').read_text();closing=s.rfind(')');fields=s[closing+2:].split()
            # fields[0] is Linux stat field3; starttime is field22.
            row['processes'][p.name]={'comm':s[s.find('(')+1:closing],
                'start_ticks':int(fields[19]),'cpu_ticks':int(fields[11])+int(fields[12]),
                'last_cpu':int(fields[36]),'allowed_cpus':sorted(os.sched_getaffinity(int(p.name)))}
        except (OSError,ValueError,IndexError):continue
    return row


def compare(before,after,rank_cpus,own_pids=()):
    if before['hostname'] != after['hostname'] or after['monotonic_ns'] <= before['monotonic_ns']:
        raise ValueError('load snapshot host/time mismatch')
    selected={int(c) for c in rank_cpus};own={str(v) for v in own_pids}
    cpus={}
    for c in sorted(selected):
        key='cpu'+str(c)
        a,b=before['cpus'].get(key),after['cpus'].get(key)
        if a is None or b is None:continue
        delta=[y-x for x,y in zip(a,b)];total=sum(delta)
        if total<=0 or min(delta)<0:continue
        cpus[key]={'total_ticks':total,'busy_pct':100*(total-delta[3]-delta[4])/total,
                   'iowait_ticks':delta[4],'irq_ticks':delta[5],'softirq_ticks':delta[6],'steal_ticks':delta[7]}
    active=[]
    for pid,b in after['processes'].items():
        a=before['processes'].get(pid)
        if pid in own or a is None or a['start_ticks']!=b['start_ticks']:continue
        ticks=b['cpu_ticks']-a['cpu_ticks']
        if ticks>0:
            active.append({'pid':int(pid),'comm':b['comm'],'cpu_ticks_delta':ticks,
                           'last_cpu':b['last_cpu'],
                           'affinity_intersects_rank_cpus':bool(selected.intersection(b['allowed_cpus']))})
    active.sort(key=lambda r:r['cpu_ticks_delta'],reverse=True)
    return {'hostname':after['hostname'],'interval_s':(after['monotonic_ns']-before['monotonic_ns'])/1e9,
            'rank_cpus':sorted(selected),'cpu_interval':cpus,'active_existing_processes_top20':active[:20],
            'scope':'before/after complete launcher including startup/finalize; not OSU timed-loop attribution',
            'limits':'misses processes exiting before after-snapshot, short bursts, offloaded URMA traffic; affinity overlap is only possible contention'}


if __name__=='__main__':print(json.dumps(snapshot(),sort_keys=True))
