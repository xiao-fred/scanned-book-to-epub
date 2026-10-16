import unittest
import json
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import patch

from book2epub.toc import detect_toc_candidates, parse_toc_entries, match_toc_to_body, toc_quality_gate, image_confirmation_prompt
from book2epub.db import connect
from book2epub.pipeline import _toc_authoritative_structure


class TocTests(unittest.TestCase):
    def test_detects_contents_page_and_adjacent_page(self):
        units = [
            {'ordinal': 1, 'kind': 'heading', 'text': 'CONTENTS'},
            {'ordinal': 2, 'kind': 'list_item', 'text': 'INTRODUCTION 1'},
            {'ordinal': 3, 'kind': 'list_item', 'text': '1: CONDITIONAL PARENTING 10'},
            {'ordinal': 4, 'kind': 'paragraph', 'text': 'Front matter'},
        ]
        candidates = detect_toc_candidates(units)
        self.assertEqual(candidates[0]['start_unit'], 1)
        self.assertGreaterEqual(candidates[0]['score'], 0.8)

    def test_parses_numbered_and_backmatter_entries(self):
        entries = parse_toc_entries('''CONTENTS\nINTRODUCTION 1\n1: CONDITIONAL PARENTING 10\n2: GIVING AND WITHHOLDING LOVE 24\nAPPENDIX • Parenting Styles: Culture 212\nNOTES 222''')
        self.assertEqual([x['number'] for x in entries[:3]], [None, 1, 2])
        self.assertEqual(entries[3]['entry_type'], 'appendix')
        self.assertEqual(entries[4]['entry_type'], 'notes')
        self.assertEqual(entries[2]['printed_page'], 24)

    def test_matches_toc_to_body_in_order(self):
        toc = parse_toc_entries('INTRODUCTION 1\n1: CONDITIONAL PARENTING 10\n2: GIVING AND WITHHOLDING LOVE 24')
        body = [
            {'ordinal': 10, 'kind': 'heading', 'text': 'INTRODUCTION'},
            {'ordinal': 20, 'kind': 'heading', 'text': 'CONDITIONAL\nPARENTING'},
            {'ordinal': 30, 'kind': 'heading', 'text': 'GIVING AND\nWITHHOLDING LOVE'},
        ]
        report = match_toc_to_body(toc, body)
        self.assertEqual([x['body_unit'] for x in report['matches']], [10, 20, 30])
        self.assertEqual(report['matched_substantive'], 3)

    def test_quality_gate_requires_numbered_chapters_and_order(self):
        report = {'entries': parse_toc_entries('INTRODUCTION 1\n1: ONE 10\n2: TWO 20'),
                  'matches': [{'toc_index': 0, 'body_unit': 1}, {'toc_index': 1, 'body_unit': 2}, {'toc_index': 2, 'body_unit': 3}],
                  'matched_substantive': 3, 'numbered_entries': 2, 'matched_numbered': 2,
                  'issues': []}
        decision = toc_quality_gate(report)
        self.assertTrue(decision['authoritative'])
        self.assertGreaterEqual(decision['confidence'], 0.95)

    def test_image_confirmation_prompt_requires_structured_entries(self):
        prompt = image_confirmation_prompt('1: ONE 10')
        self.assertIn('is_toc', prompt)
        self.assertIn('printed_page', prompt)

    def test_authoritative_structure_is_materialized_from_toc(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            db = connect(project)
            assembly = db.execute("INSERT INTO assembly_runs(generation,status) VALUES(1,'completed')").lastrowid
            texts = [('CONTENTS', 'heading'), ('INTRODUCTION 1', 'list_item'),
                     ('1: ONE 10', 'list_item'), ('2: TWO 20', 'list_item'),
                     ('INTRODUCTION', 'heading'), ('ONE', 'heading'), ('TWO', 'heading')]
            for ordinal, (text, kind) in enumerate(texts, 1):
                db.execute('INSERT INTO manuscript_units(run_id,ordinal,kind,text,provenance_json) VALUES(?,?,?,?,?)',
                           (assembly, ordinal, kind, text, '[]'))
            run = db.execute("INSERT INTO structure_runs(generation,model,status,assembly_run_id,created) VALUES(1,'test','running',?,0)",(assembly,)).lastrowid
            db.commit()
            units = [dict(x) for x in db.execute('SELECT * FROM manuscript_units WHERE run_id=? ORDER BY ordinal',(assembly,))]
            self.assertTrue(_toc_authoritative_structure(db, run, units, project, None))
            row = db.execute('SELECT status,phase,toc_status,structure_source FROM structure_runs WHERE id=?',(run,)).fetchone()
            self.assertEqual(tuple(row), ('completed', 'toc_authoritative', 'authoritative', 'toc'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM structure_nodes WHERE run_id=?',(run,)).fetchone()[0], 3)
            db.close()


if __name__ == '__main__':
    unittest.main()
