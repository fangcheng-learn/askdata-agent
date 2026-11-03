import hashlib
import os
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from app.main import app
from app.security.auth import AuthService
from app.security.access_control import AccessController
from app.security import public_demo, trial_quota


class CloudAdminTests(unittest.TestCase):
    def setUp(self):
        salt = b'offline-test-only'
        encoded = 'pbkdf2_sha256$600000$' + salt.hex() + '$' + hashlib.pbkdf2_hmac(
            'sha256', b'offline-admin-secret', salt, 600000).hex()
        self.enterContext(patch.dict(os.environ, {
            'INSIGHTPILOT_PUBLIC_DEMO': 'true',
            'INSIGHTPILOT_PUBLIC_ORIGIN': 'https://trial.example',
            'INSIGHTPILOT_VISITOR_SECRET': 'test-only-signing-key-not-for-deployment',
            'INSIGHTPILOT_ADMIN_PASSWORD_HASH': encoded,
        }))

    def test_secure_admin_identity_guest_switch_and_logout(self):
        auth = AuthService()
        self.assertIsNone(auth.login('admin', 'admin123'))
        self.assertIsNone(auth.login('sales', 'offline-admin-secret'))
        token, admin = auth.login('admin', 'offline-admin-secret')
        self.assertEqual((admin.role, admin.user_id), ('admin', public_demo.ADMIN_USER_ID))
        self.assertEqual(auth.authenticate('Bearer ' + token), admin)
        self.assertEqual(AccessController().resolve(admin.user_id).allowed_databases,
                         frozenset({public_demo.DATASOURCE}))
        guest_token, guest = auth.guest_login(token)
        self.assertNotEqual(guest.user_id, admin.user_id)
        self.assertEqual(guest.role, 'visitor')
        self.assertEqual(auth.authenticate('Bearer ' + guest_token), guest)
        auth.logout('Bearer ' + token)
        self.assertIsNone(auth.authenticate('Bearer ' + token))
        with patch.dict(os.environ, {'INSIGHTPILOT_ADMIN_PASSWORD_HASH': 'malformed'}):
            self.assertIsNone(auth.login('admin', 'offline-admin-secret'))

    def test_admin_login_secure_cookies_and_server_authorization(self):
        with TestClient(app, base_url='https://trial.example') as c:
            headers = {'Origin': 'https://trial.example'}
            endpoint = '/api/foundry/v1/auth/login'
            credentials = {'email': 'admin', 'password': 'offline-admin-secret'}
            self.assertEqual(c.post(endpoint, headers={'Origin': 'https://evil.example'},
                                   json=credentials).status_code, 403)
            self.assertEqual(c.post(endpoint, headers=headers,
                                   json={'email': 'admin', 'password': 'admin123', 'role': 'admin'}).status_code, 401)
            response = c.post(endpoint, headers=headers, json=credentials)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all('Secure' in h for h in response.headers.get_list('set-cookie')))
            self.assertEqual(response.json()['data']['user']['id'], public_demo.ADMIN_USER_ID)
            token = c.cookies['ip_session']
            self.assertEqual(c.post('/api/foundry/v1/auth/logout', headers={
                **headers, 'X-CSRF-Token': c.cookies['ip_csrf']}).status_code, 200)
            self.assertEqual(c.get('/api/foundry/v1/me', headers={'Authorization': 'Bearer ' + token}).status_code, 401)

    def quota_connection(self, spent=0, busy=False):
        conn = MagicMock()
        def execute(sql, params=None):
            row = None
            if 'SELECT run_id, lease_until' in sql: row = ('other' if busy else None, busy)
            elif 'AT TIME ZONE' in sql: row = ('2026-11-03',)
            elif 'SELECT reserved_microyuan,halted' in sql: row = (spent, False)
            elif 'SELECT starts' in sql: row = (2,)
            result = MagicMock()
            result.fetchone.return_value = row
            return result
        conn.execute.side_effect = execute
        return conn

    def test_admin_bypasses_only_visitor_count_not_money_or_concurrency(self):
        conn = self.quota_connection()
        with patch.object(trial_quota, 'transaction') as transaction:
            transaction.return_value.__enter__.return_value = conn
            trial_quota.begin_run('admin-run', public_demo.ADMIN_USER_ID)
            self.assertFalse(any('SET starts=starts+1' in call.args[0] for call in conn.execute.call_args_list))
            with self.assertRaisesRegex(trial_quota.TrialLimit, '2 次'):
                trial_quota.begin_run('guest-run', 'visitor_test')
            for blocked in [self.quota_connection(spent=trial_quota.DAILY_LIMIT),
                            self.quota_connection(busy=True)]:
                transaction.return_value.__enter__.return_value = blocked
                with self.assertRaises(trial_quota.TrialLimit):
                    trial_quota.begin_run('admin-run', public_demo.ADMIN_USER_ID)
