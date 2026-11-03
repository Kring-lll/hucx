#!/usr/bin/env python3
"""Keep proven rank execution/cleanup; optionally supervise a bounded sampler."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('profile_original_rank',ROOT/'osu_original_rank.py')
original=importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)


def main():
    if '--cleanup' in sys.argv:
        original.main()
        return
    config=json.loads(Path(sys.argv[1]).read_text())
    rank=int(os.environ['OMPI_COMM_WORLD_RANK'])
    size=int(os.environ['OMPI_COMM_WORLD_SIZE'])
    sampling=config['environment'].get('HUCX_SEND_PROFILE_KIND')=='sample' and rank in (0,size//2)
    sampler=None
    if sampling:
        env={k:v for k,v in os.environ.items() if not k.startswith(('UCX_','MPI_UCX_'))
             and k not in ('LD_PRELOAD','LD_DEBUG')}
        env.update(A89_RUN_NONCE=config['nonce'],HUCX_OSU_PROBE_DIR=config['run'])
        sampler=subprocess.Popen([sys.executable,'-B',str(ROOT/'perf_agent.py'),sys.argv[1],
                                  '--rank',str(rank)],env=env)
    # Wait for sampler before mpirun rank exit so standard cleanup cannot race it.
    try:
        code=subprocess.call([sys.executable,'-B',str(ROOT/'osu_original_rank.py'),sys.argv[1]])
    finally:
        if sampler is not None:
            try:
                sampler.wait(timeout=config['timeout']+15)
            except subprocess.TimeoutExpired:
                sampler.terminate()
                try:
                    sampler.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    sampler.kill()
                    sampler.wait(timeout=5)
    raise SystemExit(code)


if __name__=='__main__':
    main()
