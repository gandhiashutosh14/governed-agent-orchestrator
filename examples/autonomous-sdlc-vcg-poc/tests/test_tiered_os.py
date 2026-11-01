import tempfile
import unittest
from pathlib import Path
from tiered_os import Task, Evidence, SkillCatalog, TierRouter, TieredOperatingSystem, VersionedEvidenceStore, demonstration

class TieredOsTests(unittest.TestCase):
    def test_skills_progressively_load(self):
        c = SkillCatalog()
        self.assertEqual(c.load_count, 0)
        self.assertIn('context-refresh', [x['name'] for x in c.metadata()])
        c.invoke('context-refresh')
        self.assertEqual(c.load_count, 1)
        self.assertGreater(c.loaded_instruction_chars, 50)

    def test_tier_selection(self):
        router = TierRouter(SkillCatalog())
        self.assertEqual(router.decide(Task('x', 'where is auth?', 'lookup', risk='low'), 10000)['tier'], 'deterministic')
        self.assertEqual(router.decide(Task('x', 'triage', 'incident'), 10000)['tier'], 'small-model')
        self.assertEqual(router.decide(Task('x', 'code', 'code_change'), 10000)['tier'], 'frontier-agent')

    def test_budget_fails_closed_not_downgraded(self):
        router = TierRouter(SkillCatalog())
        r = router.decide(Task('x', 'approve release', 'release', risk='high'), 100)
        self.assertEqual(r['status'], 'budget_blocked')
        self.assertEqual(r['tier'], 'frontier-agent')

    def test_skill_minimum_tier_enforced(self):
        router = TierRouter(SkillCatalog())
        r=router.decide(Task('x','simple?','lookup',risk='low',required_skills=('independent-review',)),10000)
        self.assertEqual(r['tier'],'frontier-agent')

    def test_tenant_isolation_and_exact_revision(self):
        mem = VersionedEvidenceStore()
        mem.add(Evidence('a','tenant-1','src/api.py',1,'curated','hello',issue_id='I'))
        self.assertIsNone(mem.get('tenant-2','a'))
        self.assertIsNone(mem.get('tenant-1','a',revision=2))
        self.assertIsNotNone(mem.get('tenant-1','a',revision=1))

    def test_invalidation_on_version_change(self):
        mem=VersionedEvidenceStore()
        mem.add(Evidence('a','demo','src/a.py',1,'curated','old',issue_id='I'))
        mem.add(Evidence('a','demo','src/a.py',2,'curated','new',issue_id='I'))
        self.assertIsNone(mem.get('demo','a',revision=1))
        self.assertEqual(mem.invalidations,1)
        with self.assertRaises(ValueError):
            mem.add(Evidence('a','demo','src/a.py',1,'curated','old',issue_id='I'))

    def test_context_not_reused_across_commits(self):
        mem=VersionedEvidenceStore()
        mem.add(Evidence('a','demo','src/a.py',1,'curated','source',issue_id='I',commit_sha='oldsha'))
        self.assertEqual(mem.find(Task('I','task','lookup',commit_sha='newsha')),[])

    def test_receipts_commit_bound(self):
        mem=VersionedEvidenceStore()
        mem.add(Evidence('receipt1','demo','CI',1,'receipt','passed',issue_id='I',commit_sha='sha1'))
        self.assertIsNone(mem.get('demo','receipt1',issue_id='I',commit_sha='sha2'))
        self.assertIsNotNone(mem.get('demo','receipt1',issue_id='I',commit_sha='sha1'))

    def test_missing_evidence_cannot_pass(self):
        os=TieredOperatingSystem()
        result=os.run([Task('I','look up symbol','lookup',risk='low')])
        self.assertEqual(result['results'][0]['status'],'evidence_missing')

    def test_high_risk_never_auto_approved(self):
        result=demonstration()
        statuses={r['id']:r['status'] for r in result['results']}
        self.assertEqual(statuses['ENG-104'],'human_approval_required')
        self.assertEqual(statuses['ENG-106'],'human_approval_required')
        self.assertEqual(result['metadata']['provider_calls'],0)

    def test_budget_rechecks_skill_overhead(self):
        os=TieredOperatingSystem()
        os.store.add(Evidence('I:diff','demo','PR diff',1,'curated','A sanitized synthetic PR diff',issue_id='I',commit_sha='demo-commit-a1'))
        result=os.run([Task('I','review this','review',required_skills=('independent-review',))],budget_tokens=4750)
        self.assertEqual(result['results'][0]['status'],'budget_blocked')

    def test_duplicate_does_not_spend_tokens(self):
        os=TieredOperatingSystem()
        a=Task('I','incident summary','incident')
        b=Task('II','incident summary','incident')
        r=os.run([a,b])
        self.assertEqual(r['summary']['deduplicated'],1)
        self.assertEqual(r['results'][-1]['status'],'deduplicated')

    def test_demo_has_all_three_tiers(self):
        r=demonstration()
        self.assertTrue(all(r['summary']['tier_counts'][tier]>=1 for tier in ['deterministic','small-model','frontier-agent']))

if __name__=='__main__': unittest.main()