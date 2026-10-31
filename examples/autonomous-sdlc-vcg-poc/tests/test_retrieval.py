import unittest
from retrieval import ContextRetriever, Document, fixture_corpus, benchmark

class RetrievalTests(unittest.TestCase):
    def test_tiny_gold_evidence_coverage_and_context_budget(self):
        result = benchmark()
        b, a = result['summary']['fixed'], result['summary']['adaptive']
        self.assertEqual(b['gold_evidence_recall'], 1.0)
        self.assertEqual(a['gold_evidence_recall'], 1.0)
        self.assertLess(a['context_words_proxy'], b['context_words_proxy'])

    def test_tenant_isolation(self):
        retriever = ContextRetriever(fixture_corpus()[0])
        res = retriever.retrieve('invoice refund', tenant='demo')
        self.assertTrue(all(x['id'].startswith(('file:', 'issue:')) for x in res['results']))
        res = retriever.retrieve('invoice refund', tenant='support')
        self.assertTrue(all(x['id'].startswith('support:') for x in res['results']))

    def test_latest_revision_and_invalidation(self):
        old = Document('doc', 'Policy', 'old process', revision=1)
        new = Document('doc', 'Policy', 'new updated process', revision=2)
        before = ContextRetriever([old])
        after = ContextRetriever([new, old])
        self.assertNotEqual(before.version_signature, after.version_signature)
        self.assertEqual(after.docs['doc'].revision, 2)
        self.assertNotIn('old', after.docs['doc'].payload)

    def test_graph_link_expansion_for_incident(self):
        retriever = ContextRetriever(fixture_corpus()[0])
        res = retriever.retrieve('What issue is linked to the observed 401 failure and which component is affected?')
        ids = {x['id'] for x in res['results']}
        self.assertIn('issue:ENG-104', ids)
        self.assertIn('file:src/auth.py', ids)
        self.assertEqual(res['strategy'], 'hybrid_graph')

    def test_explicit_identifier_routes_to_lexical(self):
        retriever = ContextRetriever(fixture_corpus()[0])
        res = retriever.retrieve('What does issue ENG-102 request?')
        self.assertEqual(res['strategy'], 'lexical')
        self.assertEqual(res['results'][0]['id'], 'issue:ENG-102')

    def test_budget_limits_context(self):
        retriever = ContextRetriever(fixture_corpus()[0])
        res = retriever.retrieve('authentication', budget_words=20)
        self.assertLessEqual(res['context_words_proxy'], 20)

if __name__ == '__main__':
    unittest.main()
