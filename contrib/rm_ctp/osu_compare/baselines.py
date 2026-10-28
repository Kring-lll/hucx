"""Revalidate the two observed OSU batches; never relabel or rerun old data."""
import copy
import hashlib
import json
from pathlib import Path

import analysis
import run as runner
from support import PRODUCT_COMMIT, PRODUCT_MODULE, budget, digest

SOURCE_MANIFEST = '1a13e94a14a382546467b3e29a2c6c7911cca2c4308d0371f0b8159dafbf955e'
SOURCE_BATCHES = {'osu_compare_87i9g4lw': [1], 'osu_compare_697v3h8j': [8, 16, 20]}
CORE_FILES = ('run.py', 'identity.c', 'analysis.py', 'osu_rank.py')


def normalized(value):
    return json.loads(json.dumps(value))


def source_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_batch(path, case, current_manifest):
    path = Path(path).resolve()
    if path.name != 'results.json' or path.parent.parent != case or path.parent.name not in SOURCE_BATCHES:
        raise ValueError('only the two observed OSU batches may be merged')
    report = json.loads(path.read_text())
    expected_ppn = SOURCE_BATCHES[path.parent.name]
    plan = report['plan']
    if (report['status'] != 'REQUESTED_OSU_MATRIX_PASS_REVIEW_ALL_SAMPLES' or
            plan['product_commit'] != PRODUCT_COMMIT or plan['module_sha256'] != PRODUCT_MODULE or
            plan['ppn'] != expected_ppn or plan['repeats'] != 5 or plan['iterations'] != 5000 or
            plan['warmup'] != 1000 or plan['message_bytes'] != 8 or
            plan['pml'] != runner.PML or plan['coll_components'] != runner.COLL or
            set(plan['benchmarks']) != set(analysis.BENCHMARKS) or
            source_digest(report['manifest']) != SOURCE_MANIFEST):
        raise ValueError('baseline status/product/OSU parameters/toolkit identity mismatch')
    for name in CORE_FILES:
        if report['manifest']['files'][name] != current_manifest['files'][name]:
            raise ValueError('measured OSU core changed: ' + name)
    if report['manifest']['a89_dependencies'] != current_manifest['a89_dependencies']:
        raise ValueError('measured helper dependencies changed')
    expected = {(b, p, m, phase, repeat) for b in analysis.BENCHMARKS for p in expected_ppn
                for m in ('rc', 'rm') for phase, count in (('validation', 1), ('timing', 5), ('resources', 1))
                for repeat in range(count)}
    seen = set()
    hashes = {}
    for row in report['trials']:
        key = (row['benchmark'], row['ppn'], row['mode'], row['phase'], row['repeat'])
        if key not in expected or key in seen or row['passed'] is not True or row['exit'] != 0:
            raise ValueError('baseline trial coverage/exit/status mismatch')
        seen.add(key)
        run = path.parent / row['name']
        config = row['configuration']
        if Path(config['run']).resolve() != run or row['budget'] != budget(row['ppn']):
            raise ValueError('baseline run directory/budget mismatch')
        raw = run / 'trial.json'
        if json.loads(raw.read_text()) != row or json.loads((run / 'rank_config.json').read_text()) != config:
            raise ValueError('baseline raw trial/config changed')
        if row.get('cleanup_error') or set(row['cleanup']) != {'local', 'peer'} or any(
                r['terminated_own_pids'] for r in row['cleanup'].values()):
            raise ValueError('baseline processes did not exit cleanly')
        validation = row['phase'] == 'validation'
        iterations, warmup = (1000, 100) if validation else (5000, 1000)
        if (config['arguments'] != runner.arguments(row['benchmark'], iterations, warmup, validation) or
                config['resource_probe'] != (row['phase'] == 'resources')):
            raise ValueError('baseline timing/validation/resource parameters changed')
        output = analysis.parse_output((run / 'launcher.log').read_text(errors='replace'),
                                      row['benchmark'], validation, iterations)
        rank_evidence = analysis.inspect_ranks(run, row['ppn'], report['preflight']['inventory'],
                            case / 'install', config['nonce'],
                            report['preflight']['benchmarks'][row['benchmark']]['mpi_library'])
        if normalized(rank_evidence) != row['rank_evidence'] or output != row['output']:
            raise ValueError('baseline metrics differ from raw rank/OSU output')
        if row['phase'] == 'resources':
            jettys = analysis.inspect_jettys(run, row['ppn'], 1 if row['mode'] == 'rm' else 2,
                                             rank_evidence['pids'], row['budget']['tx_depth'])
            if normalized(jettys) != row['jettys']:
                raise ValueError('baseline observer accounting changed')
        elif list(run.glob('rank*_pid*.jsonl')):
            raise ValueError('baseline timing/validation unexpectedly observed')
        hashes[str(raw)] = digest(raw)
        hashes[str(run / 'launcher.log')] = digest(run / 'launcher.log')
    if seen != expected or len(report['comparisons']) != len(expected_ppn) * 2:
        raise ValueError('baseline trial/summary coverage incomplete')
    recomputed = [analysis.compare(report['trials'], b, p, 5) for p in expected_ppn for b in analysis.BENCHMARKS]
    if recomputed != report['comparisons']:
        raise ValueError('baseline comparison differs from complete samples')
    return {'report': report, 'source': {'path': str(path), 'sha256': digest(path),
             'started': report['started'], 'raw_trial_log_sha256': hashes}}


def load_baselines(case, manifest):
    case = Path(case).resolve()
    batches = [load_batch(case / name / 'results.json', case, manifest) for name in SOURCE_BATCHES]
    first = batches[0]['report']['preflight']
    for batch in batches[1:]:
        assert_same_runtime(first, batch['report']['preflight'])
    rows = [copy.deepcopy(row) for batch in batches for row in batch['report']['comparisons']]
    for batch in batches:
        for row in rows:
            if row['ppn'] in SOURCE_BATCHES[Path(batch['source']['path']).parent.name]:
                row['source_report'] = batch['source']['path']
    return {'comparisons': rows, 'sources': [b['source'] for b in batches], 'preflight': first}


def assert_same_runtime(old, new):
    if old['candidate'] != new['candidate']:
        # ldd banners may contain load addresses; identity is path/hash, not text.
        for key in ('install', 'module_sha256', 'base_libraries_sha256'):
            if old['candidate'][key] != new['candidate'][key]:
                raise ValueError('candidate library identity changed')
    if len(old['inventory']) != 2 or len(new['inventory']) != 2:
        raise ValueError('node inventory coverage incomplete')
    for before, after in zip(old['inventory'], new['inventory']):
        if any(before[k] != after[k] for k in ('hostname', 'boot_id', 'allowed_cpus')):
            raise ValueError('node boot/CPU permission changed; fresh baseline required')
    if old['observer'] != new['observer'] or old['saved_launcher_sha256'] != new['saved_launcher_sha256']:
        raise ValueError('observer or saved MPI launch specification changed')
    for name in analysis.BENCHMARKS:
        for key in ('path', 'sha256', 'mpi_library', 'mpi_library_sha256'):
            if old['benchmarks'][name][key] != new['benchmarks'][name][key]:
                raise ValueError('OSU/HMPI dependency identity changed')
    if any(old[key] != new[key] for key in ('coll_components', 'pml', 'coll_tuned_dynamic_rules')):
        raise ValueError('collective/PML settings changed')
