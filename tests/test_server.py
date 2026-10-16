import unittest

from book2epub.server import _refinement_display


class RefinementDisplayTests(unittest.TestCase):
    def test_authoritative_toc_is_reported_as_a_completed_noop(self):
        run = {'phase': 'toc_authoritative', 'status': 'completed'}
        self.assertEqual(
            _refinement_display(run, 'completed', 0, 9),
            ('completed', 1, 1, 'Not required: authoritative TOC accepted', True),
        )

    def test_regular_refinement_progress_is_unchanged(self):
        run = {'phase': 'agent_refinement', 'status': 'needs_review'}
        self.assertEqual(
            _refinement_display(run, 'running', 2, 9),
            ('running', 2, 9, None, False),
        )


if __name__ == '__main__':
    unittest.main()
