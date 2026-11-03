"""History survives account/session reload without running any model or SQL."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.models import QueryResult
from app.services.session_context import SessionContext
from tests.test_react_service_restore import ReactServiceRestoreTest, OWNER, SOURCE, SCOPED_SESSION, saved_record


class HistoryPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = ReactServiceRestoreTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.archive_path = Path(self.fixture.temp.name) / 'sessions.db'

    def service(self):
        service = self.fixture.service()
        service.config = replace(service.config, session_archive_enabled=True,
                                 session_archive_path=str(self.archive_path), short_term_summary_enabled=False)
        service.context = SessionContext(service.model_client, service.config)
        service.tasks, service.session_tasks = service.context.tasks, service.context.session_tasks
        return service

    def test_regular_and_failed_turns_survive_service_recreation(self):
        service = self.service()
        for tid, status in [('ordinary', 'completed'), ('failed', 'failed')]:
            service.context.remember(SCOPED_SESSION, QueryResult(task_id=tid, status=status,
                route='direct_response', message='受控历史', analysis='已保存内容', datasource_id=SOURCE),
                '历史问题', {'datasource_id': SOURCE}, OWNER)
        restored = self.service()
        rows = restored.list_history(OWNER, SOURCE)
        self.assertEqual({row['task_id'] for row in rows}, {'ordinary', 'failed'})
        self.assertTrue(all(row['session_id'] == 'restore-session' for row in rows))
        self.assertTrue(all(row['updated_at'] for row in rows))
        self.assertFalse(restored.list_history('demo_admin', SOURCE))

    def test_existing_investigation_checkpoint_recovers_without_archive_or_calls(self):
        record = saved_record()
        self.fixture.persist(record)
        rows = self.service().list_history(OWNER, SOURCE)
        self.assertEqual([row['task_id'] for row in rows], ['stored-task'])
        self.assertEqual(rows[0]['result']['budget']['tools'], 5)
        self.assertEqual(rows[0]['result']['budget']['active_seconds'], 19)
        self.assertTrue(rows[0]['updated_at'])

    def test_hide_is_durable_scoped_and_preserves_original_task(self):
        self.fixture.persist(saved_record())
        service = self.service()
        service.hide_history(OWNER, SOURCE, 'restore-session')
        self.assertFalse(self.service().list_history(OWNER, SOURCE))
        self.assertIsNotNone(self.fixture.store.load_task('stored-task', OWNER))
        self.assertEqual(self.service().get_task('stored-task', OWNER).status, 'completed')

    def test_revoked_datasource_permission_and_unscoped_legacy_archive_not_exposed(self):
        service = self.service()
        service.context.remember('unscoped-old-session', QueryResult(task_id='legacy', status='completed',
            route='direct_response', message='旧档案'), '旧问题', {'datasource_id': SOURCE})
        self.assertFalse(self.service().list_history(OWNER, SOURCE))
        with self.assertRaises(PermissionError):
            self.service().list_history('demo_analyst', SOURCE)


if __name__ == '__main__':
    unittest.main()
