import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.main import app
from app.config import Settings
from app.model_client import ModelClient
from app.security.auth import AuthService
from app.security.access_control import AccessController
from app.security import public_demo, trial_quota


class PublicDemoTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            'INSIGHTPILOT_PUBLIC_DEMO': 'true',
            'INSIGHTPILOT_PUBLIC_ORIGIN': 'https://trial.example',
            'INSIGHTPILOT_VISITOR_SECRET': 'test-only-signing-key-not-for-deployment',
        }))

    def test_known_accounts_disabled_and_visitors_isolated_across_restart(self):
        auth = AuthService()
        self.assertIsNone(auth.login('admin', 'admin123'))
        a, first = auth.guest_login()
        b, second = auth.guest_login()
        self.assertNotEqual(first.user_id, second.user_id)
        restarted = AuthService()
        self.assertEqual(restarted.authenticate('Bearer ' + a), first)
        self.assertEqual(restarted.guest_login(a)[1], first)
        self.assertIsNone(restarted.authenticate('Bearer ' + a[:-1] + ('a' if a[-1] != 'a' else 'b')))
        scope = AccessController().resolve(first.user_id)
        self.assertEqual(scope.allowed_databases, frozenset({'homenova_20260831'}))
        self.assertTrue(scope.allowed_tables)
        self.assertFalse(AccessController().resolve('visitor_unverified').allowed_tables)

    def test_guest_origin_secure_cookie_and_admin_denial(self):
        with TestClient(app, base_url='https://trial.example') as c:
            endpoint = '/api/foundry/v1/auth/guest'
            self.assertEqual(c.post(endpoint).status_code, 403)
            self.assertEqual(c.post(endpoint, headers={'Origin': 'https://other.example'}).status_code, 403)
            response = c.post(endpoint, headers={'Origin': 'https://trial.example'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all('Secure' in h for h in response.headers.get_list('set-cookie')))
            self.assertEqual(c.get('/api/foundry/v1/me').status_code, 200)
            token = c.cookies['ip_session']
            headers = {'Authorization': 'Bearer ' + token}
            self.assertEqual(c.get('/api/agent-config', headers=headers).status_code, 403)
            self.assertEqual(c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'}).status_code, 401)
            self.assertEqual(c.post('/api/foundry/v1/auth/logout').status_code, 403)

    def test_no_provider_dispatch_without_durable_reservation(self):
        client = ModelClient(Settings(api_key='offline-test', llm_model='qwen3.7-plus', max_retries=0))
        with patch('urllib.request.urlopen') as network:
            with self.assertRaises(trial_quota.TrialLimit):
                client.chat('test', 'test')
            network.assert_not_called()

    def test_each_retry_gets_a_new_reservation(self):
        import urllib.error
        import io
        client = ModelClient(Settings(api_key='offline-test', llm_model='qwen3.7-plus', max_retries=1))
        failure = urllib.error.HTTPError('https://example.test', 503, 'offline', {}, io.BytesIO(b'{}'))
        with patch.object(trial_quota, 'reserve', return_value=('ticket', 12, 48)) as reserve, \
             patch('urllib.request.urlopen', side_effect=failure) as network, \
             patch('time.sleep'):
            with self.assertRaises(RuntimeError):
                client.chat('test', 'test')
            self.assertEqual(reserve.call_count, 2)
            self.assertEqual(network.call_count, 2)

    def test_trial_output_cap_and_provider_allowlist(self):
        payload = trial_quota.prepare({'model': 'qwen3.7-plus', 'enable_thinking': True}, 'chat',
                                     'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions')
        self.assertFalse(payload['enable_thinking'])
        self.assertEqual(payload['max_tokens'], trial_quota.MAX_OUTPUT)
        with self.assertRaises(trial_quota.TrialLimit):
            trial_quota.prepare({'model': 'unknown'}, 'chat', 'https://example.test')
        with patch.dict(os.environ, {'INSIGHTPILOT_QUOTA_DATABASE_URL': ''}):
            with self.assertRaises(trial_quota.TrialLimit):
                trial_quota.begin_run('id', 'visitor')

    def test_local_accounts_unchanged(self):
        with patch.dict(os.environ, {'INSIGHTPILOT_PUBLIC_DEMO': 'false'}):
            self.assertIsNotNone(AuthService().login('admin', 'admin123'))

    def test_local_model_calls_never_use_public_ledger(self):
        import io
        with patch.dict(os.environ, {'INSIGHTPILOT_PUBLIC_DEMO': 'false'}), \
             patch.object(trial_quota, 'reserve') as reserve, \
             patch('urllib.request.urlopen', return_value=io.BytesIO(
                 b'{"choices":[{"message":{"content":"offline"}}]}')):
            self.assertEqual(ModelClient(Settings(api_key='offline')).chat('test', 'test'), 'offline')
            reserve.assert_not_called()

    def test_guest_cannot_read_other_visitors_saved_conversation(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import importlib
        from app.foundry_adapter.store import UIStore
        bridge = importlib.import_module('app.foundry_adapter.router')
        a, first = AuthService().guest_login()
        b, second = AuthService().guest_login()
        with TemporaryDirectory() as directory:
            store = UIStore(Path(directory) / 'ui.sqlite3')
            store.put(first.user_id, 'session', 'private-thread', {
                'id': 'private-thread', 'source': 'homenova_20260831',
                'backendSession': 'private-thread', 'title': 'PRIVATE_TEST_RECORD'})
            with patch.object(bridge, 'store', store), \
                 patch.object(bridge, 'import_history', return_value={}), \
                 TestClient(app, base_url='https://trial.example') as client:
                response = client.get('/api/foundry/v1/sessions/private-thread/conversation',
                                      headers={'Authorization': 'Bearer ' + b})
                self.assertNotIn('PRIVATE_TEST_RECORD', response.text)
                self.assertEqual(response.json()['data']['messages'], [])


if __name__ == '__main__':
    unittest.main()
