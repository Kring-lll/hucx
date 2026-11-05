"""Read-only binding to the accepted original ARM build and reviewed checkpoint."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

PRIOR_SHA = 'd6fc0dba364e1befa36d428fd0773dedc3e4b992eb618b670e033d5a8178e515'
PRIOR_NAME = 'um_send_accept_ftoq07pj'
COMPARE_NAME = 'um_osu_three_936frmcy'
COMPARE_SHA = 'dd6693098648d59db8ab4ac548be8efaddad84b4fbbbbce80c8dc087e1767a17'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checkpoint(case, commit, module_sha):
    path = case/PRIOR_NAME/'results.json'
    if digest(path) != PRIOR_SHA:
        raise ValueError('accepted results hash changed: '+str(path))
    r = json.loads(path.read_text())
    if (r['source_commit'] != commit or r['module_sha256'] != module_sha
            or r['status'] != 'PRIOR_13_AND_REMAINING_6_PASS_FULL_ACCEPTANCE_PENDING'
            or r['coverage'] != {'prior_reviewed':13, 'new_passed':6, 'selected_total':19}
            or len(r['cases']) != 6 or len(r['timing']) != 10):
        raise ValueError('accepted checkpoint identity/coverage differs')
    for group in ('cases', 'timing'):
        for row in r[group]:
            trial = (path.parent/row['name']/'trial.json').resolve()
            if trial.parent.parent != path.parent.resolve():
                raise ValueError('checkpoint trial escapes original directory')
            disk = json.loads(trial.read_text()); expected = dict(row)
            if group == 'timing':
                profile = expected.pop('profile')
                if profile not in ('rc','rm') or ('profile' in disk and disk.pop('profile') != profile):
                    raise ValueError('checkpoint timing profile differs')
            if disk != expected or not row['passed'] or row['exit'] != 0 or row.get('cleanup_error'):
                raise ValueError('checkpoint trial changed or failed: '+row['name'])
    return {'source_result':str(path), 'sha256':PRIOR_SHA, 'prior_reviewed':13,
            'new_functional_passed':6, 'timing_passed':10,
            'reused_as_new_comparison_samples':False}


def load():
    bundle = Path(__file__).parent
    if not bundle.is_file():
        raise ValueError('node execution uses the self-contained ZIP, not a repository checkout')
    import pinned_guard as guard
    with zipfile.ZipFile(bundle) as z:
        metadata = json.loads(z.read('metadata.json'))
        payload = z.read('pinned_runner.py')
    guard.FIXED_SHA = metadata['runner_sha256']
    engine = guard.load_controller(guard.CASE, payload)
    prior = checkpoint(guard.CASE, guard.COMMIT, guard.MODULE_SHA)
    prior['three_profile_comparison'] = comparison_checkpoint(guard.CASE,guard.COMMIT,guard.MODULE_SHA)
    return guard, engine, prior, metadata


def comparison_checkpoint(case,commit,module_sha):
    import policy
    path=case/COMPARE_NAME/'results.json'
    if digest(path)!=COMPARE_SHA:raise ValueError('reviewed three-profile comparison changed')
    r=json.loads(path.read_text())
    if (r['source_commit']!=commit or r['module_sha256']!=module_sha
            or r['status']!='SELECTED_THREE_PROFILE_OSU_COMPARISON_COMPLETE_REVIEW_REQUIRED'
            or len(r['gates'])!=3 or len(r['trials'])!=126
            or [v['profile'] for v in r['gates']]!=list(policy.PROFILES)):
        raise ValueError('reviewed three-profile coverage/identity changed')
    for row in r['gates']+r['trials']:
        p=(path.parent/row['name']/'trial.json').resolve()
        if p.parent.parent!=path.parent.resolve() or json.loads(p.read_text())!=row:
            raise ValueError('reviewed trial changed: '+row['name'])
        policy.inspect_trial(row,row['profile'],row['phase'])
    if [policy.summarize(r['trials'],i) for i in range(6)]!=r['comparisons']:
        raise ValueError('reviewed three-profile statistics changed')
    return {'source_result':str(path),'sha256':COMPARE_SHA,'reviewed_mpi':129,
            'old_groups':r['comparisons'],'merged_with_new_samples':False}


def verify_zip(bundle):
    with zipfile.ZipFile(bundle) as z:
        entries = z.namelist()
        manifest = json.loads(z.read('files.sha256.json'))
        if len(set(entries)) != len(entries) or set(entries) != set(manifest) | {'files.sha256.json'}:
            raise ValueError('tool ZIP coverage/duplicate mismatch')
        for name, expected in manifest.items():
            if '/' in name or '\\' in name or hashlib.sha256(z.read(name)).hexdigest() != expected:
                raise ValueError('tool ZIP entry changed: '+name)


def entry():
    sys.dont_write_bytecode = True
    verify_zip(Path(__file__).parent)
    from run_compare import main
    main()
