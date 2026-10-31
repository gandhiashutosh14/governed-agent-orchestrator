import unittest
from src import auth, api, store

class AppTests(unittest.TestCase):
    def setUp(self):
        auth.ACTIVE.clear()
        auth.REVOKED.clear()
        store.REQUEST_COUNT = 0

    def test_revoke_blocks_access(self):
        auth.issue_token("example")
        self.assertTrue(auth.valid_token("example"))
        auth.revoke_token("example")
        self.assertFalse(auth.valid_token("example"))

    def test_api_includes_trace_id(self):
        self.assertEqual(api.handle_request("invalid", "trace-1")["trace_id"], "trace-1")

    def test_store_count(self):
        self.assertEqual(store.increment_count(), 1)
