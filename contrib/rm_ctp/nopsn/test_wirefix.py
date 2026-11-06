import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('wirefix',ROOT/'resume_wirefix.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


class RecoveryTests(unittest.TestCase):
    def test_prior_three_passes_and_unlaunched_64b_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            case=Path(temp);root=case/f.PRIOR_NAME;root.mkdir()
            rows=[]
            for index in range(3):
                run=root/str(index);run.mkdir()
                row={'name':str(index),'variant':'full' if index==0 else 'nopsn','passed':True,
                    'exit':0,'benchmark':'osu_latency','ppn':1,'phase':'validation','message_bytes':8,
                    'wire_observed':index<2,'output':{'validation_pass':True},
                    'configuration':{'nonce':'c'*32},'rank_evidence':{'pids':{'0':100,'1':101}}}
                if index<2:
                    row['wire_evidence']=[]
                    for rank,pid in ((0,100),(1,101)):
                        v={'rank':rank,'pid':pid,'nonce':'c'*32,'errors':0,'retry_success':0,
                            'data_success':1,'short_success':1,'header_mask':1<<(8 if index==0 else 6),'max_wr_bytes':24}
                        (run/('nopsn_wire_rank%d_pid%d.json'%(rank,pid))).write_text(json.dumps(v));row['wire_evidence'].append(v)
                (run/'launcher.log').write_text('original OSU log')
                rows.append(row)
            report={'status':'STOPPED_INCOMPLETE','products':{'nopsn':{'source_commit':f.COMMIT,'module_sha256':f.MODULE}},
                'trials':rows,'comparisons':[],'error':'ValueError: unplanned OSU workload',
                'observer_wirefix':{'tool_commit':'fea1fbf8d565dc1ced4ea2b43fb6f895d2de2e08'},
                'active_task':{'index':3,'variant':'nopsn','benchmark':'osu_multi_lat','ppn':1,'phase':'validation','bytes':64,'wire_observed':False}}
            def save():
                (root/'results.json').write_text(json.dumps(report))
                for row in rows:(root/row['name']/'trial.json').write_text(json.dumps(row))
            save();before=(root/'results.json').read_bytes()
            proof=f.review_failure(case)
            self.assertEqual(proof['completed_8b_gates_reviewed'],3)
            self.assertFalse(proof['prior_samples_merged']);self.assertFalse(proof['failed_task_started_mpi'])
            self.assertEqual(before,(root/'results.json').read_bytes())
            for key,value in [('passed',False),('variant','full'),('exit',134),('wire_observed',True)]:
                old=rows[2][key];rows[2][key]=value;save()
                with self.assertRaises(ValueError):f.review_failure(case)
                rows[2][key]=old
            save();report['active_task']['bytes']=65;save()
            with self.assertRaises(ValueError):f.review_failure(case)

    def test_real_controller_guard_and_memory_overlay(self):
        with tempfile.TemporaryDirectory() as temp:
            case=Path(temp)/'case';entry=case/'source'/f.ENTRY;entry.parent.mkdir(parents=True)
            entry.write_text('# original controller\n')
            module=case/'install/lib/ucx/libuct_ub.so.0.0.0';module.parent.mkdir(parents=True);module.write_bytes(b'unchanged ARM placeholder')
            # Actual candidate validator runs with a fixture LAB and ROOT. No MPI.
            payload=(ROOT/'run_candidate.py').read_bytes()
            meta={'files':{'patched_runner.py':hashlib.sha256(payload).hexdigest()},'original_controller_sha256':f.digest(entry)}
            namespace={'__name__':'guard_fixture','__file__':str(entry)}
            exec(compile(payload,str(entry),'exec'),namespace)
            archive=case/'archive';archive.write_bytes(b'archive fixture')
            build={'source_commit':f.COMMIT,'archive':str(archive),'archive_sha256':f.digest(archive),'module_sha256':f.digest(module)}
            (case/'build_manifest.json').write_text(json.dumps(build))
            (case/'source/.hucx_source_commit').write_text(f.COMMIT+'\n')
            mf=case/'source/contrib/rm_ctp/r1/candidate_manifest.json';mf.parent.mkdir()
            mf.write_text(json.dumps({'files':{f.ENTRY:f.digest(entry)}}))
            # Keep the real loader; insert fixture root into its isolated namespace only.
            amended=payload.replace(b"LAB=Path('/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-nopsn-dev')",('LAB=Path('+repr(str(case.parent))+')').encode())
            meta['files']['patched_runner.py']=hashlib.sha256(amended).hexdigest()
            with patch.object(f,'CASE',case),patch.object(f,'MODULE',f.digest(module)),patch.object(f,'ARCHIVE',f.digest(archive)):
                loaded=f.load_controller(case,amended,meta)
                self.assertEqual(loaded['ROOT'],entry.parent)
                self.assertEqual(loaded['plan'](True)['total_mpi'],215)
                self.assertNotIn('LOCAL_VERSIONED',entry.read_text())
                original=module.read_bytes();module.write_bytes(b'changed')
                with self.assertRaises(ValueError):f.load_controller(case,amended,meta)
                module.write_bytes(original)
                entry.write_text('changed')
                with self.assertRaises(ValueError):f.load_controller(case,amended,meta)
                entry.write_text('# original controller\n');archive.write_bytes(b'changed')
                with self.assertRaises(ValueError):f.load_controller(case,amended,meta)

    def test_default_is_plan_without_live_activity(self):
        with patch('sys.argv',['wirefix']),patch.object(f.subprocess,'run') as run,contextlib.redirect_stdout(io.StringIO()) as text:
            f.main()
        run.assert_not_called();self.assertEqual(json.loads(text.getvalue())['new_mpi'],47)


if __name__=='__main__':unittest.main()
