"""Configuration operations are authenticated and independent of model APIs."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent_configuration_runtime import (configuration_snapshot, configuration_store,
    current_configuration, effective_prompt)
from app.config import Settings
from app.main import app
from app.models import QueryResult
from app.services.investigation_store import InvestigationStore
from app.workflows.query_graph import QueryWorkflow
from app.workflows.react_harness import ReactRecord


class AgentConfigurationApiTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'config.json'
        environment = patch.dict(os.environ, {'AGENT_CONFIGURATION_PATH': str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)
        token = current_configuration.set(None)
        self.addCleanup(current_configuration.reset, token)
        self.client = self.enterContext(TestClient(app))
        self.admin = self.login('admin', 'admin123')

    def login(self, username, password):
        response = self.client.post('/api/auth/login', json={'username': username, 'password': password})
        self.assertEqual(response.status_code, 200)
        return {'Authorization': 'Bearer ' + response.json()['access_token']}

    def test_authentication_and_admin_scope(self):
        self.assertEqual(self.client.get('/api/agent-config').status_code, 401)
        sales = self.login('sales', 'sales123')
        self.assertEqual(self.client.get('/api/agent-config', headers=sales).status_code, 403)
        self.assertEqual(self.client.put('/api/agent-config', headers=sales,
            json={'expected_revision': '0', 'prompts': {'react': 'unallowed'}}).status_code, 403)
        self.assertFalse(self.path.exists())

    def test_catalog_is_actual_default_source_and_contains_no_credentials(self):
        response = self.client.get('/api/agent-config', headers=self.admin)
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual(len(value['agents']), 6)
        self.assertEqual(len(value['prompts']), 9)
        self.assertEqual(len(value['skills']), 5)
        self.assertEqual(value['revision'], '0')
        self.assertFalse(self.path.exists())
        self.assertNotIn('api_key', response.text)
        prompt = next(p for p in value['prompts'] if p['id'] == 'sql_query')
        skill = next(p for p in value['skills'] if p['id'] == 'database_query')
        self.assertIn(skill['effective_text'], prompt['assembled_text'])
        self.assertIn('[[SKILL:database_query]]', prompt['required_placeholders'])

    def test_save_conflict_runtime_effect_and_restore_defaults(self):
        saved = self.client.put('/api/agent-config', headers=self.admin, json={
            'expected_revision': '0', 'prompts': {'react': '受控配置测试正文'},
            'skills': {'business_diagnosis': '受控业务方法'}})
        self.assertEqual(saved.status_code, 200, saved.text)
        value = saved.json()
        self.assertEqual(effective_prompt('react', 'fallback'), '受控配置测试正文')
        prompt = next(p for p in value['prompts'] if p['id'] == 'react')
        self.assertTrue(prompt['overridden'])
        conflict = self.client.put('/api/agent-config', headers=self.admin,
            json={'expected_revision': '0', 'prompts': {'react': '过期覆盖'}})
        self.assertEqual(conflict.status_code, 409)
        restored = self.client.put('/api/agent-config', headers=self.admin, json={
            'expected_revision': value['revision'], 'prompts': {'react': None},
            'skills': {'business_diagnosis': None}})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(effective_prompt('react', 'fallback'), 'fallback')
        self.assertFalse(next(p for p in restored.json()['prompts'] if p['id'] == 'react')['overridden'])

    def test_invalid_marker_path_and_unknown_fields_do_not_write(self):
        cases = [
            {'prompts': {'../../config.py': 'bad'}},
            {'skills': {'../data_qa': 'bad'}},
            {'prompts': {'sql_query': '删除了必需Skill标记'}},
            {'prompts': {'react': ''}},
            {'models': {'react': 'not_supported_here'}},
        ]
        for change in cases:
            with self.subTest(change=change):
                result = self.client.put('/api/agent-config', headers=self.admin,
                    json={'expected_revision': '0', **change})
                self.assertEqual(result.status_code, 422, result.text)
                self.assertFalse(self.path.exists())

    def test_corrupt_configuration_returns_explicit_error(self):
        self.path.write_text('{damaged', encoding='utf-8')
        response = self.client.get('/api/agent-config', headers=self.admin)
        self.assertEqual(response.status_code, 503)

    def workflow(self):
        config = Settings(harness_state_path=str(self.path.with_name('state.sqlite3')))
        workflow = QueryWorkflow(object(), object(), config)
        return workflow

    @staticmethod
    def result(task_id):
        return QueryResult(task_id=task_id, status='completed', route='direct_response', message='offline')

    def test_existing_task_retains_snapshot_new_task_uses_saved_version(self):
        workflow = self.workflow()
        first = configuration_store().update(expected_revision='0', prompts={'react': '版本一'}, actor='admin')
        observed = []

        def first_action():
            observed.append(effective_prompt('react', 'default'))
            configuration_store().update(expected_revision=first['revision'], prompts={'react': '版本二'}, actor='admin')
            observed.append(effective_prompt('react', 'default'))
            return self.result('old')

        workflow._bounded_invoke('old', first_action)
        workflow._bounded_invoke('old', lambda: (observed.append(effective_prompt('react', 'default')) or self.result('old')))
        workflow._bounded_invoke('new', lambda: (observed.append(effective_prompt('react', 'default')) or self.result('new')))
        self.assertEqual(observed, ['版本一', '版本一', '版本一', '版本二'])
        self.assertIsNone(current_configuration.get())

    def test_restored_investigation_keeps_stored_configuration(self):
        workflow = self.workflow()
        snapshot = configuration_store().update(expected_revision='0', prompts={'react': '任务创建时'}, actor='admin')
        state = {'task_id': 'persisted', 'query': '离线控制测试', 'session_id': 'offline',
                 'access_scope': {'user_id': 'demo_admin'}, 'datasource_id': 'ecommerce_ops'}
        task = ReactRecord(state, protocol_version=3, configuration_snapshot=snapshot,
                           final_status='partial')
        workflow.harness._persist(task)
        configuration_store().update(expected_revision=snapshot['revision'], prompts={'react': '之后的配置'}, actor='admin')
        restored = workflow.harness.restore('persisted', 'demo_admin')
        self.assertEqual(restored.configuration_snapshot['revision'], snapshot['revision'])
        observed = []
        workflow._bounded_invoke('persisted', lambda: (
            observed.append(effective_prompt('react', 'default')) or self.result('persisted')))
        self.assertEqual(observed, ['任务创建时'])

    def test_historical_task_without_configuration_uses_original_defaults(self):
        workflow = self.workflow()
        configuration_store().update(expected_revision='0', prompts={'react': '新全局配置'}, actor='admin')
        workflow.harness.records['legacy'] = ReactRecord({'task_id': 'legacy'})
        observed = []
        workflow._bounded_invoke('legacy', lambda: (
            observed.append(effective_prompt('react', '原默认')) or self.result('legacy')))
        self.assertEqual(observed, ['原默认'])


if __name__ == '__main__':
    unittest.main()
