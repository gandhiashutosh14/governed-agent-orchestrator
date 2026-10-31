import tempfile
import unittest
from pathlib import Path
from harness import Harness, VersionedContextGraph, SAMPLE_REPO, build_demo
from server import ingest_signal

class HarnessTests(unittest.TestCase):
    def test_fixture_real_tests_pass(self):
        self.assertTrue(Harness().test_gate()['passed'])

    def test_knowledge_graph_has_import_and_issue_links(self):
        h = Harness()
        graph = h.context_graph.with_issues(h.issues)
        self.assertTrue(any(e['from']=='file:src/api.py' and e['to']=='file:src/auth.py' for e in graph['edges']))
        self.assertTrue(any(e['from']=='issue:ENG-101' and e['to']=='file:src/auth.py' for e in graph['edges']))

    def test_context_invalidation_on_file_change(self):
        with tempfile.TemporaryDirectory() as path:
            p = Path(path)
            (p/'main.py').write_text('def hello(): return 1\n')
            g1 = VersionedContextGraph(p)
            self.assertNotEqual(g1.files['main.py']['sha'], 'none')
            (p/'main.py').write_text('def hello(): return 2\n')
            g2 = VersionedContextGraph(p)
            self.assertNotEqual(g1.files['main.py']['sha'],g2.files['main.py']['sha'])

    def test_dedup_and_independent_reviewers(self):
        d = Harness().run('optimized')
        by_id = {x['id']:x for x in d['issues']}
        self.assertEqual(by_id['ENG-105']['status'], 'deduplicated')
        self.assertEqual(by_id['ENG-101']['status'], 'human_approval_required')
        self.assertEqual(len(set(by_id['ENG-102']['reviewers'])),2)

    def test_stale_revisions_block_baseline_review(self):
        base = Harness().run('baseline',inject_change=True)
        opt = Harness().run('optimized',inject_change=True)
        self.assertGreater(base['summary']['stale_context_uses'],0)
        self.assertEqual(opt['summary']['stale_context_uses'],0)
        self.assertEqual({x['id']:x for x in base['issues']}['ENG-102']['status'],'stale_or_failed_gate')
        self.assertEqual({x['id']:x for x in opt['issues']}['ENG-102']['status'],'simulation_gates_passed')

    def test_budget_preflight(self):
        data=Harness().run('optimized',budget=1000)
        self.assertTrue(any(x['status']=='budget_blocked' for x in data['issues']))

    def test_simulation_savings_are_not_claimed_real(self):
        with tempfile.TemporaryDirectory() as p:
            compare=build_demo(dest=p)['comparison']
            self.assertGreater(compare['modeled_token_reduction_pct'],0)
            self.assertIn('not measured LLM',compare['notes'])

    def test_reject_raw_logs(self):
        with self.assertRaises(ValueError):
            ingest_signal({'service':'api','signal_type':'error','count':1,'raw_body':'private'})

if __name__=='__main__':
    unittest.main()
