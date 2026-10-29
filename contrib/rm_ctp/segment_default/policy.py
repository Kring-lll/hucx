"""Candidate identity and effective configuration checks; no device operations."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re

B_ROOT = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev')
SEGMENT_KEYS = frozenset(('UCX_RM_URMA_SEG_SIZE', 'UCX_RM_URMA_URMA_SEG_SIZE',
                          'UCX_RC_URMA_SEG_SIZE', 'UCX_UM_URMA_SEG_SIZE',
                          'UCX_URMA_SEG_SIZE', 'UCX_SEG_SIZE'))


def without_segment_overrides(environment):
    return {key: value for key, value in environment.items() if key not in SEGMENT_KEYS}


def validate_build(case, root=B_ROOT):
    case = Path(case).resolve()
    if case.parent != Path(root).resolve():
        raise ValueError('candidate CASE must be a direct child of the B experiment root')
    build = json.loads((case / 'build_manifest.json').read_text())
    if (Path(build['case_dir']).resolve() != case
            or not re.fullmatch(r'[0-9a-f]{40}', build['source_commit'])
            or not re.fullmatch(r'[0-9a-f]{64}', build['module_sha256'])):
        raise ValueError('invalid candidate build identity')
    source = case / 'source'
    manifest = json.loads((source / 'contrib/rm_ctp/r1/candidate_manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError('candidate source differs from reviewed manifest: ' + name)
    module = case / 'install/lib/ucx/libuct_ub.so.0.0.0'
    if hashlib.sha256(module.read_bytes()).hexdigest() != build['module_sha256']:
        raise ValueError('candidate module differs from build receipt')
    code = (source / 'src/uct/urma/rm_urma/rm_urma_iface.c').read_text()
    if not re.search(r'\{"URMA_",\s*"SEG_SIZE=4096"', code):
        raise ValueError('candidate does not contain the RM 4K table default')
    return build


def check_defaults(text, bytes_value):
    values = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
    expected = {'UCX_RM_URMA_SEG_SIZE': 4096,
                'UCX_RC_URMA_SEG_SIZE': 8192,
                'UCX_UM_URMA_SEG_SIZE': 8192}
    for name, size in expected.items():
        if name not in values or bytes_value(values[name]) != size:
            raise ValueError('unexpected effective default: ' + name + '=' + str(values.get(name)))
    return {name: bytes_value(values[name]) for name in expected}


@contextmanager
def bind_candidate(osu, case, build):
    """Bind the existing runner to one newly built candidate, then restore it."""
    names = ('DEFAULT_CASE', 'PRODUCT_COMMIT', 'PRODUCT_MODULE', 'environment')
    original = {name: getattr(osu, name) for name in names}
    original_environment = osu.environment

    def default_environment(*args, **kwargs):
        return without_segment_overrides(original_environment(*args, **kwargs))

    osu.DEFAULT_CASE = Path(case)
    osu.PRODUCT_COMMIT = build['source_commit']
    osu.PRODUCT_MODULE = build['module_sha256']
    osu.environment = default_environment
    try:
        yield
    finally:
        for name, value in original.items():
            setattr(osu, name, value)
