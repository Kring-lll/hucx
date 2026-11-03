"""Real subprocess regression for ucx_info log paths; no UCX/URMA device is used."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_validation as runner


class QueryEnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='send_query_constructor_')
        cls.root = Path(cls.work.name)
        cls.case = cls.root / 'accepted'
        cls.install = cls.case / 'install'
        (cls.install / 'bin').mkdir(parents=True)
        source = cls.root / 'query.c'
        # The constructor reproduces the log-before-main dependency. It is a
        # CPU fixture, not a replacement UCX implementation or hardware proof.
        source.write_text(r'''
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern char **environ;
__attribute__((constructor)) static void log_init(void)
{
    const char *path = getenv("UCX_LOG_FILE");
    if (path != NULL && path[0] != '\0') {
        FILE *log = fopen(path, "w");
        if (log == NULL) {
            fprintf(stderr, "QUERY_INIT_FAILED errno=%d path=%s\n", errno, path);
            exit(83);
        }
        fclose(log);
    }
}
int main(int argc, char **argv)
{
    if (getenv("TEST_QUERY_FAIL") != NULL) {
        fputs("QUERY_ROOT_CAUSE_FIRST_LINE\n", stderr);
        for (unsigned i = 0; i < 5000; ++i) {
            fputc('x', stderr);
        }
        fputs("\nQUERY_ERROR_LAST_LINE\n", stderr);
        return 23;
    }
    if (argc > 1 && !strcmp(argv[1], "-c")) {
        for (char **p = environ; *p != NULL; ++p) {
            if (!strncmp(*p, "UCX_", 4)) {
                puts(*p);
            }
        }
        return 0;
    }
    puts("# Device: udmac0d1e2:1");
    const char *mode = getenv("UCX_RM_URMA_LINK_MODE");
    puts(mode != NULL && !strcmp(mode,"reliable_v3") ?
         "# am_bcopy: <= 3989" : "# am_bcopy: <= 4093");
    return 0;
}
''')
        subprocess.run(['gcc','-std=gnu99','-Wall','-Wextra','-Werror',str(source),
                        '-o',str(cls.install / 'bin/ucx_info')], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def application_env(self, profile='reliable_v3'):
        env = runner.profile_environment(self.case,4,{},profile,'eager',self.root/'peer_missing/config')
        env.update(LD_PRELOAD='/unused/wr.so:/unused/drop.so',HUCX_SEND_INJECT_PROFILE='eager_middle',
                   HUCX_OSU_PROBE_DIR='/unused/run',RM_RESOURCE_OBSERVER_DIR='/unused/observer',
                   A89_RUN_NONCE='not-a-query-nonce',LD_DEBUG='libs')
        return env

    def execute_query(self, env):
        result = subprocess.run([sys.executable,'-c',runner.CONFIGURATION_QUERY,
                                 str(self.install),json.dumps(env),'rm_urma'],
                                capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def peer_query(self, script, query, install, environment, transport, **kwargs):
        return subprocess.check_output([sys.executable,'-c',query,str(install),environment,transport],
                                        text=True,timeout=10)

    def test_legacy_missing_log_parent_fails_before_configuration(self):
        env = self.application_env()
        env.pop('LD_PRELOAD')
        env.pop('LD_DEBUG')
        result = self.execute_query(env)
        self.assertEqual(result['config']['exit'],83)
        self.assertIn('QUERY_INIT_FAILED',result['config']['stderr'])
        self.assertEqual(result['config']['stdout'],'')

    def test_sanitized_query_works_without_peer_result_directory(self):
        for profile in runner.policy.MODES:
            app = self.application_env(profile)
            before = copy.deepcopy(app)
            query = runner.configuration_query_environment(self.install,app)
            result = self.execute_query(query)
            self.assertTrue(all(row['exit']==0 for row in result.values()))
            self.assertFalse((self.root/'peer_missing').exists())
            self.assertEqual(app,before)
            self.assertEqual(query['UCX_LOG_FILE'],'')
            self.assertEqual(query['LD_PRELOAD'],'')
            self.assertEqual(query['UCX_RM_URMA_LINK_MODE'],before['UCX_RM_URMA_LINK_MODE'])
            for key in before:
                if key.startswith('UCX_') and not key.startswith('UCX_LOG_'):
                    self.assertEqual(query[key],before[key])
            for key in ('HUCX_SEND_INJECT_PROFILE','HUCX_OSU_PROBE_DIR','LD_DEBUG',
                        'RM_RESOURCE_OBSERVER_DIR','A89_RUN_NONCE'):
                self.assertNotIn(key,query)

    def test_only_candidate_core_preloads_retained(self):
        core = str(self.install/'lib/libucp.so.0')
        app = self.application_env()
        app['LD_PRELOAD'] = '/unused/drop.so:' + core + ':/unused/resource_observer.so'
        query = runner.configuration_query_environment(self.install,app)
        self.assertEqual(query['LD_PRELOAD'],core)
        self.assertIn('/unused/drop.so',app['LD_PRELOAD'])

    def test_capture_all_modes_through_real_local_and_peer_subprocesses(self):
        for profile in runner.policy.MODES:
            with tempfile.TemporaryDirectory(dir=self.root) as temp:
                out = Path(temp)
                with mock.patch.object(runner,'CASE',self.case), \
                     mock.patch.object(runner.osu.base,'ssh',side_effect=self.peer_query):
                    runner.config_capture(self.install,self.application_env(profile),out,
                                          'rc' if profile=='rc' else 'iface')
                for host in ('node89','node97'):
                    receipt = json.loads((out/(host+'_query.json')).read_text())
                    self.assertTrue(all(value['exit']==0 for value in receipt['queries'].values()))
                    self.assertEqual(receipt['environment']['UCX_LOG_FILE'],'')
                    self.assertEqual((out/(host+'_ucx_config_stderr.txt')).read_text(),'')
                self.assertTrue((out/'effective_configuration.json').is_file())

    def test_complete_error_is_saved_not_only_backtrace_tail(self):
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            out = Path(temp)
            env = self.application_env()
            env['TEST_QUERY_FAIL'] = '1'
            with mock.patch.object(runner,'CASE',self.case), \
                 mock.patch.object(runner.osu.base,'ssh',side_effect=self.peer_query), \
                 self.assertRaisesRegex(RuntimeError,'EXIT=23'):
                runner.config_capture(self.install,env,out,'iface')
            for host in ('node89','node97'):
                text = (out/(host+'_ucx_config_stderr.txt')).read_text()
                self.assertTrue(text.startswith('QUERY_ROOT_CAUSE_FIRST_LINE'))
                self.assertIn('QUERY_ERROR_LAST_LINE',text)
                self.assertGreater(len(text),5000)


if __name__ == '__main__':
    unittest.main()
