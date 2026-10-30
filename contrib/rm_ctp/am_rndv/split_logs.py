"""Keep strict OSU stdout separate from per-application UCX diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

MAX_LOG = 128 * 1024 * 1024
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


def collect(out, row, evidence, base):
    """Fetch only known peer application logs; wrapper/info processes excluded."""
    run = Path(out) / row['name']
    identities = log_identities(out, row, evidence)
    names = [item['name'] for item in identities if item['rank'] >= row['ppn']]
    code = ('import hashlib,json,pathlib,sys\n'
            'root=pathlib.Path(sys.argv[1]);names=json.loads(sys.argv[2]);result={}\n'
            'for name in names:\n'
            ' p=root/name\n'
            ' if p.is_symlink() or not p.is_file() or p.stat().st_size>134217728: '
            'raise ValueError("missing/nonregular/oversized UCX log: "+name)\n'
            ' data=p.read_bytes();result[name]={"size":len(data),"sha256":hashlib.sha256(data).hexdigest()}\n'
            'print(json.dumps(result))\n')
    remote = json.loads(base.ssh('python3 -c "$1" "$2" "$3"\n', code, run,
                                 json.dumps(names), timeout=60))
    if set(remote) != set(names):
        raise ValueError('peer UCX log manifest coverage mismatch')
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
    combined = []
    for item in identities:
        path = run / item['name']
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOG:
            raise ValueError('missing/nonregular/oversized actual application UCX log')
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if item['rank'] >= row['ppn']:
            if remote[item['name']] != {'size': len(data), 'sha256': digest}:
                raise ValueError('peer UCX log transfer hash mismatch')
        text = data.decode(errors='replace')
        if ERROR.search(text):
            raise ValueError('runtime error in actual application log: ' + item['name'])
        # Each process keeps its own request-pointer order; cross-rank order is
        # unnecessary for the existing request-level path checker.
        combined.append(text)
        receipt.append({**item, 'size': len(data), 'sha256': digest})
    return '\n'.join(combined), receipt
