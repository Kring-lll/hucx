"""Keep strict OSU stdout separate from per-application UCX diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

MAX_LOG = 256 * 1024 * 1024
ERROR = re.compile(r'UCX\s+(ERROR|FATAL)|Assertion.*failed|Caught signal|'
                   r'DATA VALIDATION ERROR|MPI_ERR_|OSU_PROOF_ERROR', re.I)


def trial_directory(out, case, index):
    name = '%s_%s_p%d_validation_%d' % (case['benchmark'], case['mode'], case['ppn'], index)
    if case['sizes'][0] != 8:
        name += '_b%d' % case['sizes'][0]
    return Path(out) / name


def log_pattern(directory):
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError('UCX log directory must be absolute')
    return str(directory / 'ucx_%h_%p.log')


def log_identities(out, row, evidence):
    run = Path(out) / row['name']
    env = row['configuration']['environment']
    if env.get('UCX_LOG_FILE') != log_pattern(run):
        raise ValueError('UCX log pattern does not belong to this trial')
    result = []
    for rank in range(2 * row['ppn']):
        host = evidence['inventory'][rank // row['ppn']]['hostname']
        pid = row['rank_evidence']['pids'][rank]
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', host) or type(pid) is not int or pid <= 0:
            raise ValueError('invalid actual application identity')
        result.append({'rank': rank, 'host': host, 'pid': pid,
                       'name': 'ucx_%s_%d.log' % (host, pid)})
    if len({item['name'] for item in result}) != len(result):
        raise ValueError('duplicate actual application log identity')
    return result


def collect(out, row, evidence, base, parse_lines=None, reuse_existing=False):
    """Fetch only known peer application logs; wrapper/info processes excluded."""
    run = Path(out) / row['name']
    identities = log_identities(out, row, evidence)
    names = [item['name'] for item in identities if item['rank'] >= row['ppn']]
    code = ('import hashlib,json,pathlib,sys\n'
            'root=pathlib.Path(sys.argv[1]);names=json.loads(sys.argv[2]);result={}\n'
            'for name in names:\n'
            ' p=root/name\n'
            f' if p.is_symlink() or not p.is_file() or p.stat().st_size>{MAX_LOG}: '
            'raise ValueError("missing/nonregular/oversized UCX log: "+name)\n'
            ' h=hashlib.sha256();size=0\n'
            ' with p.open("rb") as f:\n'
            '  for block in iter(lambda:f.read(1048576),b""):\n'
            '   size+=len(block);h.update(block)\n'
            f'   if size>{MAX_LOG}: raise ValueError("UCX log grew past limit: "+name)\n'
            ' result[name]={"size":size,"sha256":h.hexdigest()}\n'
            'print(json.dumps(result))\n')
    remote = json.loads(base.ssh('python3 -c "$1" "$2" "$3"\n', code, run,
                                 json.dumps(names), timeout=60))
    if set(remote) != set(names):
        raise ValueError('peer UCX log manifest coverage mismatch')
    if reuse_existing:
        if not all((run / name).is_file() for name in names):
            raise ValueError('peer log copy missing from the selected checkpoint')
    else:
        for name in names:
            if (run / name).exists():
                raise ValueError('peer log destination already exists; preserve it')
        command = ['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8']
        command += [base.PEER + ':' + str(run / name) for name in names]
        command += [str(run) + '/']
        transfer_env = {key: value for key, value in os.environ.items()
                        if not key.startswith('UCX_') and key not in ('LD_PRELOAD', 'LD_DEBUG')}
        base.run_logged(command, transfer_env, run / 'fetch_ucx_logs.log', 120)
    receipt = []

    def lines():
        for item in identities:
            path = run / item['name']
            if path.is_symlink() or not path.is_file():
                raise ValueError('missing/nonregular actual application UCX log: ' + item['name'])
            size = path.stat().st_size
            if size > MAX_LOG:
                raise ValueError('oversized actual application UCX log: %s bytes=%d limit=%d' %
                                 (item['name'], size, MAX_LOG))
            hasher = hashlib.sha256()
            consumed = 0
            with path.open('rb') as stream:
                for raw in stream:
                    consumed += len(raw)
                    if consumed > MAX_LOG:
                        raise ValueError('UCX log grew past limit: ' + item['name'])
                    hasher.update(raw)
                    text = raw.decode(errors='replace')
                    if ERROR.search(text):
                        raise ValueError('runtime error in actual application log: ' + item['name'])
                    yield text
            if consumed != size:
                raise ValueError('UCX log changed during verification: ' + item['name'])
            value = {'size': consumed, 'sha256': hasher.hexdigest()}
            if item['rank'] >= row['ppn'] and remote[item['name']] != value:
                raise ValueError('peer UCX log transfer hash mismatch')
            receipt.append({**item, **value})
            yield '\n'

    iterator = lines()
    if parse_lines is None:
        result = ''.join(iterator)
    else:
        result = parse_lines(iterator)
        # Exhaust even an early-returning parser, so trailing errors/hash checks
        # can never be bypassed by a successful prefix parse.
        for _ in iterator:
            pass
    return result, receipt
