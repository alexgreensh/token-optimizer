import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest

API = Path(__file__).resolve().parents[1] / 'plugins/token-optimizer-bar/dashboard/plugin_api.py'
spec = importlib.util.spec_from_file_location('desktop_bar_api', API)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = api.HOME
        api.HOME = Path(self.tmp.name)
        (api.HOME / 'limit-watches').mkdir()
        self.state = api.HOME / 'limit-watches/account-limits.json'

    def tearDown(self):
        api.HOME = self.old_home
        self.tmp.cleanup()

    def test_missing_and_bad_context(self):
        result = api.status({'context_used': 'invalid', 'context_max': 0})
        self.assertIsNone(result['quality'])
        self.assertEqual(result['limits'], {})

    def test_account_exact_match_freshness_and_no_private_fields(self):
        now = time.time()
        self.state.write_text(json.dumps({
            'openai-codex': {'windows': {'Session': {'used': 25, 'checked_at': now, 'reset': now + 60}, 'Weekly': {'used': 40, 'checked_at': now - 1800}}},
            '_activity': {'counters': {'session-1|model-1|openai-codex|endpoint': {}}},
            '_private': {'token': 'private-value'},
        }))
        result = api.status({'session_id': 'session-1', 'model': 'model-1'})
        self.assertEqual(result['provider'], 'openai-codex')
        self.assertFalse(result['limits']['openai-codex']['five_hour']['stale'])
        self.assertTrue(result['limits']['openai-codex']['weekly']['stale'])
        self.assertNotIn('private-value', json.dumps(result))
        self.assertIsNone(api.status({'session_id': 'session-1', 'model': 'other'})['provider'])

    def test_malformed_collector(self):
        self.state.write_text('{')
        self.assertEqual(api.status({})['limits'], {})

if __name__ == '__main__':
    unittest.main()
