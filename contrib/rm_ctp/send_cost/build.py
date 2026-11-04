"""Build a separate diagnostic installation; never touch accepted source/install."""
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile

import instrument


def validate_accepted():
    case = instrument.ACCEPTED
    build = json.loads((case/'build_manifest.json').read_text())
    if (build['source_commit'] != instrument.BASE_COMMIT or
            build['module_sha256'] != instrument.BASE_MODULE or
            Path(build['case_dir']).resolve() != case.resolve() or
            instrument.digest(case/'install/lib/ucx/libuct_ub.so.0.0.0') != instrument.BASE_MODULE):
        raise ValueError('accepted inline candidate changed')
    identity = json.loads((instrument.ROOT.parent/'send_inline/product_identity.json').read_text())
    if len(identity['files']) != 15:
        raise ValueError('accepted product source coverage differs')
    for name, expected in identity['files'].items():
        if instrument.digest(case/'source'/name) != expected:
            raise ValueError('accepted product source changed: '+name)
    checkpoint = case/'send_inline_accept_9gpdcz12/results.json'
    if instrument.digest(checkpoint) != '0c9d72fc108e25e9afc306256dc402d58dce9e70b41d4e27dab2eede25986d80':
        raise ValueError('accepted inline checkpoint changed')
    if json.loads(checkpoint.read_text())['status'] != 'SELECTED_INLINE_FUNCTIONAL_AND_REQUESTED_OSU_PASS_REVIEW_REQUIRED':
        raise ValueError('accepted inline checkpoint status differs')
    return build


def archive_path():
    return instrument.ACCEPTED.parent/'upload/hucx-c-send-inline-20261103-source.tar.gz'


def extract(archive, source):
    if instrument.digest(archive) != instrument.ARCHIVE_SHA:
        raise ValueError('accepted original source archive hash differs')
    with tarfile.open(archive, 'r:gz') as stream:
        if stream.pax_headers.get('comment', '').strip() != instrument.BASE_COMMIT:
            raise ValueError('accepted source archive commit differs')
        members = stream.getmembers()
        names = set()
        for item in members:
            p = PurePosixPath(item.name)
            if (p.is_absolute() or '..' in p.parts or '\\' in item.name or
                    not (item.isfile() or item.isdir()) or item.name in names):
                raise ValueError('unsafe original source archive member')
            names.add(item.name)
        stream.extractall(source)


def build():
    validate_accepted()
    work = Path(tempfile.mkdtemp(prefix='send_cost_build_', dir=instrument.ACCEPTED.parent))
    print('DIAGNOSTIC_BUILD_DIR='+str(work), flush=True)
    for name in ('source', 'build', 'install'):
        (work/name).mkdir()
    extract(archive_path(), work/'source')
    changed = instrument.apply(work/'source')
    commands = [(['bash', './autogen.sh'], work/'source'),
                ([str(work/'source/configure'), '--prefix='+str(work/'install'), '--with-ub=/usr',
                  '--disable-profiling', '--disable-java', '--without-cuda', '--without-rocm',
                  '--without-verbs', '--without-rdmacm', 'CC=gcc', 'CXX=g++',
                  'CFLAGS=-O2 -g', 'CXXFLAGS=-O2 -g'], work/'build'),
                (['make', '-j8'], work/'build'), (['make', 'install'], work/'build')]
    # Selected ALWAYS profiling macros work with the ordinary profiling sites
    # disabled. Do not enable thousands of unrelated UCX scopes.
    env = {k:v for k,v in os.environ.items() if not k.startswith(('UCX_', 'MPI_UCX_', 'HUCX_'))
           and k not in ('LD_PRELOAD', 'LD_DEBUG')}
    with (work/'build.log').open('x') as log:
        for command, directory in commands:
            subprocess.run(command, cwd=directory, env=env, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
    module = work/'install/lib/ucx/libuct_ub.so.0.0.0'
    result = dict(case_dir=str(work), source_commit=instrument.BASE_COMMIT,
                  module_sha256=instrument.digest(module), archive_sha256=instrument.ARCHIVE_SHA,
                  diagnostic_only=True, product_accepted=False, instrumentation_files=changed,
                  note='derived source with diagnostics; not the accepted a2b binary')
    (work/'build_manifest.json').write_text(json.dumps(result, indent=2)+'\n')
    subprocess.run(['tar', '-czf', str(work/'install.tar.gz'), '-C', str(work), 'install'], check=True)
    print('DIAGNOSTIC_MODULE_SHA256='+result['module_sha256'], flush=True)
    return work, result
