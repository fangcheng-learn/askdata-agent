"""Controlled integration checks for fatal storage and restored cost limits."""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.models import QueryResult
from app.runtime_budget import RunBudget
from app.workflows.query_graph import QueryWorkflow
from tests import test_react_harness as harness_fixtures


class ReactBudgetAuditTest(unittest.TestCase):
    def test_real_mcp_storage_failure_stops_before_another_model_decision(self):
        fixture = harness_fixtures.ReactHarnessTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        harness, model = fixture.harness([
            harness_fixtures.action('load_skill', names=['business_diagnosis']),
            harness_fixtures.action('query', queries=[harness_fixtures.query()]),
            harness_fixtures.action('deliver'),  # This decision must never be requested.
        ])
        harness.workflow.database_engine.result_store = harness.store
        with patch.object(harness.store, 'save_result', side_effect=OSError('controlled disk failure')):
            result, budget = fixture.run_bounded(harness)
        self.assertEqual(len(model.calls), 2, 'storage faults must not be sent back for SQL repair')
        self.assertEqual(result.status, 'failed')
        self.assertFalse(result.final_reviewed)
        self.assertFalse(result.evidence)
        self.assertEqual(budget.tools, 2)  # One skill read and one real local MCP call.
        self.assertIn('持久化', result.stop_reason)
        failed_query = next(item for item in result.plan if item['action_type'] == 'query')
        self.assertEqual(failed_query.get('error_kind'), 'storage')
        self.assertEqual(budget.repairs, {})

    def test_restored_budget_keeps_original_caps_when_current_config_is_larger(self):
        original = RunBudget(
            'resume-task', tools=7, spent=12, model_tokens={'chat': 900, 'retrieval': 100},
            model_token_limit=1000, retrieval_token_limit=500,
            max_input_tokens=300, max_output_tokens=30, closing_token_reserve=100,
            reference_now='2026-10-29T09:00:00+08:00',
        )
        saved = json.loads(json.dumps(original.export_state()))
        restored = RunBudget.from_state('resume-task', saved)
        workflow = object.__new__(QueryWorkflow)
        workflow.budgets = {'resume-task': restored}
        workflow.harness = SimpleNamespace(records={})
        workflow.configuration_snapshots = {}
        workflow.config = SimpleNamespace(
            harness_tool_limit=20, harness_repair_limit=2, harness_replan_limit=1,
            harness_seconds=300, harness_investigation_seconds=240,
            harness_model_tokens=160000, harness_retrieval_tokens=80000,
            harness_input_tokens=48000, harness_output_tokens=4096, harness_closing_tokens=60000,
        )
        result = workflow._invoke_locked('resume-task', lambda: QueryResult(
            task_id='resume-task', status='completed', route='database_query', message='controlled no-call'))
        self.assertEqual(restored.model_token_limit, 1000)
        self.assertEqual(restored.retrieval_token_limit, 500)
        self.assertEqual(restored.max_input_tokens, 300)
        self.assertEqual(restored.max_output_tokens, 30)
        self.assertEqual(restored.closing_token_reserve, 100)
        self.assertIsNone(result.budget['model_budget']['remaining'])
        self.assertEqual(result.budget['model_budget']['mode'], 'observe_only')
        self.assertFalse(result.budget['model_budget']['enforced'])
        self.assertEqual(restored.tools, 7)
        self.assertGreaterEqual(restored.spent, 12)
        self.assertEqual(restored.reference_now, original.reference_now)


if __name__ == '__main__':
    unittest.main()
