import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book2epub.refinement import make_batches, make_request, make_global_request, request_bytes
from book2epub.translation import translate_segment
from book2epub.db import connect
from book2epub.pipeline import _refinement_call, structure_refinement, set_progress_hook, reset_stage


class RefinementBatchTests(unittest.TestCase):
    def test_long_neighborhoods_are_bounded_without_losing_headings(self):
        units = []
        for ordinal in range(1, 49):
            kind = 'heading' if ordinal % 3 == 1 else 'paragraph'
            text = f'Chapter {ordinal}' if kind == 'heading' else 'body ' * 12000
            units.append({'ordinal': ordinal, 'kind': kind, 'text': text})
        tree = [{'start_unit': x['ordinal'], 'title': x['text'], 'node_type': 'chapter'}
                for x in units if x['kind'] == 'heading']
        batches = make_batches(units, tree)
        self.assertGreater(len(batches), 1)
        self.assertEqual([h['ordinal'] for batch in batches for h in batch['headings']],
                         [x['ordinal'] for x in units if x['kind'] == 'heading'])
        for batch in batches:
            system, user = make_request(batch)
            self.assertLessEqual(request_bytes(system, user), 40000)
            self.assertNotIn('body body body body body body body body body body body body', system)

    def test_oversized_heading_is_reported_instead_of_truncated(self):
        units = [{'ordinal': 1, 'kind': 'heading', 'text': 'X' * 50000}]
        with self.assertRaisesRegex(ValueError, 'heading.*request limit'):
            make_batches(units, [])

    def test_request_contains_only_batch_nodes(self):
        units = [{'ordinal': n, 'kind': 'heading', 'text': f'Heading {n}'} for n in range(1, 31)]
        tree = [{'start_unit': n, 'title': f'Heading {n}', 'node_type': 'section'} for n in range(1, 31)]
        batches = make_batches(units, tree, max_headings=6)
        self.assertEqual(len(batches), 5)
        first = json.loads(make_request(batches[0])[1])
        self.assertEqual(len(first['headings']), 6)
        self.assertEqual(len(first['existing_nodes']), 6)

    def test_new_chapter_starts_a_new_batch_when_prior_batch_is_substantial(self):
        units = [{'ordinal': n, 'kind': 'heading', 'text': f'Heading {n}'} for n in range(1, 14)]
        tree = [{'start_unit': n, 'title': f'Heading {n}',
                 'node_type': 'chapter' if n in (1, 8) else 'section'} for n in range(1, 14)]
        batches = make_batches(units, tree)
        self.assertEqual(batches[1]['headings'][0]['ordinal'], 8)

    def test_global_request_is_bounded_for_a_long_tree(self):
        nodes = [{'unit': n, 'title': f'Heading {n}',
                  'node_type': 'chapter' if n % 10 == 1 else 'section',
                  'parent_start_unit': n - (n - 1) % 10} for n in range(1, 1001)]
        system, user = make_global_request(nodes, [])
        self.assertLessEqual(request_bytes(system, user), 40000)
        data = json.loads(user)
        self.assertEqual(data['node_count'], 1000)
        self.assertEqual(len(data['chapters']), 100)

    def test_refinement_request_stops_after_three_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'BOOK2EPUB_TRANSLATION_URL': 'https://example.test/responses',
                   'BOOK2EPUB_TRANSLATION_MODEL': 'test-model'}
            with patch.dict(os.environ, env), patch('book2epub.translation._post_json', side_effect=TimeoutError('slow')) as post, patch('book2epub.translation._retry_sleep'):
                with self.assertRaisesRegex(RuntimeError, 'slow'):
                    translate_segment('input', 'eng', 'eng', provider='hosted',
                                      env_file=Path(directory) / '.env', max_attempts=3)
            self.assertEqual(post.call_count, 3)

    def test_validated_refinement_response_is_reused_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            db = connect(project)
            response = {'action': 'propose', 'nodes': [], 'unresolved': []}
            usage = {'model': 'test-model', 'input_tokens': 12, 'output_tokens': 8}
            with patch('book2epub.pipeline.translate_segment', return_value=(json.dumps(response), usage)) as call:
                first, hit1 = _refinement_call(db, 1, project, 'test-model',
                                               'batch:1', 'system', 'user')
                second, hit2 = _refinement_call(db, 1, project, 'test-model',
                                                'batch:1', 'system', 'user')
            self.assertEqual(first, second)
            self.assertEqual((hit1, hit2), (False, True))
            self.assertEqual(call.call_count, 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_events WHERE stage='structure-refinement'").fetchone()[0], 1)
            db.close()

    def test_refinement_reset_discards_batch_cache_but_keeps_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            db = connect(project)
            run = db.execute("INSERT INTO structure_runs(generation,round,max_rounds,status) VALUES(1,2,3,'needs_review')").lastrowid
            db.execute("INSERT INTO structure_versions(run_id,version,phase,tree_json,accepted) VALUES(?,1,'discovery','[]',1)",(run,))
            db.execute("INSERT INTO structure_chunks(run_id,chunk_key,result_json) VALUES(?,'v5:r1:1-10','{}')",(run,))
            db.execute("INSERT INTO structure_chunks(run_id,chunk_key,result_json) VALUES(?,'refinement-key','{}')",(run,))
            db.commit(); db.close()
            reset_stage(project, 'structure-refinement')
            db = connect(project)
            keys = [r[0] for r in db.execute('SELECT chunk_key FROM structure_chunks WHERE run_id=?',(run,))]
            self.assertEqual(keys, ['v5:r1:1-10'])
            self.assertEqual(db.execute('SELECT round FROM structure_runs WHERE id=?',(run,)).fetchone()[0], 1)
            db.close()

    def test_refinement_assembles_validated_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            db = connect(project)
            assembly = db.execute("INSERT INTO assembly_runs(generation,status) VALUES(1,'completed')").lastrowid
            for n, text, kind in ((1, '1. First', 'heading'), (2, 'Detail', 'subheading'),
                                  (3, '2. Second', 'heading')):
                db.execute('INSERT INTO manuscript_units(run_id,ordinal,kind,text) VALUES(?,?,?,?)',
                           (assembly, n, kind, text))
            run = db.execute("INSERT INTO structure_runs(generation,model,status,assembly_run_id,created,round,max_rounds) VALUES(1,'test-model','needs_review',?,0,1,3)",(assembly,)).lastrowid
            first = db.execute("INSERT INTO structure_nodes(run_id,ordinal,node_type,title,number,start_unit,end_unit) VALUES(?,1,'chapter','1. First',1,1,2)",(run,)).lastrowid
            db.execute("INSERT INTO structure_nodes(run_id,ordinal,parent_id,node_type,title,start_unit,end_unit) VALUES(?,2,?,'section','Detail',2,2)",(run,first))
            db.execute("INSERT INTO structure_nodes(run_id,ordinal,node_type,title,number,start_unit,end_unit) VALUES(?,3,'chapter','2. Second',2,3,3)",(run,))
            db.commit()
            sizes = []
            calls = {'local': 0, 'global': 0}
            def provider(user, source, target, provider, env_file, image_path, system, model, **kwargs):
                sizes.append(request_bytes(system, user))
                if system.startswith('Check the proposed hierarchy'):
                    calls['global'] += 1
                    if calls['global'] == 1:
                        raise TimeoutError('temporary global failure')
                    result = {'content_start_unit': 1, 'backmatter_start_unit': 4,
                              'updates': [], 'remove_units': [], 'unresolved': [], 'done': True}
                else:
                    calls['local'] += 1
                    headings = json.loads(user)['headings']
                    result = {'action': 'propose', 'nodes': [
                        {'unit': h['ordinal'], 'title': h['text'],
                         'node_type': 'section' if h['kind'] == 'subheading' else 'chapter',
                         'number': 1 if h['ordinal'] == 1 else (2 if h['ordinal'] == 3 else None),
                         'parent_start_unit': 1 if h['ordinal'] == 2 else None}
                        for h in headings], 'excluded_units': [], 'unresolved': []}
                return json.dumps(result), {'model': model}
            db.close()
            progress = []
            set_progress_hook(lambda stage, current, total, detail: progress.append((current,total)))
            try:
                with patch.dict(os.environ, {'BOOK2EPUB_STRUCTURE_MAX_ROUNDS': '3'}), patch('book2epub.pipeline.translate_segment', side_effect=provider):
                    with self.assertRaisesRegex(TimeoutError, 'temporary global failure'):
                        structure_refinement(project, model='test-model')
                    progress.clear()
                    structure_refinement(project, model='test-model')
            finally:
                set_progress_hook(None)
            db = connect(project)
            result = db.execute('SELECT status,convergence FROM structure_runs WHERE id=?',(run,)).fetchone()
            self.assertEqual(tuple(result), ('completed', 1))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM structure_nodes WHERE run_id=?',(run,)).fetchone()[0], 3)
            self.assertTrue(sizes and all(size <= 40000 for size in sizes))
            self.assertEqual(calls, {'local': 2, 'global': 3})
            self.assertEqual(progress[-1], (4, 4))
            self.assertEqual(progress, sorted(progress))
            db.close()

    def test_unaccounted_heading_keeps_prior_accepted_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory); db = connect(project)
            assembly = db.execute("INSERT INTO assembly_runs(generation,status) VALUES(1,'completed')").lastrowid
            db.execute("INSERT INTO manuscript_units(run_id,ordinal,kind,text) VALUES(?,1,'heading','1. First')",(assembly,))
            run = db.execute("INSERT INTO structure_runs(generation,model,status,assembly_run_id,created,round) VALUES(1,'test-model','needs_review',?,0,1)",(assembly,)).lastrowid
            db.execute("INSERT INTO structure_nodes(run_id,ordinal,node_type,title,start_unit,end_unit) VALUES(?,1,'chapter','1. First',1,1)",(run,))
            db.commit(); db.close()
            with patch('book2epub.pipeline.translate_segment', return_value=(json.dumps({'action':'propose','nodes':[],'excluded_units':[]}), {'model':'test-model'})):
                with self.assertRaisesRegex(ValueError, 'unaccounted for'):
                    structure_refinement(project, model='test-model')
            db=connect(project)
            self.assertEqual(db.execute('SELECT title FROM structure_nodes WHERE run_id=?',(run,)).fetchone()[0], '1. First')
            self.assertEqual(db.execute('SELECT round FROM structure_runs WHERE id=?',(run,)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM structure_chunks WHERE run_id=?',(run,)).fetchone()[0], 0)
            db.close()

    def test_exhausted_rounds_raise_instead_of_reporting_job_success(self):
        with tempfile.TemporaryDirectory() as directory:
            project=Path(directory); db=connect(project)
            assembly=db.execute("INSERT INTO assembly_runs(generation,status) VALUES(1,'completed')").lastrowid
            db.execute("INSERT INTO manuscript_units(run_id,ordinal,kind,text) VALUES(?,1,'heading','1. First')",(assembly,))
            run=db.execute("INSERT INTO structure_runs(generation,model,status,assembly_run_id,created,round) VALUES(1,'test-model','needs_review',?,0,1)",(assembly,)).lastrowid
            db.execute("INSERT INTO structure_nodes(run_id,ordinal,node_type,title,number,start_unit,end_unit) VALUES(?,1,'chapter','1. First',1,1,1)",(run,))
            db.commit(); db.close()
            def provider(user, source, target, provider, env_file, image_path, system, model, **kwargs):
                if system.startswith('Check the proposed hierarchy'):
                    result={'content_start_unit':1,'backmatter_start_unit':2,'updates':[],
                            'remove_units':[],'unresolved':[]}
                else:
                    result={'action':'propose','nodes':[{'unit':1,'title':'1. First',
                            'node_type':'chapter','number':1,'parent_start_unit':None}],
                            'excluded_units':[],'unresolved':[]}
                return json.dumps(result),{'model':model}
            with patch.dict(os.environ,{'BOOK2EPUB_STRUCTURE_MAX_ROUNDS':'2'}), patch('book2epub.pipeline.translate_segment',side_effect=provider):
                with self.assertRaisesRegex(RuntimeError,'did not converge'):
                    structure_refinement(project,model='test-model')
            db=connect(project)
            self.assertEqual(db.execute('SELECT status FROM structure_runs WHERE id=?',(run,)).fetchone()[0], 'failed_validation')
            db.close()


if __name__ == '__main__':
    unittest.main()
