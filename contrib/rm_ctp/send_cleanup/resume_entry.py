#!/usr/bin/env python3
"""Repair the verified a68 controller in memory; preserve source/install bytes."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

CASE = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-c-send-reliability-dev/r1_shared_a68a7ab31_DBw3Ll')
COMMIT = 'a68a7ab3119e2ac545a1bf4a78e537d958b23f50'
MODULE = 'cea0c23ef9f2331ddaa5a270cacd425a84fc4ccf2b16a3c556b5b08e2146c765'
ENGINE = '5acb9f8c59ef45e65524b69bb870fda3b7d2f882c56e16caa93f61ba7af75367'
MANIFEST = '3caf6b69a869b3272600710867d1f1b29484dbf93b99bcb7af09abb21bcd5d02'
OLD = "    if ROOT!=case/'source/contrib/rm_ctp/send_pool':"
NEW = "    if (ROOT.name not in ('send_pool', 'send_cleanup') or\n            ROOT != case/'source/contrib/rm_ctp'/ROOT.name):"

def checked_source(case):
    build = json.loads((case/'build_manifest.json').read_text())
    if (Path(build['case_dir']).resolve() != case.resolve() or
            build['source_commit'] != COMMIT or build['module_sha256'] != MODULE):
        raise ValueError('resume CASE/build identity differs')
    module = case/'install/lib/ucx/libuct_ub.so.0.0.0'
    if hashlib.sha256(module.read_bytes()).hexdigest() != MODULE:
        raise ValueError('accepted module changed')
    source = case/'source'
    manifest_data = (source/'contrib/rm_ctp/r1/candidate_manifest.json').read_bytes()
    if hashlib.sha256(manifest_data).hexdigest() != MANIFEST:
        raise ValueError('original source manifest changed')
    manifest = json.loads(manifest_data)
    for name, expected in manifest['files'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest() != expected:
            raise ValueError('reviewed source changed: '+name)
    engine = source/'contrib/rm_ctp/send_pool/run_candidate.py'
    data = engine.read_bytes()
    if hashlib.sha256(data).hexdigest() != ENGINE:
        raise ValueError('original controller changed')
    text = data.decode('utf-8')
    if text.count(OLD) != 1:
        raise ValueError('controller patch is not unique')
    return text.replace(OLD, NEW, 1)

def run(case, tool_commit='local-entry-fix'):
    patched = checked_source(case)
    entry = case/'source/contrib/rm_ctp/send_cleanup/run_candidate.py'
    spec = importlib.util.spec_from_file_location('cleanup_verified_entry', entry)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    engine = wrapper.engine
    # __file__ remains the canonical source path. No source file is overwritten.
    exec(compile(patched, str(entry.parent/'controller_memory_fix.py'), 'exec'), engine.__dict__)
    wrapper.configure()
    original_plan = engine.plan
    def plan():
        value = original_plan()
        value['controller_entry_fix'] = dict(tool_commit=tool_commit,
            original_engine_sha256=ENGINE, memory_only=True, product_rebuild=False)
        return value
    engine.plan = plan
    sys.argv = [str(entry), str(case), '--run']
    print('SOURCE_MODULE_AND_CONTROLLER=PASS MEMORY_ONLY_ENTRY_FIX=YES PRODUCT_REBUILD=NO', flush=True)
    engine.main()

def main(tool_commit='local-entry-fix'):
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(dict(default_action='PLAN_ONLY', case=str(CASE),
            product_commit=COMMIT, product_rebuild=False,
            normal_tasks=28, focused_mpi_tasks=14, bounded_uct_tasks=6,
            timing=False), indent=2))
        return
    run(CASE, tool_commit)

if __name__ == '__main__':
    main()
