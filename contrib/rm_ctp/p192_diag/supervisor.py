"""Bounded local GDB samples of guarded OSU processes, followed by own-job exit."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from proc_guard import find_app, snapshot

RANKS = (0, 96, 189)
CAPTURE_AT = (90, 150)


class DiagnosticStop(RuntimeError):
    pass


def stack_label(text):
    mpi = re.findall(r'\bP?MPI_(?:Barrier|Comm_split|Comm_dup|Send|Recv|Reduce|Bcast|Allreduce|Init_thread|Init|Finalize)\b', text)
    if mpi:
        return ','.join(dict.fromkeys(mpi))
    functions = re.findall(r'\b(?:ucp_worker_progress|mca_pml_ucx_[A-Za-z_]+|ompi_coll_[A-Za-z_]+)\b', text)
    return ','.join(dict.fromkeys(functions)) or 'UNRESOLVED_READ_RAW_STACK'


def capture_one(config, rank, directory, round_number, proc=Path('/proc'), execute=subprocess.run):
    path = directory / ('round%d_rank%d.gdb.log' % (round_number, rank))
    row = {'rank': rank, 'round': round_number, 'passed': False, 'log': str(path)}
    try:
        app = find_app(config, rank, proc)
        # Check again immediately before attaching, including start time + nonce.
        current = find_app(config, rank, proc)
        if any(current[k] != app[k] for k in ('pid','start_ticks','binary','rank')):
            raise ProcessLookupError('target changed before GDB attach')
        row.update(app)
        command = ['/usr/bin/gdb', '--nx', '--nh', '-q', '-batch', '-p', str(app['pid']),
                   '-ex', 'set pagination off', '-ex', 'set print frame-arguments none',
                   '-ex', 'thread apply all bt 24', '-ex', 'detach', '-ex', 'quit']
        row['command'] = command
        with path.open('w') as output:
            result = execute(command, stdout=output, stderr=subprocess.STDOUT, timeout=12)
        row['exit'] = result.returncode
        text = path.read_text(errors='replace')
        row['wait_frames'] = stack_label(text)
        row['passed'] = result.returncode == 0 and re.search(r'^#0\s', text, re.M) is not None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        # A timed-out debugger must not leave our own target stopped.
        if row.get('pid') and proc == Path('/proc'):
            try:
                current = find_app(config, rank, proc)
                if (current['pid'], current['start_ticks']) == (row['pid'], row['start_ticks']):
                    os.kill(row['pid'], signal.SIGCONT)
            except (OSError, ValueError, KeyError):
                pass
    return row


class Supervisor:
    def __init__(self, runner, out, report):
        self.runner = runner
        self.out = out
        self.report = report
        self.original = runner.base.run_logged

    def save(self):
        self.runner.base.write_json(self.out / 'diagnostic.json', self.report)

    def sample(self, log, number):
        config_path = log.parent / 'rank_config.json'
        config = json.loads(config_path.read_text())
        target = self.out / ('capture%d' % number)
        target.mkdir()
        local = snapshot(config)
        self.runner.base.write_json(target / 'node89_processes.json', local)
        try:
            text = self.runner.base.ssh('python3 "$1" "$2"\n', self.out / 'proc_guard.py', config_path, timeout=25)
            peer = json.loads(text)
            self.runner.base.write_json(target / 'node97_processes.json', peer)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.report.setdefault('snapshot_errors', []).append(str(exc))
        for rank in RANKS:
            row = capture_one(config, rank, target, number)
            self.report['stacks'].append(row)
            self.save()
            print('STACK ROUND=%d RANK=%d CAPTURE=%s WAIT=%s' %
                  (number, rank, 'PASS' if row['passed'] else 'FAILED',
                   row.get('wait_frames', row.get('error', '?'))[:200]), flush=True)

    def __call__(self, command, environment, log, timeout):
        if log.name != 'launcher.log':
            return self.original(command, environment, log, timeout)
        self.runner.base.write_json(log.with_suffix('.command.json'),
                    {'command': command, 'environment': {k:v for k,v in environment.items()
                      if k.startswith(('UCX_','LD_','OMPI_'))},
                     'timeout_seconds': timeout, 'scope': 'DIAGNOSTIC_NOT_PERFORMANCE'})
        started = time.monotonic()
        with log.open('w') as output:
            process = subprocess.Popen(command, env=environment, stdout=output,
                        stderr=subprocess.STDOUT, start_new_session=True)
            try:
                print('SINGLE_PPN192_STARTED=YES CAPTURE_AT_SECONDS=[90,150]', flush=True)
                for number, deadline in enumerate(CAPTURE_AT, 1):
                    while process.poll() is None and time.monotonic() - started < deadline:
                        time.sleep(.5)
                    if process.poll() is not None:
                        break
                    self.sample(log, number)
                if process.poll() is None:
                    self.report['intentional_early_stop'] = True
                    self.save()
                    raise DiagnosticStop('two stack sampling rounds finished; stop own launcher, not a validation failure')
                if process.returncode != 0:
                    raise RuntimeError('EXIT=%d LOG=%s' % (process.returncode, log))
                return {'exit': 0, 'launcher_wall_s': time.monotonic() - started}
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                self.report['supervisor_wall_s'] = time.monotonic() - started
                self.save()
