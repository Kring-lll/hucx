import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import ExitStack,redirect_stdout
from types import SimpleNamespace
import io

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import run_candidate as runner


def summary(length=123,limit=128,inline=1):
    return {'schema':1,'rank':0,'pid':123,'nonce':'a'*32,'calls':1,'error_calls':0,
            'incomplete':0,'provider_is_liburma':True,'buckets':[dict(bytes=length,num_sge=1,
            inline=inline,inline_limit=limit,complete_enable=1,reliable_kind=1,submitted=1,success=1)]}


class EvidenceTests(unittest.TestCase):
    def test_exact_limit_and_fallback_data_ack(self):
        for size,cap in ((39,39),(123,128),(128,128),(129,128),(4096,128),(123,0)):
            with self.subTest(size=size,cap=cap):
                value=summary(size,cap,int(size<=cap))
                runner.inspect_inline(value,0,123,'a'*32,size)

    def test_wrong_decision_identity_and_missing_boundary_rejected(self):
        for value in (summary(129,128,1),summary(123,128,0),dict(summary(),pid=124)):
            with self.subTest(value=value),self.assertRaises(ValueError):
                runner.inspect_inline(value,0,123,'a'*32)
        with self.assertRaises(ValueError):runner.inspect_inline(summary(),0,123,'a'*32,128)

    def test_old_reliable_must_remain_noninline(self):
        runner.inspect_inline(summary(inline=0),0,123,'a'*32,baseline=True)
        with self.assertRaises(ValueError):runner.inspect_inline(summary(),0,123,'a'*32,baseline=True)

    def test_binding_restores_old_case_and_original_callback(self):
        old=runner.previous.CASE;capture=runner.integration.capture_configuration
        default=runner.osu.DEFAULT_CASE
        with runner.bound(Path('/isolated'),{'source_commit':'b'*40,'module_sha256':'c'*64}):
            self.assertEqual(runner.previous.CASE,Path('/isolated'))
            self.assertEqual(runner.osu.DEFAULT_CASE,Path('/isolated'))
        self.assertEqual(runner.previous.CASE,old)
        self.assertEqual(runner.osu.DEFAULT_CASE,default)
        self.assertIs(runner.integration.capture_configuration,capture)

    def test_comparison_keeps_all_five_samples_including_outlier(self):
        item=dict(benchmark='osu_alltoall',ppn=4,size=8,path='eager')
        rows=[]
        for label in runner.LABELS:
            for phase,count in (('validation',1),('timing',5),('resources',1)):
                for index in range(count):
                    rows.append(dict(item,comparison_label=label,phase=phase,passed=True,
                        output={'value':28.1 if index==4 else 7},rank_evidence={'before_finalize_rss_sum_mib':400},jettys={'fixture':True}))
        result=runner.compare(rows,item)
        self.assertEqual(result['profiles']['new_reliable']['max_us'],28.1)
        self.assertEqual(len(result['profiles']['new_reliable']['samples_us']),5)
        with self.assertRaises(ValueError):runner.compare(rows[:-1],item)

    def test_failed_validation_does_not_collect_or_declare_inline_success(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);(out/'failed').mkdir()
            with mock.patch.object(runner.previous,'run_trial',return_value={'name':'failed','passed':False,'error':'fixture'}),\
                 mock.patch.object(runner,'add_inline_evidence') as inspect:
                value=runner.one(Path('/isolated'),out,{'source_commit':'b'*40,'module_sha256':'c'*64},
                    {},{}, {},dict(benchmark='osu_multi_lat',ppn=1,size=8,path='eager'),
                    'validation',0,'new_reliable')
                self.assertFalse(value['passed']);inspect.assert_not_called()

    def test_real_observer_compiles_and_serializes_extra_metadata(self):
        # Explicit CPU-only header fixture; never passed to the node compiler.
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            old=ROOT.parent/'send_profile/tests'
            header=(old/'urma_api.h').read_text().replace('int trans_mode;','int trans_mode;unsigned max_inline_data;')
            (out/'urma_api.h').write_text(header)
            for source,target,extra in ((ROOT/'send_probe.c','observer.so',['-shared','-fPIC','-ldl','-pthread']),
                                        (old/'provider.c','liburma.so',['-shared','-fPIC'])):
                subprocess.run(['gcc','-std=gnu11','-Wall','-Wextra','-Werror','-I'+str(out),
                                str(source),'-o',str(out/target),*extra],check=True,capture_output=True)
            code='''import ctypes,os
provider=ctypes.CDLL(os.environ["TEST_PROVIDER"],mode=os.RTLD_LOCAL)
class S(ctypes.Structure): _fields_=[("addr",ctypes.c_uint64),("len",ctypes.c_uint32)]
class SRC(ctypes.Structure): _fields_=[("count",ctypes.c_uint), ("sge",ctypes.POINTER(S))]
class FLAG(ctypes.Structure): _fields_=[("inline",ctypes.c_uint),("complete",ctypes.c_uint)]
class W(ctypes.Structure): pass
W._fields_=[("opcode",ctypes.c_int),("flags",FLAG),("src",SRC),("next",ctypes.POINTER(W))]
class J(ctypes.Structure): _fields_=[("mode",ctypes.c_int),("cap",ctypes.c_uint)]
wire=ctypes.create_string_buffer(bytes(123));s=S(ctypes.addressof(wire),123)
w=W(2,FLAG(1,1),SRC(1,ctypes.pointer(s)),None);j=J(1,128);bad=ctypes.POINTER(W)()
assert ctypes.CDLL(None).urma_post_jetty_send_wr(ctypes.byref(j),ctypes.byref(w),ctypes.byref(bad))==0
'''
            env=dict(os.environ,LD_PRELOAD=str(out/'observer.so'),TEST_PROVIDER=str(out/'liburma.so'),
                     HUCX_OSU_PROBE_DIR=str(out),OMPI_COMM_WORLD_RANK='0',A89_RUN_NONCE='a'*32)
            subprocess.run([sys.executable,'-c',code],env=env,check=True,capture_output=True)
            row=json.loads(next(out.glob('wr_rank0_pid*.json')).read_text())
            self.assertEqual(row['buckets'][0]['inline_limit'],128)
            self.assertEqual(row['buckets'][0]['complete_enable'],1)


