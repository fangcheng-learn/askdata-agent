"""Runtime storage admission and rollback; temporary SQLite only, no model/data query."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from app.services.investigation_store import InvestigationStorageError, InvestigationStore


class InvestigationCapacityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = InvestigationStore(self.path)
        self.store.save_task('task', 'owner', 'session', 'snapshot', {'status': 'saved'}, {'tools': 2})

    @staticmethod
    def result():
        return {'user_id': 'owner', 'task_id': 'task', 'datasource_id': 'snapshot',
                'columns': ['value'], 'rows': [{'value': 'x' * 300000}],
                'complete': True, 'total_rows': 1}

    def test_invalid_limit_does_not_disable_control(self):
        for limit in (0, -1, None, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                InvestigationStore(self.path, max_bytes=limit)

    def test_large_checkpoint_rejected_and_old_revision_retained(self):
        limited = InvestigationStore(self.path, max_bytes=150000)
        before = limited.load_task('task', 'owner')
        with self.assertRaisesRegex(InvestigationStorageError, '容量不足'):
            limited.save_task('task', 'owner', 'session', 'snapshot',
                              {'status': 'new', 'large': 'x' * 300000}, {'tools': 9})
        self.assertEqual(limited.load_task('task', 'owner'), before)

    def test_result_and_task_update_roll_back_together(self):
        limited = InvestigationStore(self.path, max_bytes=150000)
        with self.assertRaises(InvestigationStorageError):
            limited.save_task('task', 'owner', 'session', 'snapshot', {'status': 'new'},
                              {'tools': 9}, results=[('oversized-ref', self.result())])
        self.assertEqual(limited.load_task('task', 'owner')['payload'], {'status': 'saved'})
        self.assertEqual(limited.load_task('task', 'owner')['budget']['tools'], 2)
        with self.assertRaises(KeyError):
            limited.load_result('oversized-ref', 'owner', 'task', 'snapshot')

    def test_result_only_save_obeys_same_quota(self):
        limited = InvestigationStore(self.path, max_bytes=150000)
        with self.assertRaises(InvestigationStorageError):
            limited.save_result('oversized-ref', self.result())
        with self.assertRaises(KeyError):
            limited.load_result('oversized-ref', 'owner', 'task', 'snapshot')

    def test_over_quota_existing_records_can_still_be_read(self):
        small = InvestigationStore(self.path, max_bytes=1)
        self.assertEqual(small.load_task('task', 'owner')['payload'], {'status': 'saved'})
        self.assertEqual(len(small.list_tasks('owner')), 1)
        with self.assertRaises(InvestigationStorageError):
            small.save_task('new-task', 'owner', 'session', 'snapshot', {}, {})
        self.assertIsNone(small.load_task('new-task', 'owner'))

    def test_retained_wal_and_shm_count_against_total(self):
        # A live reader prevents the write connection's normal final-close
        # checkpoint, making this a real WAL file rather than a fake artifact.
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        reader.execute('BEGIN')
        reader.execute('SELECT revision FROM investigation_tasks').fetchall()
        self.store.save_task('wal-task', 'owner', 'session', 'snapshot',
                             {'text': 'y' * 20000}, {})
        wal_bytes = Path(str(self.path) + '-wal').stat().st_size
        self.assertGreater(wal_bytes, 0)
        files_size = sum(Path(str(self.path) + suffix).stat().st_size
                         for suffix in ('', '-wal', '-shm'))
        self.store.max_bytes = files_size - 1
        with self.assertRaisesRegex(InvestigationStorageError, f'现有文件{files_size}字节'):
            self.store.save_task('blocked', 'owner', 'session', 'snapshot', {}, {})
        self.assertEqual(self.store.load_task('wal-task', 'owner')['payload']['text'], 'y' * 20000)
        self.assertIsNone(self.store.load_task('blocked', 'owner'))


if __name__ == '__main__':
    unittest.main()
