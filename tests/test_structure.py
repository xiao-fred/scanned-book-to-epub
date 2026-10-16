import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book2epub.db import connect
from book2epub.pipeline import _embedded_text_quality, _pdf_text_pages, _reconcile_structure_candidates, _structure_max_rounds, _validate_structure_tree


class StructureValidationTests(unittest.TestCase):
    def test_embedded_pdf_text_is_split_per_page(self):
        with patch('book2epub.pipeline.shutil.which', return_value='/usr/bin/pdftotext'), patch('book2epub.pipeline.subprocess.check_output', return_value='first page\fsecond page\f'):
            self.assertEqual(_pdf_text_pages(Path('source.pdf')), ['first page', 'second page', ''])

    def test_embedded_text_quality_rejects_empty_or_tiny_text(self):
        self.assertEqual(_embedded_text_quality(''), 0.0)
        self.assertEqual(_embedded_text_quality('short'), 0.0)
        self.assertGreater(_embedded_text_quality('A sufficiently long embedded text page with many words.'), 0.5)

    def test_structure_round_default_and_cap(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_structure_max_rounds(), 10)
        with patch.dict(os.environ, {'BOOK2EPUB_STRUCTURE_MAX_ROUNDS': 'invalid'}):
            self.assertEqual(_structure_max_rounds(), 10)
        with patch.dict(os.environ, {'BOOK2EPUB_STRUCTURE_MAX_ROUNDS': '50'}):
            self.assertEqual(_structure_max_rounds(), 10)
        with patch.dict(os.environ, {'BOOK2EPUB_STRUCTURE_MAX_ROUNDS': '6'}):
            self.assertEqual(_structure_max_rounds(), 6)

    def test_new_database_uses_ten_round_default(self):
        with tempfile.TemporaryDirectory() as directory:
            db=connect(Path(directory))
            column=db.execute("PRAGMA table_info(structure_runs)").fetchall()
            max_rounds=next(row for row in column if row['name']=='max_rounds')
            self.assertEqual(max_rounds['dflt_value'], '10')
            columns={row['name'] for row in column}
            self.assertTrue({'toc_status','toc_confidence','toc_pages_json','toc_entries_json','structure_source'} <= columns)
            db.close()

    def test_reconciliation_is_semantics_free(self):
        units=[{'ordinal':1,'kind':'heading','text':'A'}, {'ordinal':2,'kind':'heading','text':'B'}]
        candidates=[
            {'start_unit':1,'title':'A','node_type':'chapter','confidence':.9},
            {'start_unit':1,'title':'duplicate','node_type':'section','confidence':.1},
            {'start_unit':2,'title':'B','node_type':'section','parent_start_unit':1},
        ]
        nodes,warnings=_reconcile_structure_candidates(units,candidates)
        self.assertEqual([n['unit'] for n in nodes],[1,2])
        self.assertEqual(nodes[0]['node_type'],'chapter')
        self.assertEqual(warnings,[])

    def test_valid_tree_allows_number_only_chapter(self):
        nodes=[
            {'unit':10,'title':'1. Opening','node_type':'chapter','number':1,'parent_start_unit':None},
            {'unit':20,'title':None,'node_type':'chapter','number':2,'parent_start_unit':None},
            {'unit':21,'title':'Detail','node_type':'section','parent_start_unit':20},
        ]
        errors,warnings=_validate_structure_tree(nodes,10,30)
        self.assertEqual(errors,[])
        self.assertTrue(any(x['issue']=='chapter_title_missing' for x in warnings))

    def test_hard_invariants_reject_bad_global_tree(self):
        nodes=[
            {'unit':10,'title':'1. One','node_type':'chapter','number':1,'parent_start_unit':None},
            {'unit':10,'title':'1. One','node_type':'chapter','number':1,'parent_start_unit':None},
            {'unit':12,'title':'Orphan','node_type':'section','parent_start_unit':99},
            {'unit':35,'title':'Printed contents','node_type':'part','parent_start_unit':None},
        ]
        errors,_=_validate_structure_tree(nodes,10,30)
        codes={x['issue'] for x in errors}
        self.assertIn('duplicate_or_invalid_anchor',codes)
        self.assertIn('duplicate_chapter_number',codes)
        self.assertIn('invalid_section_parent',codes)
        self.assertIn('backmatter_or_toc_navigation_node',codes)


if __name__=='__main__':
    unittest.main()
