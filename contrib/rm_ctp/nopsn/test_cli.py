"""Exercise every real one_trial configuration before the MPI boundary; CPU only."""
import functools
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('nopsn_cli_candidate',ROOT/'run_candidate.py')
n=importlib.util.module_from_spec(spec);spec.loader.exec_module(n)


class CliTests(unittest.TestCase):
    def test_original_failure_and_all_planned_formatters(self):
        engine,policy,scale,runner=n.load_engine()
        with self.assertRaisesRegex(ValueError,'unplanned OSU workload'):
            policy.arguments('osu_multi_lat',100,10,True,64)
        self.assertEqual(n.cli_preflight(scale.arguments,True)['planned_tasks'],215)
        self.assertEqual(n.cli_preflight(scale.arguments,False)['planned_tasks'],47)
        for item in n.schedule(True):
            _,b,_,size,phase,_,_,_=item
            args=n.workload_arguments(scale.arguments,b,100,10,phase=='validation',size)
            if b=='nopsn_alternating':self.assertEqual(args,[])
            elif b=='osu_multi_lat':
                self.assertEqual(args,['--message-size','%d:%d'%(size,size),'--iterations','100','--warmup','10','--validation'])
            else:self.assertEqual(args,scale.arguments(b,100,10,phase=='validation',size))
        for size,validation in ((63,True),(64,False),(1048577,True)):
            with self.assertRaises(ValueError):n.workload_arguments(scale.arguments,'osu_multi_lat',100,10,validation,size)
        with self.assertRaises(ValueError):policy.arguments('osu_multi_lat',100,10,True,64)

    def test_actual_one_trial_configuration_for_all_213_osu_tasks(self):
        engine,policy,scale,runner=n.load_engine();osu=engine['osu']
        inv=[{'hostname':h,'boot_id':'fixed','allowed_cpus':list(range(192)),
            'available_cores':192,'mem_available_kib':128*1024**2} for h in ('node89','node97')]
        def write(path,value):Path(path).write_text(json.dumps(value))
        def stop_before_mpi(*a,**k):raise RuntimeError('CPU_FIXTURE_STOP_BEFORE_MPI')
        fakebase=SimpleNamespace(ssh=lambda *a,**k:'{"terminated_own_pids":[]}',inventory=lambda:inv,
            parsed_config=lambda *a:None,write_json=write,copy_remote=lambda *a:None,
            HOSTS=['node89','node97'],PEER='node97',run_logged=stop_before_mpi)
        benches=dict(scale.BENCHMARKS);benches['osu_multi_lat']=policy.BENCHMARKS['osu_multi_lat']
        evidence={'inventory':inv,'benchmarks':{b:{'mpi_library':'fixture'} for b in benches}}
        count=0
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            replacements={'base':fakebase,'DEFAULT_CASE':out,'BENCHMARKS':benches,
                'budget':lambda p:{'ppn':p,'rx_depth':4096,'tx_depth':64,'window':32},
                'environment':lambda *a:{'LD_PRELOAD':'fixture'},
                'arguments':functools.partial(n.workload_arguments,scale.arguments),
                'launcher':lambda *a:['fixture_no_mpi'],'fixed_mca':lambda c:c}
            with patch.dict(osu.__dict__,replacements),patch.object(osu.subprocess,'check_output',return_value='{"terminated_own_pids":[]}'):
                for index,item in enumerate(n.schedule(True)):
                    _,b,p,size,phase,_,_,_=item
                    if b=='nopsn_alternating':continue
                    trialargs=SimpleNamespace(iterations=100 if phase=='validation' else 10000 if b=='osu_latency' else 1000,
                        warmup=10 if phase=='validation' else 1000 if b=='osu_latency' else 100,timeout=300)
                    row=osu.one_trial(out,{'rm':(['/fixture/mpirun'],{})},evidence,b,'rm',p,phase,index,trialargs,message_bytes=size)
                    self.assertIn('CPU_FIXTURE_STOP_BEFORE_MPI',row.get('error',''))
                    config=json.loads((out/row['name']/'rank_config.json').read_text())
                    self.assertEqual(config['arguments'],n.workload_arguments(scale.arguments,b,trialargs.iterations,trialargs.warmup,phase=='validation',size))
                    self.assertEqual(config['resource_probe'],phase=='resources')
                    text=benches[b][1]+' v7.5.2\n'+str(size)+' 2.0'
                    if b=='osu_alltoall':text+=' 1.0 3.0 '+str(row['iterations'])
                    if phase=='validation':text+=' Pass'
                    parsed=(policy.parse_output if b=='osu_multi_lat' else scale.parse_output)(text,b,phase=='validation',row['iterations'],size)
                    self.assertTrue(parsed['value']>0)
                    count+=1
        self.assertEqual(count,213)


if __name__=='__main__':unittest.main()
