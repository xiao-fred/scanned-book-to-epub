import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book2epub.db import connect, meta_set
from book2epub import pipeline


class AssemblyBoundaryTests(unittest.TestCase):
    def test_split_long_groups_sentences_without_mid_sentence_cuts(self):
        text = ' '.join(f'Sentence {i} has words.' for i in range(1, 7))
        chunks = pipeline._split_long(text, 15)
        self.assertEqual(chunks, [
            'Sentence 1 has words. Sentence 2 has words. Sentence 3 has words.',
            'Sentence 4 has words. Sentence 5 has words. Sentence 6 has words.',
        ])

    def test_split_long_preserves_single_overlong_sentence(self):
        text = ' '.join(['word'] * 25) + '.'
        self.assertEqual(pipeline._split_long(text, 10), [text])

    def test_split_long_reads_configured_default_limit(self):
        old = os.environ.get('BOOK2EPUB_ASSEMBLY_MAX_WORDS')
        try:
            os.environ['BOOK2EPUB_ASSEMBLY_MAX_WORDS'] = '5'
            text = 'One two three. Four five six.'
            self.assertEqual(pipeline._split_long(text), ['One two three.', 'Four five six.'])
        finally:
            if old is None:
                os.environ.pop('BOOK2EPUB_ASSEMBLY_MAX_WORDS', None)
            else:
                os.environ['BOOK2EPUB_ASSEMBLY_MAX_WORDS'] = old

    def make_project(self):
        root = Path(tempfile.mkdtemp())
        db = connect(root)
        meta_set(db, 'source_lang', 'eng')
        for number, text in enumerate((
            'Terminal sentence.',
            'A paragraph that continues',
            'lowercase across the page',
            'Uppercase boundary starts here',
        ), 1):
            page = db.execute(
                'INSERT INTO pages(number,image,text,status) VALUES(?,?,?,?)',
                (number, f'page-{number:05d}.png', text, 'done')).lastrowid
            db.execute(
                'INSERT INTO blocks(page_id,ordinal,kind,text,corrected_text,corrected_kind,corrected_confidence,status) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (page, 1, 'paragraph', text, text, 'paragraph', 1.0, 'approved'))
        db.commit()
        db.close()
        return root

    def run_mode(self, mode, response=None):
        root = self.make_project()
        calls = []

        def fake_translate(*args, **kwargs):
            calls.append(args[0])
            result = response or {'decisions': [
                {'index': 0, 'decision': 'split', 'confidence': 0.99, 'reason': 'new paragraph'},
            ]}
            return json.dumps(result), {'model': 'test-model', 'input_tokens': 3,
                                        'output_tokens': 2, 'cost': 0.01, 'currency': 'USD'}

        old = os.environ.copy()
        try:
            os.environ.update({
                'BOOK2EPUB_ASSEMBLY_JOIN_MODE': mode,
                'BOOK2EPUB_ASSEMBLY_JOIN_MODEL': 'test-model',
                'BOOK2EPUB_ASSEMBLY_JOIN_CONFIDENCE': '0.75',
                'BOOK2EPUB_ASSEMBLY_JOIN_BATCH_SIZE': '40',
            })
            with patch.object(pipeline, 'translate_segment', fake_translate):
                pipeline.assemble(root)
            db = connect(root)
            run = db.execute('SELECT * FROM assembly_runs ORDER BY id DESC LIMIT 1').fetchone()
            units = db.execute('SELECT * FROM manuscript_units WHERE run_id=? ORDER BY ordinal', (run['id'],)).fetchall()
            report = json.loads(db.execute("SELECT value FROM metadata WHERE key='assembly_validation_report'").fetchone()[0])
            usage = db.execute("SELECT COUNT(*) FROM usage_events WHERE stage='assembly-join'").fetchone()[0]
            db.close()
            return calls, units, report, usage
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_hybrid_only_sends_ambiguous_boundary(self):
        calls, units, report, usage = self.run_mode('hybrid')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(units), 3)
        self.assertEqual(units[1]['text'], 'A paragraph that continues lowercase across the page')
        self.assertEqual(report['result'], 'pass')
        self.assertEqual(usage, 1)

    def test_llm_sends_every_eligible_boundary(self):
        calls, units, report, usage = self.run_mode('llm', {
            'decisions': [
                {'index': 0, 'decision': 'split', 'confidence': 1},
                {'index': 1, 'decision': 'join', 'confidence': 1},
                {'index': 2, 'decision': 'split', 'confidence': 1},
            ]
        })
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].count('LEFT block='), 3)
        self.assertEqual(len(units), 3)
        self.assertEqual(report['result'], 'pass')
        self.assertEqual(usage, 1)

    def test_low_confidence_falls_back_without_text_loss(self):
        calls, units, report, _ = self.run_mode('hybrid', {
            'decisions': [{'index': 0, 'decision': 'join', 'confidence': 'not-a-number'}]
        })
        self.assertEqual(len(calls), 1)
        self.assertEqual(report['fallback_join_decisions'], 1)
        self.assertEqual(len(units), 3)
        self.assertEqual(report['missing_blocks'], [])
        self.assertEqual(report['duplicate_blocks'], [])

    def test_heuristic_does_not_call_llm(self):
        calls, _, report, usage = self.run_mode('heuristic')
        self.assertEqual(calls, [])
        self.assertEqual(usage, 0)
        self.assertEqual(report['result'], 'pass')

    def test_provider_failure_uses_configured_fallback(self):
        root = self.make_project()
        old = os.environ.copy()
        try:
            os.environ.update({
                'BOOK2EPUB_ASSEMBLY_JOIN_MODE': 'hybrid',
                'BOOK2EPUB_ASSEMBLY_JOIN_MODEL': 'test-model',
                'BOOK2EPUB_ASSEMBLY_JOIN_FALLBACK': 'heuristic',
            })
            with patch.object(pipeline, 'translate_segment', side_effect=RuntimeError('provider down')):
                pipeline.assemble(root)
            db = connect(root)
            report = json.loads(db.execute("SELECT value FROM metadata WHERE key='assembly_validation_report'").fetchone()[0])
            db.close()
            self.assertEqual(report['fallback_join_decisions'], 1)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_provider_failure_can_fail_the_run(self):
        root = self.make_project()
        old = os.environ.copy()
        try:
            os.environ.update({
                'BOOK2EPUB_ASSEMBLY_JOIN_MODE': 'hybrid',
                'BOOK2EPUB_ASSEMBLY_JOIN_MODEL': 'test-model',
                'BOOK2EPUB_ASSEMBLY_JOIN_FALLBACK': 'fail',
            })
            with patch.object(pipeline, 'translate_segment', side_effect=RuntimeError('provider down')):
                with self.assertRaises(RuntimeError):
                    pipeline.assemble(root)
            db = connect(root)
            status = db.execute('SELECT status FROM assembly_runs ORDER BY id DESC LIMIT 1').fetchone()[0]
            db.close()
            self.assertEqual(status, 'failed')
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == '__main__':
    unittest.main()
