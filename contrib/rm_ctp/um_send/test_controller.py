"""CPU gates for isolation/configuration, UM provenance and strict OSU parsing."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('rm_um_candidate',HERE/'run_candidate.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Gates(unittest.TestCase):
    def setUp(self):m.PROFILE='rm';m.OBSERVE=m.FAULT=False
    def test_clean_environment(self):
        source={'UCX_RM_URMA_LINK_MODE':'reliable_v3','UCX_RM_URMA_MAX_PEERS':'8',
            'UCX_RM_URMA_NATIVE_RMA':'y','MPI_UCX_TLS':'tcp','LD_DEBUG':'libs',
            'HUCX_UM_SEND_FAULT':'loss_dup','OMPI_MCA_osc':'rdma','PATH':'/usr/bin'}
        env=m.environment(source,'iface',Path('/candidate/install'),{})
        self.assertEqual(env['UCX_TLS'],'rm_urma,self')
        self.assertFalse(set(source)-{'PATH'} & env.keys())
        self.assertNotIn('SEG_SIZE',' '.join(env))
    def test_three_profiles(self):
        for profile in ('rc','um','rm'):
            m.PROFILE=profile
            env=m.environment({},profile,Path('/candidate/install'),{})
            self.assertEqual(env['UCX_TLS'],'rc_urma,um_urma:aux,self' if profile=='rc' else profile+'_urma,self')
            self.assertTrue(all(k.startswith('UCX_'+profile.upper()+'_URMA_')
                for k in env if '_URMA_' in k))
    def test_observer_absent_from_timing(self):
        m.PROBE=Path('/probe.so');m.FAULT=True
        env=m.environment({},'iface',Path('/candidate/install'),{})
        self.assertNotIn('/probe.so',env['LD_PRELOAD'])
        self.assertNotIn('HUCX_UM_SEND_FAULT',env)
    def test_effective_units_and_old_mode_rejected(self):
        env=m.environment({},'iface',Path('/candidate/install'),{})
        text='\n'.join(k+'='+v for k,v in env.items() if k.startswith('UCX_RM_URMA_'))
        text=text.replace('TIMEOUT=10s','TIMEOUT=10.00s')+'\nUCX_RM_URMA_SEG_SIZE=4K\n'
        m.check_config(text,env)
        with self.assertRaises(ValueError):m.check_config(text.replace('4K','8K'),env)
        with self.assertRaises(ValueError):m.check_config(text+'UCX_RM_URMA_LINK_MODE=iface_v2\n',env)
    def test_plan_covers_real_boundaries(self):
        p=m.plan();self.assertEqual(len(p),19)
        self.assertEqual(len(m.plan(True)),20)
        self.assertEqual(max(r[2] for r in p),4)
        self.assertEqual(max(r[3] for r in p),1048576)
        self.assertTrue(all(n in [r[3] for r in p] for n in (4079,4080,4081,8135,8136,8137)))
        self.assertEqual(sum(r[4] for r in p),1)
    def test_ucx_time_units(self):
        for actual, expected in [('10000000.00us','10s'),('10000ms','10.00s'),
                ('10000000000ns','10s'),('1m','60s'),('1h','3600s'),
                ('1d','86400s'),('0.1us','100ns'),(' 10.00S ','10000ms')]:
            with self.subTest(actual=actual):
                self.assertEqual(m.time_seconds(actual),m.time_seconds(expected))
    def test_invalid_ucx_time_rejected(self):
        for value in ('', 'nan', 'inf', '-1s', '10', '10fortnights', 'x10s', '10s extra'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.time_seconds(value)
    def test_actual_node_config_and_wrong_value(self):
        env=m.environment({},'iface',Path('/candidate/install'),{})
        text='\n'.join(k+'='+v for k,v in env.items() if k.startswith('UCX_RM_URMA_'))
        text=text.replace('TIMEOUT=10s','TIMEOUT=10000000.00us')+'\nUCX_RM_URMA_SEG_SIZE=4K\n'
        m.check_config(text,env)
        for wrong in ('1000000.00us','10000001.00us','20s','inf'):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                m.check_config(text.replace('10000000.00us',wrong),env)
    def test_original_osu_parser(self):
        t='# OSU MPI Multi Latency Test v7.5.2\n8 2.5 Pass\n'
        self.assertTrue(m.osu.parse_output(t,'osu_multi_lat',True,100)['validation_pass'])
        for bad in (t+t,t.replace('Pass','Fail'),t.replace('2.5','nan')):
            with self.assertRaises(ValueError):m.osu.parse_output(bad,'osu_multi_lat',True,100)
    def test_um_structs_and_headers_unchanged(self):
        root=HERE.parents[2]/'src/uct/urma'
        for name in ('def.h','inl.h','ep.h','iface.h'):
            original=(root/'um_urma'/('um_urma_'+name)).read_text()
            expected=original.replace('um_urma','rm_urma').replace('UM_URMA','RM_URMA')
            self.assertEqual(expected,(root/'rm_urma'/('rm_urma_'+name)).read_text())
        self.assertEqual(len(list((root/'rm_urma').glob('*.[ch]'))),6)
    def test_bad_case_rejected_before_access(self):
        with self.assertRaises(ValueError):m.validate_source(Path('/tmp/old-case'))

if __name__=='__main__':unittest.main()
