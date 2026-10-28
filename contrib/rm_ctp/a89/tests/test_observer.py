"""Real dynamic-loader/timeout test; no MPI or URMA dependency."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rank_exec
import support

OBSERVER = r'''
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
__attribute__((constructor)) static void start(void)
{
    const char *directory = getenv("A89_TEST_OBS_DIR");
    char name[4096];
    if (directory == NULL) { return; }
    snprintf(name, sizeof(name), "%s/observer_%ld.txt", directory, (long)getpid());
    FILE *file = fopen(name, "w");
    if (file == NULL) { _exit(2); }
    fprintf(file, "%ld\n", (long)getpid());
    fclose(file);
}
'''
APPLICATION = r'''
#include <stdio.h>
#include <unistd.h>
int main(void) { printf("APP_PID=%ld\n", (long)getpid()); return 0; }
'''


class ObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='a89_loader_')
        cls.root = Path(cls.folder.name)
        for name, text in (('observer', OBSERVER), ('application', APPLICATION)):
            (cls.root / (name + '.c')).write_text(text)
        cls.observer = cls.root / 'observer.so'
        cls.application = cls.root / 'application'
        subprocess.run(['gcc', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(cls.root / 'observer.c'), '-o', str(cls.observer)], check=True)
        subprocess.run(['gcc', '-Wall', '-Wextra', '-Werror',
                        str(cls.root / 'application.c'), '-o', str(cls.application)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def config(self, directory, probe=True):
        return {'timeout': 5, 'run': str(directory), 'resource_probe': probe,
                'binary': str(self.application), 'arguments': [],
                'observers': {socket.gethostname(): str(self.observer)}}

    def test_old_timeout_preload_reproduces_two_observer_processes(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            env = dict(os.environ, A89_TEST_OBS_DIR=directory, LD_PRELOAD=str(self.observer))
            subprocess.run(['/usr/bin/timeout', '5s', str(self.application)], env=env,
                           check=True, capture_output=True, text=True, timeout=8)
            self.assertEqual(len(list(Path(directory).glob('observer_*.txt'))), 2)

    def test_fixed_preload_observes_only_actual_application_pid(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            env = dict(os.environ, A89_TEST_OBS_DIR=directory)
            env.pop('LD_PRELOAD', None)
            command, timeout_env, application_env = rank_exec.execution(self.config(directory), env)
            self.assertNotIn('RM_RESOURCE_OBSERVER_DIR', timeout_env)
            self.assertNotIn(str(self.observer), timeout_env.get('LD_PRELOAD', ''))
            self.assertIn(str(self.observer), application_env['LD_PRELOAD'])
            result = subprocess.run(command, env=timeout_env, check=True,
                                    capture_output=True, text=True, timeout=8)
            pid = int(result.stdout.strip().split('=')[1])
            logs = list(Path(directory).glob('observer_*.txt'))
            self.assertEqual([p.name for p in logs], ['observer_%d.txt' % pid])

    def test_timing_run_stays_unobserved(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            env = dict(os.environ, A89_TEST_OBS_DIR=directory)
            env.pop('LD_PRELOAD', None)
            command, timeout_env, _ = rank_exec.execution(self.config(directory, False), env)
            self.assertNotIn('/usr/bin/env', command)
            subprocess.run(command, env=timeout_env, check=True,
                           capture_output=True, text=True, timeout=8)
            self.assertEqual(list(Path(directory).glob('observer_*.txt')), [])

    def test_resource_records_must_match_application_pid_and_still_reject_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observer = root / 'rank0_pid123.jsonl'
            rows = [{'event': 'start', 'rank': 0},
                    {'event': 'acquire', 'kind': 'jetty', 'rc': 0, 'mode': 1,
                     'ptr': 'p', 'ctx': 'c', 'depth': 64},
                    {'event': 'release', 'kind': 'jetty', 'rc': 0, 'ptr': 'p'}]
            observer.write_text('\n'.join(map(json.dumps, rows)))
            maps = '\n'.join('0-1 rw /candidate/lib/' + lib + '.0' for lib in
                             ('libucp.so', 'libuct.so', 'libucs.so', 'libucm.so', 'libuct_ub.so'))
            for phase in ('after_init', 'after_first', 'after_steady'):
                (root / ('rank0_%s.maps' % phase)).write_text(maps)
            self.assertFalse(observer.with_suffix('.maps').exists())
            result = support.resource_counts(root, [0], 1, '/candidate', 64, {0: 123})
            self.assertEqual(result['0']['application_pid'], 123)
            self.assertEqual(result['0']['maps'], ['rank0_after_init.maps',
                                                  'rank0_after_first.maps', 'rank0_after_steady.maps'])
            (root / 'rank0_after_steady.maps').unlink()
            with self.assertRaises(FileNotFoundError):
                support.resource_counts(root, [0], 1, '/candidate', 64, {0: 123})
            (root / 'rank0_after_steady.maps').write_text(maps.replace('/candidate/', '/wrong/'))
            with self.assertRaisesRegex(ValueError, 'mixed'):
                support.resource_counts(root, [0], 1, '/candidate', 64, {0: 123})
            (root / 'rank0_after_steady.maps').write_text(maps)
            with self.assertRaisesRegex(ValueError, 'PID'):
                support.resource_counts(root, [0], 1, '/candidate', 64, {0: 456})
            observer.write_text('\n'.join(map(json.dumps, rows[:-1])))
            with self.assertRaisesRegex(ValueError, 'leak'):
                support.resource_counts(root, [0], 1, '/candidate', 64, {0: 123})
