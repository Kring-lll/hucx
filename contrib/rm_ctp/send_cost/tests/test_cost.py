import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
sys.path.insert(0, str(ROOT))
import instrument
import profile_read as reader
import run_cost


def fixture(pid=42, host=b'node89', mode=1, library=b'/candidate/install/lib/libucs.so.0'):
    header = reader.HEADER.pack(2, library, b'osu', host, pid, mode, 2, 2, 1000000000)
    locations = reader.LOCATION.pack(b'file.c', b'func', b'', 7, 1)
    locations += reader.LOCATION.pack(b'file.c', b'func', b'rmc_empty', 8, 2)
    threads = b''
    for tid, ticks, count in ((100, 300, 3), (101, 200, 2)):
        threads += reader.THREAD.pack(tid, 10, 1000, 0)
        threads += reader.ACCUM.pack(0, count)+reader.ACCUM.pack(ticks, count)
    return header+locations+threads


class ReaderTests(unittest.TestCase):
    def parse(self, data, **kw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'x.prof'
            path.write_bytes(data)
            return reader.read(path, kw.get('pid', 42), kw.get('host', 'node89'), Path('/candidate/install'))

    def test_real_packed_layout_and_multi_thread_aggregation(self):
        result = self.parse(fixture())
        self.assertEqual(reader.HEADER.size, 2140)
        self.assertEqual(reader.LOCATION.size, 165)
        self.assertEqual(reader.THREAD.size, 28)
        row = result['scopes'][0]
        self.assertEqual((row['calls'], row['total_ns'], row['mean_ns']), (5, 500, 100))

    def test_wrong_pid_host_mode_library_rejected(self):
        for options in ({'pid':43}, {'host':b'node97'}, {'mode':3}, {'library':b'/old/lib/libucs.so.0'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.parse(fixture(**options))

    def test_truncation_and_extra_bytes_rejected(self):
        for n in (0, 10, 2139, len(fixture())-1):
            with self.subTest(length=n), self.assertRaises(ValueError):
                self.parse(fixture()[:n])
        with self.assertRaises(ValueError):
            self.parse(fixture()+b'x')

    def test_version_and_location_limits_rejected(self):
        data = bytearray(fixture())
        struct.pack_into('<I', data, 0, 3)
        with self.assertRaises(ValueError): self.parse(data)
        data = bytearray(fixture())
        struct.pack_into('<I', data, 2124, 3000)
        with self.assertRaises(ValueError): self.parse(data)


class InstrumentTests(unittest.TestCase):
    def test_all_pinned_transformations_and_source_tree_unchanged(self):
        expected = json.loads((ROOT/'source_identity.json').read_text())
        for name in instrument.FILES:
            path = REPO/name
            self.assertEqual(instrument.digest(path), expected[name])
            result = instrument.transform(name, path.read_text())
            self.assertIn('rm_send_cost_diag.h', result)
            self.assertIn('COST_', result)
            with self.assertRaises(ValueError): instrument.transform(name, result)
        result = instrument.transform(instrument.FILES[2], (REPO/instrument.FILES[2]).read_text())
        self.assertIn('use_inline ? NULL : entry->desc->tseg', result)
        self.assertIn('use_inline ? URMA_INLINE_ENABLE : URMA_INLINE_DISABLE', result)

    def test_apply_private_copy_and_fail_closed_on_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in (*instrument.FILES, 'contrib/rm_ctp/r1/candidate_manifest.json'):
                target = source/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO/name, target)
            before = (source/instrument.FILES[0]).read_bytes()
            path = source/instrument.FILES[-1]
            path.write_bytes(path.read_bytes()+b'\n')
            with self.assertRaises(ValueError): instrument.apply(source)
            self.assertEqual(before, (source/instrument.FILES[0]).read_bytes())
            shutil.copy2(REPO/instrument.FILES[-1], path)
            changed = instrument.apply(source)
            self.assertEqual(len(changed), 6)
            manifest = json.loads((source/'contrib/rm_ctp/r1/candidate_manifest.json').read_text())
            for name in instrument.FILES:
                self.assertEqual(manifest['files'][name], instrument.digest(source/name))

    def test_no_retry_or_ack_policy_edits(self):
        result = instrument.transform(instrument.FILES[0], (REPO/instrument.FILES[0]).read_text())
        for token in ('unsigned budget = 8;', 'now - entry->posted_at < iface->reliable.rto',
                      '!peer->reliable.ack_in_flight && budget', 'peer->reliable.rx_next - 1'):
            self.assertIn(token, result)


class NativeTests(unittest.TestCase):
    def test_real_profile_macros_and_diag_calls_preserve_single_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            for name, text in {
                'ucs/config/global_opts.h':'#include <stdint.h>\n#include <stddef.h>\ntypedef int ucs_status_t;\n',
                'ucs/sys/compiler_def.h':'#define BEGIN_C_DECLS\n#define END_C_DECLS\n#define UCS_S_PACKED __attribute__((packed))\n#define ucs_typeof(x) __typeof__(x)\n#define ucs_compiler_fence() __asm__ __volatile__("" ::: "memory")\n',
                'ucs/time/time_def.h':'typedef uint64_t ucs_time_t;\n',
            }.items():
                path = out/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
            source = out/'main.c'
            source.write_text(r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include "diag.h"
int hucx_profile_active;
static int records, invocations, amount;
ucs_profile_context_t *ucs_profile_default_ctx;
void ucs_profile_record(ucs_profile_context_t *ctx, ucs_profile_type_t type,
 const char *name,uint32_t p32,uint64_t p64,const char *file,int line,
 const char *func,volatile ucs_profile_loc_id_t *loc) {
 (void)ctx;(void)type;(void)name;(void)p32;(void)p64;(void)file;(void)line;(void)func;(void)loc; ++records;errno=29;
}
void hucx_cost_add(unsigned id,unsigned long long n) { assert(id==2);amount+=(int)n; }
static int call(int n) { ++invocations; errno=17; return n+10; }
static void action(int n) { invocations+=n; }
int main(void) {
 for(int mode=0;mode<2;++mode) {
  hucx_profile_active=mode;int arg=0;records=invocations=amount=0;
  int result=COST_CALL("rmc_test",call,++arg);
  assert(result==11 && arg==1 && invocations==1 && errno==17 && records==2*mode);
  COST_VOID("rmc_void",action,2);assert(invocations==3 && records==4*mode);
  COST_ADD(2,3);assert(amount==3*mode);
  (void)COST_CALL("rmc_empty",hucx_cost_empty);assert(records==6*mode);
 }
 return 0;
}
''')
            command = ['gcc','-std=gnu11','-O2','-Wall','-Wextra','-Werror',
                       '-I'+str(ROOT),'-I'+str(out),'-I'+str(REPO/'src'), str(source),'-o',str(out/'test')]
            # The actual diagnostic has weak declarations in libuct and
            # definitions in a separate preload DSO. Keep separate TUs here too.
            text = source.read_text().replace('static int records, invocations, amount;',
                                             'int records, invocations, amount;')
            text = text.replace('static int call(', 'int call(').replace('static void action(', 'void action(')
            prefix, main = text.split('int main(void)', 1)
            provider = out/'provider.c'; provider.write_text(prefix)
            source.write_text('#include <assert.h>\n#include <errno.h>\n#include "diag.h"\n'
                              'extern int records,invocations,amount; int call(int); void action(int);\n'
                              'int main(void)'+main)
            command.insert(-2, str(provider))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(out/'test')], check=True)

    def test_phase_forwarding_and_init_finalize_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            provider = ROOT.parent/'send_profile/tests/mpi_provider.c'
            subprocess.run(['gcc','-shared','-fPIC',str(provider),'-o',str(out/'libmpi.so')], check=True)
            subprocess.run(['gcc','-std=gnu11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                            '-DPROFILE_PHASE_ONLY',str(ROOT/'phase.c'),'-ldl','-o',str(out/'phase.so')], check=True)
            source = out/'client.c'
            source.write_text('''#include <assert.h>
#include <dlfcn.h>
int MPI_Init(int*,char***); int MPI_Init_thread(int*,char***,int,int*); int MPI_Finalize(void);
int main(int argc,char **argv) {
 void (*add)(unsigned,unsigned long long)=dlsym(RTLD_DEFAULT,"hucx_cost_add");
 int *active=dlsym(RTLD_DEFAULT,"hucx_profile_active");assert(add && active && !*active);
 if (argc>1) {int p;assert(!MPI_Init_thread(&argc,&argv,0,&p));} else assert(!MPI_Init(&argc,&argv));
 if (*active) add(2,3);assert(!MPI_Finalize() && !*active);return 0;
}''')
            subprocess.run(['gcc',str(source),'-L'+str(out),'-Wl,-rpath,'+str(out),'-lmpi','-ldl',
                            '-o',str(out/'client')], check=True)
            for enabled, init_thread in ((False,False),(True,False),(True,True)):
                run=out/str((enabled,init_thread)); run.mkdir()
                env=dict(os.environ,LD_PRELOAD=str(out/'phase.so'),HUCX_OSU_PROBE_DIR=str(run),
                         OMPI_COMM_WORLD_RANK='0',A89_RUN_NONCE='a'*32,HUCX_SEND_COST_MODE='on' if enabled else 'off')
                subprocess.run([str(out/'client')]+(['thread'] if init_thread else []),env=env,check=True)
                final=next(p for p in run.glob('profile*.json') if not p.stem.endswith(('_ready','_end')))
                row=json.loads(final.read_text())
                self.assertTrue(row['finalized'])
                self.assertEqual(row['events']['rx_post_calls'],3 if enabled else 0)
                self.assertGreaterEqual(row['user_cpu_ns'],0)


class ControllerTests(unittest.TestCase):
    def test_schedule_rotates_and_covers_exact_workloads(self):
        rows=run_cost.schedule()
        self.assertEqual(len(rows),27)
        self.assertTrue(all(stage=='accepted' and phase=='validation' for stage,phase,_,_ in rows[:3]))
        for mode in run_cost.MODES:
            picked=[r for r in rows if r[3]==mode]
            self.assertEqual(len(picked),9)
            self.assertEqual([r[2] for r in picked if r[0]=='accepted' and r[1]=='timing'],list(range(5)))

    def test_bounded_plan_and_default_no_devices(self):
        plan=run_cost.plan()
        self.assertEqual(plan['total_mpi'],54)
        self.assertFalse(plan['accepted_product_changed'])
        self.assertEqual(plan['max_ppn'],4)
        self.assertEqual(plan['bytes'],8)
        result=subprocess.run([sys.executable,str(ROOT/'run_cost.py')],text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(result.stdout)['total_mpi'],54)

    def test_failed_trial_never_summarized(self):
        with self.assertRaises(ValueError):run_cost.summarize([], 'osu_multi_lat',1)

    def test_actual_runner_environment_and_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);case=out/'case';case.mkdir()
            item=dict(benchmark='osu_multi_lat',ppn=1,profile='reliable_v3')
            from contextlib import nullcontext
            for stage in ('accepted','off','on'):
                def fake(*args,**kwargs):
                    env=run_cost.osu.environment({},'iface',case/'install',run_cost.osu.budget(1))
                    self.assertEqual(env['UCX_PROFILE_MODE'],'accum' if stage=='on' else '')
                    self.assertEqual('libsend_cost_phase.so' in env['LD_PRELOAD'],stage!='accepted')
                    self.assertEqual(env['UCX_RM_URMA_LINK_MODE'],'reliable_v3')
                    run=out/'osu_multi_lat_rm_p1_diagnostic_0';run.mkdir(exist_ok=True)
                    return dict(name=run.name,passed=False,error='fixture')
                previous=run_cost.osu.environment
                with patch.object(run_cost,'bound',return_value=nullcontext()),patch.object(run_cost.osu,'one_trial',side_effect=fake):
                    row=run_cost.trial(case,{'module_sha256':'a'*64},out,{}, {'inventory':[]},item,'diagnostic',0,stage)
                self.assertFalse(row['passed'])
                self.assertIs(run_cost.osu.environment,previous)


if __name__=='__main__':unittest.main()
