"""Identify only this diagnostic's OSU children; no global process killing."""
import json
import os
from pathlib import Path
import re
import socket
import sys


def start_ticks(pid, proc=Path('/proc')):
    # Same /proc parsing as the already tested A89 rank wrapper.
    return (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]


def matches(pid, config, rank, proc=Path('/proc')):
    environment = (proc / str(pid) / 'environ').read_bytes().split(b'\0')
    required = ['A89_RUN_NONCE=' + config['nonce'],
                'HUCX_OSU_PROBE_DIR=' + config['run'],
                'OMPI_COMM_WORLD_RANK=' + str(rank)]
    return all(value.encode() in environment for value in required)


def find_app(config, rank, proc=Path('/proc')):
    """Follow the recorded timeout's direct child, never search unrelated jobs."""
    if not re.fullmatch('[0-9a-f]{32}', config['nonce']):
        raise ValueError('invalid diagnostic nonce')
    wrapper = json.loads((Path(config['run']) / ('pid_rank%d.json' % rank)).read_text())
    pid = int(wrapper['pid'])
    if (wrapper['rank'] != rank or wrapper['nonce'] != config['nonce'] or
            start_ticks(pid, proc) != str(wrapper['start_ticks']) or
            not matches(pid, config, rank, proc)):
        raise ValueError('wrapper identity mismatch')
    children = (proc / str(pid) / 'task' / str(pid) / 'children').read_text().split()
    for value in children:
        child = int(value)
        before = start_ticks(child, proc)
        binary = (proc / value / 'exe').resolve()
        if binary != Path(config['binary']).resolve() or not matches(child, config, rank, proc):
            continue
        if start_ticks(child, proc) != before:
            continue
        status = (proc / value / 'status').read_text()
        state = next((s.split(':', 1)[1].strip() for s in status.splitlines()
                      if s.startswith('State:')), '?')
        threads = next((s.split(':', 1)[1].strip() for s in status.splitlines()
                        if s.startswith('Threads:')), '?')
        return {'rank': rank, 'pid': child, 'start_ticks': before,
                'binary': str(binary), 'state': state, 'threads': threads}
    raise ProcessLookupError('no matching OSU child for rank %d' % rank)


def snapshot(config, proc=Path('/proc')):
    root = Path(config['run'])
    rows = []
    for path in root.glob('pid_rank*.json'):
        wrapper = json.loads(path.read_text())
        rank = wrapper['rank']
        item = {'rank': rank}
        try:
            item.update(find_app(config, rank, proc))
            if proc == Path('/proc'):
                item['actual_main_affinity'] = sorted(os.sched_getaffinity(item['pid']))
        except (OSError, ValueError, KeyError) as exc:
            item['unavailable'] = str(exc)
        rows.append(item)
    return {'hostname': socket.gethostname(), 'started': len(rows),
            'after_init': len(list(root.glob('rank*_after_init.maps'))),
            'before_finalize': len(list(root.glob('rank*_before_finalize.maps'))),
            'processes': rows}


if __name__ == '__main__':
    print(json.dumps(snapshot(json.loads(Path(sys.argv[1]).read_text()))))
