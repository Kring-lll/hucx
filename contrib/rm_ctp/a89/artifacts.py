"""One SSH stream for rank artifacts; no MPI execution or process cleanup.

All members are flat, regular files from the selected peer ranks. Hashes are
checked before publishing a complete batch. Partial downloads stay diagnostic.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import uuid

MAX_FILE = 128 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024

REMOTE = r'''
import hashlib, io, json, pathlib, re, sys, tarfile
run = pathlib.Path(sys.argv[1])
ranks = json.loads(sys.argv[2])
probe = sys.argv[3] == '1'
schema = sys.argv[4] if len(sys.argv) > 4 else 'a89'
phases = ('after_init', 'before_finalize') if schema == 'osu' else ('after_init', 'after_first', 'after_steady')
names = []
for rank in ranks:
    names += ['rank%d.json' % rank, 'pid_rank%d.json' % rank]
    names += ['rank%d_%s.maps' % (rank, phase) for phase in phases]
if probe:
    names += [p.name for p in run.glob('rank*_pid*.jsonl')
              if re.fullmatch(r'rank(\d+)_pid\d+\.jsonl', p.name)
              and int(re.fullmatch(r'rank(\d+)_pid\d+\.jsonl', p.name)[1]) in ranks]
manifest = {}
total = 0
for name in sorted(set(names)):
    path = run / name
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('missing/nonregular artifact: ' + name)
    data = path.read_bytes()
    total += len(data)
    if len(data) > 128*1024*1024 or total > 2*1024*1024*1024:
        raise RuntimeError('artifact size limit')
    manifest[name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
    data = json.dumps(manifest, sort_keys=True).encode()
    header = tarfile.TarInfo('__artifact_manifest.json')
    header.size = len(data)
    archive.addfile(header, io.BytesIO(data))
    for name in sorted(manifest):
        archive.add(str(run / name), arcname=name, recursive=False)
'''


def required_names(ranks, schema='a89'):
    if schema not in ('a89', 'osu'):
        raise ValueError('unknown artifact schema')
    phases = ('after_init', 'before_finalize') if schema == 'osu' else ('after_init', 'after_first', 'after_steady')
    names = set()
    for rank in ranks:
        names.update(('rank%d.json' % rank, 'pid_rank%d.json' % rank))
        names.update('rank%d_%s.maps' % (rank, phase) for phase in phases)
    return names


def unpack_checked(archive_path, run, ranks, probe, schema='a89'):
    """Validate the entire archive before writing any rank artifact."""
    run = Path(run)
    ranks = set(ranks)
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        if not members or members[0].name != '__artifact_manifest.json':
            raise ValueError('artifact manifest missing')
        if any(not member.isfile() or member.size > MAX_FILE or member.size < 0
               for member in members) or sum(member.size for member in members) > MAX_TOTAL:
            raise ValueError('artifact member type/size rejected')
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError('duplicate artifact member')
        manifest = json.load(archive.extractfile(members[0]))
        expected = required_names(ranks, schema)
        observed = set()
        for name in manifest:
            if '/' in name or '\\' in name or name in ('', '.', '..'):
                raise ValueError('artifact path rejected')
            if name not in expected:
                match = re.fullmatch(r'rank(\d+)_pid\d+\.jsonl', name)
                if not probe or match is None or int(match[1]) not in ranks:
                    raise ValueError('unexpected artifact: ' + name)
                observed.add(int(match[1]))
        if not expected <= set(manifest) or set(names[1:]) != set(manifest):
            raise ValueError('artifact coverage/manifest mismatch')
        if probe and observed != ranks:
            raise ValueError('observer artifact rank coverage incomplete')
        with tempfile.TemporaryDirectory(prefix='.artifact_stage_', dir=run) as temporary:
            stage = Path(temporary)
            for member in members[1:]:
                row = manifest[member.name]
                if row['size'] != member.size:
                    raise ValueError('artifact size mismatch')
                target = stage / member.name
                with archive.extractfile(member) as source, target.open('wb') as destination:
                    shutil.copyfileobj(source, destination)
                if hashlib.sha256(target.read_bytes()).hexdigest() != row['sha256']:
                    raise ValueError('artifact checksum mismatch: ' + member.name)
                old = run / member.name
                if old.is_symlink() or (old.exists() and old.read_bytes() != target.read_bytes()):
                    raise ValueError('existing artifact differs: ' + member.name)
            for name in manifest:
                if not (run / name).exists():
                    (stage / name).replace(run / name)
    return manifest


def fetch(run, ranks, probe, peer='root@9.88.43.97', destination=None, schema='a89'):
    """Fetch one immutable post-exit set, retaining archive/log/receipt locally."""
    source = Path(run)
    target = Path(destination) if destination is not None else source
    ranks = list(ranks)
    required_names(ranks, schema)
    receipt_dir = target / 'artifact_fetch'
    receipt_dir.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    partial = receipt_dir / (token + '.partial')
    stderr = receipt_dir / (token + '.stderr.log')
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', peer,
               shlex.join(['python3', '-c', REMOTE, str(source), json.dumps(ranks),
                           '1' if probe else '0', schema])]
    try:
        with partial.open('wb') as stream, stderr.open('wb') as errors:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream,
                                    stderr=errors, timeout=900)
        if result.returncode:
            raise RuntimeError('SSH artifact stream exit=%d stderr=%s' %
                               (result.returncode, stderr))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('artifact SSH stream timeout=900s; partial=%s stderr=%s' %
                           (partial, stderr)) from exc
    archive_path = partial.with_suffix('.tar.gz')
    partial.replace(archive_path)
    manifest = unpack_checked(archive_path, target, ranks, probe, schema)
    receipt = {'method': 'one_ssh_tar_gzip_stream', 'source_run': str(source),
               'peer': peer, 'ranks': ranks, 'schema': schema, 'archive': str(archive_path),
               'archive_sha256': hashlib.sha256(archive_path.read_bytes()).hexdigest(),
               'files': manifest, 'validated': True}
    (receipt_dir / (token + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt
