import unittest

from book2epub.pipeline import _reconcile_structure_candidates, _validate_structure_tree


class StructureValidationTests(unittest.TestCase):
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
