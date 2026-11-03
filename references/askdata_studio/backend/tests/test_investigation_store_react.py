from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from app.services.investigation_store import (
    InvestigationIntegrityError,
    InvestigationRevisionConflict,
    InvestigationStore,
)


def result_payload(rows: int = 1207, *, complete: bool = True) -> dict:
    return {
        "user_id": "user-a", "task_id": "task-a", "datasource_id": "snapshot-a",
        "sql": "SELECT value FROM authorized_data", "columns": ["value"],
        "rows": [{"value": index} for index in range(rows)],
        "complete": complete, "total_rows": rows if complete else None,
        "snapshot": {"snapshot_id": "snapshot-a", "observation_cutoff_at": "2026-08-31"},
        "preview_truncated": rows > 200,
    }


class InvestigationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "runtime" / "state.sqlite3"
        self.store = InvestigationStore(self.path)

    def save_task(self, payload: dict | None = None, budget: dict | None = None, **kwargs) -> None:
        self.store.save_task("task-a", "user-a", "session-a", "snapshot-a",
                             payload or {"status": "waiting_clarification"},
                             budget or {"tools": 7, "repairs": {"A1": 1}, "active_seconds": 91.25},
                             **kwargs)

    def test_checkpoint_and_full_result_survive_new_store_instance(self) -> None:
        rows = result_payload()
        payload = {
            "status": "waiting_clarification", "goal": "解释GMV下降",
            "facts": [{"metric_id": "gmv", "result_id": "R1", "value": 123}],
            "semantics": {"gmv": {"definition": "支付商品金额", "version": "v1"}},
            "reference_now": "2026-10-29T09:00:00+08:00",
        }
        self.save_task(payload, results=[("ref-a", rows)], expected_revision=0)
        restored = InvestigationStore(self.path)
        task = restored.load_task("task-a", "user-a")
        self.assertEqual(task["payload"], payload)
        self.assertEqual(task["budget"], {"tools": 7, "repairs": {"A1": 1}, "active_seconds": 91.25})
        self.assertEqual(task["revision"], 1)
        self.assertEqual(restored.load_result("ref-a", "user-a", "task-a", "snapshot-a"), rows)
        self.assertEqual(len(restored.load_result("ref-a", "user-a", "task-a", "snapshot-a")["rows"]), 1207)

    def test_unknown_and_truncated_totals_remain_unknown(self) -> None:
        payload = result_payload(501, complete=False)
        self.store.save_result("ref-a", payload)
        loaded = self.store.load_result("ref-a", "user-a", "task-a", "snapshot-a")
        self.assertFalse(loaded["complete"])
        self.assertIsNone(loaded["total_rows"])
        self.assertEqual(len(loaded["rows"]), 501)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM investigation_result_chunks").fetchone()[0], 2)

    def test_result_scope_and_immutable_reference(self) -> None:
        payload = result_payload(1)
        self.store.save_result("ref-a", payload)
        self.store.save_result("ref-a", copy.deepcopy(payload))
        for arguments in (("other-user", "task-a", "snapshot-a"),
                          ("user-a", "other-task", "snapshot-a"),
                          ("user-a", "task-a", "other-snapshot")):
            with self.subTest(arguments=arguments), self.assertRaises(PermissionError):
                self.store.load_result("ref-a", *arguments)
        changed = copy.deepcopy(payload)
        changed["rows"][0]["value"] = 999
        with self.assertRaises(InvestigationIntegrityError):
            self.store.save_result("ref-a", changed)
        changed = copy.deepcopy(payload)
        changed["user_id"] = "other-user"
        with self.assertRaises(PermissionError):
            self.store.save_result("ref-a", changed)
        with self.assertRaises(KeyError):
            self.store.load_result("missing", "user-a", "task-a", "snapshot-a")

    def test_task_owner_session_source_and_listing_are_scoped(self) -> None:
        self.save_task()
        self.store.save_task("task-b", "user-b", "session-a", "snapshot-a", {}, {})
        self.store.save_task("task-c", "user-a", "session-c", "snapshot-a", {}, {})
        with self.assertRaises(PermissionError):
            self.store.load_task("task-a", "user-b")
        for values in (("user-b", "session-a", "snapshot-a"),
                       ("user-a", "session-b", "snapshot-a"),
                       ("user-a", "session-a", "snapshot-b")):
            with self.subTest(values=values), self.assertRaises(PermissionError):
                self.store.save_task("task-a", *values, {}, {})
        self.assertEqual({item["task_id"] for item in self.store.list_tasks("user-a")}, {"task-a", "task-c"})
        self.assertEqual([item["task_id"] for item in self.store.list_tasks("user-a", "session-a")], ["task-a"])
        self.assertIsNone(self.store.load_task("missing", "user-a"))

    def test_result_failure_rolls_back_entire_checkpoint(self) -> None:
        self.save_task(expected_revision=0)
        previous = self.store.load_task("task-a", "user-a")
        payload = result_payload(1)
        self.store.save_result("existing", payload)
        changed = copy.deepcopy(payload)
        changed["sql"] = "SELECT a_different_value"
        with self.assertRaises(InvestigationIntegrityError):
            self.save_task({"status": "completed"}, {"tools": 8},
                           results=[("new-result", payload), ("existing", changed)],
                           expected_revision=1)
        self.assertEqual(self.store.load_task("task-a", "user-a"), previous)
        with self.assertRaises(KeyError):
            self.store.load_result("new-result", "user-a", "task-a", "snapshot-a")

    def test_cross_task_result_cannot_be_committed_into_checkpoint(self) -> None:
        payload = result_payload(1)
        payload["task_id"] = "other-task"
        with self.assertRaises(PermissionError):
            self.save_task(results=[("bad-ref", payload)])
        self.assertIsNone(self.store.load_task("task-a", "user-a"))

    def test_two_writers_using_same_revision_cannot_overwrite(self) -> None:
        self.save_task(expected_revision=0)
        barrier = Barrier(2)

        def write(value: int) -> str:
            writer = InvestigationStore(self.path)
            old = writer.load_task("task-a", "user-a")
            barrier.wait(timeout=5)
            try:
                writer.save_task("task-a", "user-a", "session-a", "snapshot-a",
                                 {"value": value}, {"tools": 8}, expected_revision=old["revision"])
                return "saved"
            except InvestigationRevisionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(write, [1, 2]))
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        self.assertEqual(self.store.load_task("task-a", "user-a")["revision"], 2)
        with self.assertRaises(InvestigationRevisionConflict):
            self.save_task(expected_revision=0)

    def test_missing_result_chunk_is_not_returned_as_complete(self) -> None:
        self.store.save_result("ref-a", result_payload())
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM investigation_result_chunks WHERE chunk_index = 1")
        with self.assertRaises(InvestigationIntegrityError):
            self.store.load_result("ref-a", "user-a", "task-a", "snapshot-a")

    def test_preview_cannot_be_persisted_as_complete_and_budget_is_not_stringified(self) -> None:
        payload = result_payload(200)
        payload["total_rows"] = 1207
        with self.assertRaises(ValueError):
            self.store.save_result("ref-a", payload)
        with self.assertRaises(TypeError):
            self.save_task(budget={"repairs": {"unserializable"}})
        self.assertIsNone(self.store.load_task("task-a", "user-a"))


if __name__ == "__main__":
    unittest.main()
