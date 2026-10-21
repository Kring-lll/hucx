#!/usr/bin/env python3
"""Summarize new per-run observer files and actual process maps only."""
import json
from pathlib import Path
import sys

def inspect(directory, mode, install):
    result={}
    for path in sorted(Path(directory).glob('rank*_pid*.jsonl')):
        rows=[json.loads(s) for s in path.read_text().splitlines()]
        rank=next(r['rank'] for r in rows if r.get('event')=='start')
        if rank<0: continue
        live={}; current={}; peak={}; depths=set()
        for row in rows:
            if row.get('valid',1)!=1: raise ValueError('observer invalid: '+str(path))
            if row.get('kind')!='jetty' or row.get('rc')!=0: continue
            if row['event']=='acquire' and row.get('mode')==mode:
                ctx=row['ctx']; live[row['ptr']]=ctx
                current[ctx]=current.get(ctx,0)+1
                peak[ctx]=max(peak.get(ctx,0),current[ctx])
                depths.add(row['depth'])
            elif row['event']=='release' and row['ptr'] in live:
                ctx=live.pop(row['ptr']); current[ctx]-=1
        if not peak or any(current.values()):
            raise ValueError('missing target-mode Jetty or resources remain at exit: '+str(path))
        maps=path.with_suffix('.maps')
        lines=maps.read_text().splitlines()
        for name in ('libucp','libuct','libucs','libucm'):
            if not any(install+'/lib/' in line and name in line for line in lines):
                raise ValueError('candidate '+name+' missing from '+str(maps))
        if not any(install+'/lib/ucx/' in line and 'libuct_ub' in line for line in lines):
            raise ValueError('candidate module missing from '+str(maps))
        if depths!={8}: raise ValueError('TX_DEPTH=8 was not applied: '+str(depths))
        if mode==1 and any(n!=1 for n in peak.values()):
            raise ValueError('RM Jetty peak is not one per observed context: '+str(peak))
        result[str(rank)]={'jetty_peak_by_context':peak,'tx_depth':8,'live_at_exit':0,'maps':'PASS'}
    if not result: raise ValueError('no ranked observer files in '+str(directory))
    return result

if __name__=='__main__':
    print(json.dumps(inspect(sys.argv[1],int(sys.argv[2]),sys.argv[3])))