class OrchestrationTests(unittest.TestCase):
    def exercise(self,out,fail=False):
        candidate=Path('/isolated_inline_case')
        build={'source_commit':'b'*40,'module_sha256':'c'*64}
        old_build={'source_commit':runner.integration.matrix.COMMIT,'module_sha256':runner.integration.matrix.MODULE}
        called=[]
        def preflight(directory,commands):
            (directory/'libosu_identity.so').write_bytes(b'identity fixture')
            return {'inventory':[],'benchmarks':{}}
        def compile_helpers(case,directory,commands):
            result={}
            for name,file in (('mpi_probe','mpi_probe'),('injector','libsend_drop.so'),('wr_probe','libsend_wr_probe.so')):
                p=directory/file;p.write_bytes(b'fixture');result[name]=p
            return result
        def logged(command,*args):
            Path(command[command.index('-o')+1]).write_bytes(b'fixture')
        def run_trial(case,directory,commands,evidence,helpers,item,phase,index,repeat,inject):
            self.assertEqual(runner.osu.DEFAULT_CASE,case)
            self.assertEqual(runner.previous.CASE,case)
            self.assertIs(runner.integration.capture_configuration,runner.previous.config_capture)
            name='trial_'+str(index);(directory/name).mkdir()
            called.append((phase,case))
            return dict(item,name=name,phase=phase,passed=not fail,exit=0,error='fixture' if fail else '',
                        output={'value':2.5},rank_evidence={'before_finalize_rss_sum_mib':100},
                        jettys={'fixture':True},production_retransmit_posts=3)
        def failure(integration,case,directory,commands,binary,library,item):
            self.assertEqual(case,candidate)
            return dict(item,name='failure_'+item['limit']+'_'+item['fault'],passed=True)
        def inline_evidence(row,*args,**kwargs):
            row['inline_evidence']=[{'data_buckets':[dict(bytes=row.get('expected_wr'),inline_limit=128,inline=1)]}]
        base=SimpleNamespace(deploy=mock.Mock(return_value={'fixture':True}),normal_regression=mock.Mock(return_value={'cases':28,'passed':True}))
        spec=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module:None))
        fake_import=SimpleNamespace(util=SimpleNamespace(spec_from_file_location=lambda *args:spec,
                    module_from_spec=lambda item:SimpleNamespace(base=base)))
        with ExitStack() as stack:
            for target,name,value in (
                (runner,'importlib',fake_import),(runner,'load_prior',lambda:{}),
                (runner,'sdk_headers',lambda:{}),
                (runner,'add_inline_evidence',inline_evidence),
                (runner.integration,'validate_case',lambda c:old_build),
                (runner.osu,'preflight',preflight),(runner.osu,'saved_spec',lambda *args:(['mpirun'],{})),
                (runner.integration,'compile_helpers',compile_helpers),(runner.integration,'copy_checked',lambda *args:None),
                (runner.integration,'run_custom',lambda case,directory,commands,evidence,helpers,ppn,injection:dict(name='alternating_'+str(ppn),passed=True)),
                (runner.integration,'collect_diagnostics',lambda *args,**kwargs:None),
                (runner.osu.base,'ssh',lambda *args,**kwargs:''),(runner.osu.base,'run_logged',logged),
                (runner.previous,'run_trial',run_trial),
                (runner.previous.failure,'compile_probe',lambda *args:(Path('/probe'),Path('/injector'))),
                (runner.previous.failure,'run',failure)):
                stack.enter_context(mock.patch.object(target,name,value))
            with redirect_stdout(io.StringIO()):
                if fail:
                    with self.assertRaises(RuntimeError):runner.execute(candidate,build,out,True)
                else:
                    runner.execute(candidate,build,out,True)
        return json.loads((out/'results.json').read_text()),called,base

    def test_complete_controller_rebinds_two_installs_and_retains_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            report,called,base=self.exercise(Path(directory))
            self.assertEqual(len(report['trials']),56)
            self.assertEqual(len(report['failures']),6)
            self.assertEqual(len(report['comparisons']),2)
            self.assertEqual(sum(phase=='timing' for phase,case in called),30)
            self.assertIn(('timing',runner.OLD_CASE),called)
            base.normal_regression.assert_called_once()

    def test_first_data_failure_stops_before_all_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            report,called,base=self.exercise(Path(directory),True)
            self.assertEqual(report['status'],'STOPPED_INCOMPLETE')
            self.assertEqual(len(called),1)
            self.assertEqual(report['comparisons'],[])


if __name__=='__main__':unittest.main()
