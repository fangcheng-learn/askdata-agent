"""Scoped checkpoint restoration; no model, network, or business-data queries."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.database import SCHEMA, physical_table_name
from app.models import QueryResult
from app.querying.duckdb_engine import DuckDbEngine
from app.runtime_budget import RunBudget
from app.security import AccessController
from app.services.askdata_service import AskDataService
from app.services.investigation_store import InvestigationStore
from app.services.session_context import SessionContext
from app.skills import SkillRegistry
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord

SOURCE = "ecommerce_ops"
OWNER = "demo_current_sales"
SESSION = "restore-session"
SCOPED_SESSION = f"{OWNER}:{SOURCE}:{SESSION}"


class NoCallsModel:
    def chat(self, *args, **kwargs):
        raise AssertionError("Checkpoint reading must not invoke a summary model")

    def chat_json(self, *args, **kwargs):
        raise AssertionError("Checkpoint reading must not invoke a model")


def saved_record(task_id="stored-task", *, status="completed", owner=OWNER):
    scope = AccessController().scoped(OWNER, SOURCE).public()
    scope["user_id"] = owner
    state = {
        "task_id": task_id, "query": "2026年6月GMV为什么变化", "standalone_query": "2026年6月GMV为什么变化",
        "datasource_id": SOURCE, "session_id": f"{owner}:{SOURCE}:{SESSION}",
        "workspace": {"datasource_id": SOURCE}, "access_scope": scope,
        "required_questions": ["解释GMV变化"], "scenario": {},
    }
    record = ReactRecord(state=state, final_status=status,
                         final_reviewed=status == "completed", approved_text="受控单元测试交付")
    if status == "waiting_clarification":
        record.clarification = {"parameter": "period", "question": "请补充比较期间", "reason": "期间影响答案", "options": []}
    return record


class ReactServiceRestoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = InvestigationStore(self.path)

    def service(self):
        service = AskDataService.__new__(AskDataService)
        service.model_client = NoCallsModel()
        service.config = Settings(api_key="", session_archive_enabled=False,
                                  short_term_summary_enabled=True,
                                  harness_state_path=str(self.path))
        service.access_controller = AccessController()
        service.context = SessionContext(service.model_client, service.config)
        service.tasks = service.context.tasks
        service.session_tasks = service.context.session_tasks
        service.workflow = SimpleNamespace(
            investigation_store=InvestigationStore(self.path), budgets={},
            model_client=service.model_client, database_engine=DuckDbEngine(), skills=SkillRegistry(),
        )
        service.workflow.harness = ReactInvestigationHarness(service.workflow, service.workflow.investigation_store)
        return service

    def persist(self, record, *, results=None):
        budget = RunBudget(record.state["task_id"], tools=5, spent=19,
                           repairs={"A2": 1}, model_calls={"chat": 2})
        self.store.save_task(record.state["task_id"], record.state["access_scope"]["user_id"],
                             record.state["session_id"], SOURCE, asdict(record), budget.export_state(),
                             results=results)

    def test_get_restores_owner_scope_session_and_budget_without_model(self):
        record = saved_record()
        table = next(t for t in SCHEMA if t.get("database") == SOURCE)
        name = physical_table_name(table)
        rows = [{"n": i} for i in range(450)]
        result = {"user_id": OWNER, "task_id": "stored-task", "datasource_id": SOURCE,
                  "sql": "unit-test-placeholder", "columns": ["n"], "rows": rows,
                  "complete": True, "total_rows": len(rows)}
        record.evidence = [{"id": "stored-task:R1", "step_id": "A1", "goal": "受控结果",
                            "user_id": OWNER, "datasource_id": SOURCE, "result_ref": "stored-ref",
                            "columns": ["n"], "rows": rows[:5], "complete": True,
                            "total_rows": 450, "display_truncated": True, "check_status": "reviewed",
                            "query_contract": {"tables": [name]}}]
        self.persist(record, results=[("stored-ref", result)])
        service = self.service()
        with patch.object(service.context.short_term_memory, "maybe_schedule", side_effect=AssertionError("No summary")):
            restored = service.get_task("stored-task", OWNER)
        self.assertEqual(restored.status, "completed")
        self.assertTrue(restored.final_reviewed)
        self.assertEqual(restored.session_id, SESSION)
        self.assertEqual(restored.budget["tools"], 5)
        self.assertEqual(restored.budget["repairs"], {"A2": 1})
        self.assertEqual(restored.budget["active_seconds"], 19)
        self.assertEqual(service.tasks["stored-task"]["session_id"], SCOPED_SESSION)
        self.assertIsNone(service.workflow.budgets["stored-task"].started)
        self.assertEqual(service.workflow.investigation_store.load_result("stored-ref", OWNER, "stored-task", SOURCE)["rows"], rows)

    def test_wrong_owner_cannot_read_even_with_same_datasource_permission(self):
        self.persist(saved_record())
        with self.assertRaises(PermissionError):
            self.service().get_task("stored-task", "demo_admin")

    def test_current_datasource_permission_is_checked_before_restoration(self):
        self.persist(saved_record(owner="demo_analyst"))
        with self.assertRaises(PermissionError):
            self.service().get_task("stored-task", "demo_analyst")

    def test_running_checkpoint_is_visible_but_not_automatically_replayed(self):
        record = saved_record(status="")
        record.actions = [{"id": "A1", "kind": "query", "action_type": "query", "goal": "受控查询",
                           "status": "running", "depends_on": [], "input_result_ids": [], "result_ids": []}]
        self.persist(record)
        service = self.service()
        result = service.get_task("stored-task", OWNER)
        self.assertEqual(result.status, "waiting_clarification")
        self.assertEqual(result.clarification.parameter, "resume")
        self.assertEqual(result.plan[0]["status"], "interrupted")
        self.assertEqual(result.budget["tools"], 5)
        self.assertEqual(self.store.load_task("stored-task", OWNER)["payload"]["actions"][0]["status"], "running")
        self.assertFalse(AskDataService._parameter_reply("请重新调查7月GMV下降的原因", result))

    def test_submit_parameter_restores_same_task_and_remaining_budget(self):
        self.persist(saved_record(status="waiting_clarification"))
        service = self.service()
        calls = []
        def resume(task_id, answer):
            calls.append((task_id, answer))
            return QueryResult(task_id=task_id, status="completed", route="database_query", message="受控恢复完成",
                               workflow_mode="investigation_react_v1", final_reviewed=True)
        service.workflow.resume_harness = resume
        service.workflow.invoke = Mock(side_effect=AssertionError("Must not reroute a parameter reply"))
        with patch.object(service.context.short_term_memory, "maybe_schedule", side_effect=AssertionError("No summary")):
            result = service.submit("2026年6月和5月", SESSION, {"datasource_id": SOURCE}, OWNER)
        self.assertEqual(calls, [("stored-task", "2026年6月和5月")])
        self.assertEqual(result.task_id, "stored-task")
        self.assertEqual(result.budget["tools"], 5)

    def test_complete_new_question_is_not_absorbed_as_old_parameter(self):
        self.persist(saved_record(status="waiting_clarification"))
        service = self.service()
        states = []
        def invoke(payload, task_id):
            states.append(payload)
            return QueryResult(task_id=task_id, status="completed", route="direct_response", message="受控新目标",
                               workflow_mode="direct_response")
        service.workflow.invoke = invoke
        service.workflow.resume_harness = Mock(side_effect=AssertionError("Must not resume old task"))
        with patch.object(service.context.short_term_memory, "maybe_schedule"):
            result = service.submit("2026年7月比6月GMV下降的原因是什么", SESSION, {"datasource_id": SOURCE}, OWNER)
        self.assertNotEqual(result.task_id, "stored-task")
        self.assertEqual(states[0]["query"], "2026年7月比6月GMV下降的原因是什么")
        self.assertEqual(service.tasks["stored-task"]["result"].status, "waiting_clarification")

    def test_completed_same_session_followup_receives_authorized_saved_results(self):
        record = saved_record()
        record.evidence = [{"id": "stored-task:R1", "step_id": "A1", "goal": "测试聚合",
                            "user_id": OWNER, "datasource_id": SOURCE, "check_status": "reviewed",
                            "columns": ["amount"], "rows": [{"amount": 10}],
                            "total_rows": 1, "complete": True, "display_truncated": False}]
        self.persist(record)
        service = self.service()
        states = []
        def invoke(payload, task_id):
            states.append(payload)
            return QueryResult(task_id=task_id, status="completed", route="data_qa", message="受控追问",
                               workflow_mode="qa")
        service.workflow.invoke = invoke
        with patch.object(service.context.short_term_memory, "maybe_schedule"):
            service.submit("解释一下刚才的变化", SESSION, {"datasource_id": SOURCE}, OWNER)
        self.assertIn('stored-task', states[0]['recent_result_context'])
        self.assertIn('amount', states[0]['recent_result_context'])
        self.assertEqual(states[0]['reusable_evidence'][0]['id'], 'stored-task:R1')
        self.assertEqual(states[0]['session_id'], SCOPED_SESSION)

    def test_continue_is_explicit_and_does_not_become_a_business_parameter(self):
        record = saved_record(status="")
        self.persist(record)
        service = self.service()
        service.get_task("stored-task", OWNER)
        calls = []
        def resume(task_id, answer):
            calls.append((task_id, answer))
            return QueryResult(task_id=task_id, status="completed", route="database_query", message="受控继续",
                               workflow_mode="investigation_react_v1", final_reviewed=True)
        service.workflow.resume_harness = resume
        service.workflow.invoke = Mock(side_effect=AssertionError("Explicit continue must retain task"))
        with patch.object(service.context.short_term_memory, "maybe_schedule"):
            result = service.submit("继续调查", SESSION, {"datasource_id": SOURCE}, OWNER)
        self.assertEqual(calls, [("stored-task", "__continue__")])
        self.assertEqual(result.budget['tools'], 5)

    def test_react_remember_does_not_add_an_extra_summary_model(self):
        service = self.service()
        result = QueryResult(task_id="react", status="completed", route="database_query", message="完成",
                             workflow_mode="investigation_react_v1")
        with patch.object(service.context.short_term_memory, "maybe_schedule") as schedule:
            service.context.remember(SCOPED_SESSION, result, "问题", {"datasource_id": SOURCE}, OWNER)
        schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
