"""Count model dispatches, not just rejected SQL execution attempts.

All model/validator/MCP interactions are controlled doubles. No paid model,
HTTP or business SQL is used. Completed independent results remain available.
"""
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from app.runtime_budget import BudgetStop, RunBudget
from app.services.investigation_store import InvestigationStore
from app.workflows.react_harness import ReactInvestigationHarness
from tests.test_react_round_policy import ControlledHarness, query, state


class SqlGenerationPreDispatchTest(unittest.TestCase):
    def failure(self, *, independent=False, rewrite=None):
        bad = query('BAD1', metric='bad')
        items = [query('GOOD', metric='good'), bad] if independent else [bad]
        action_id = 'A2' if independent else 'A1'
        replacement = rewrite if rewrite is not None else {
            'query_id': action_id, 'sql': 'BAD2', 'query_contract': bad['query_contract']}
        return ControlledHarness(
            normal=[{'action': {'type': 'query', 'queries': items}},
                    {'action': {'type': 'query', 'queries': [query('FORBIDDEN_THIRD')]}}],
            rewrites=[replacement], fail_sql=('BAD1', 'BAD2'))

    def test_two_failed_candidates_never_dispatch_another_react(self):
        fixture = self.failure()
        with fixture.bounded() as budget:
            result = fixture.harness.run(state())
        self.assertEqual([node for node, _ in fixture.calls], ['react', 'sql_repair'])
        self.assertEqual(fixture.validated, ['BAD1', 'BAD2'])
        self.assertEqual(fixture.executed, [])
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(result.status, 'failed')
        self.assertIn('不再调用模型重写', result.stop_reason)
        self.assertTrue(result.execution_log[-1]['stopped_before_model_call'])
        self.assertEqual(result.plan[0]['sql_generations'], 2)

    def test_independent_batch_finishes_and_closes_with_preserved_evidence(self):
        fixture = self.failure(independent=True)
        with fixture.bounded() as budget:
            result = fixture.harness.run(state())
        self.assertEqual([node for node, _ in fixture.calls],
                         ['react', 'sql_repair', 'draft', 'delivery_review'])
        self.assertEqual(fixture.executed, ['GOOD'])
        self.assertEqual([entry['sql'] for entry in result.evidence], ['GOOD'])
        self.assertEqual(result.status, 'partial')
        self.assertEqual(budget.react_rounds, 1)

    def test_later_independent_batch_member_is_not_lost_after_failure(self):
        fixture = self.failure()
        first = query('BAD1', metric='bad')
        fixture.normal = iter([{'action': {'type': 'query', 'queries': [
            first, query('GOOD_LATER', metric='other')]}}])
        with fixture.bounded():
            result = fixture.harness.run(state())
        self.assertEqual(fixture.executed, ['GOOD_LATER'])
        self.assertEqual(result.status, 'partial')
        self.assertEqual([node for node, _ in fixture.calls],
                         ['react', 'sql_repair', 'draft', 'delivery_review'])

    def test_successful_second_candidate_can_continue_investigation(self):
        original = query('BAD1')
        fixture = ControlledHarness(
            normal=[{'action': {'type': 'query', 'queries': [original]}},
                    {'action': {'type': 'query', 'queries': [query('OTHER', metric='other')]}},
                    {'action': {'type': 'deliver'}}],
            rewrites=[{'query_id': 'A1', 'sql': 'FIXED', 'query_contract': original['query_contract']}],
            fail_sql=('BAD1',))
        with fixture.bounded() as budget:
            result = fixture.harness.run(state())
        self.assertEqual([node for node, _ in fixture.calls],
                         ['react', 'sql_repair', 'react', 'react', 'draft', 'delivery_review'])
        self.assertEqual(fixture.executed, ['FIXED', 'OTHER'])
        self.assertEqual([entry['sql_generations'] for entry in result.plan], [2, 1])
        self.assertEqual(budget.react_rounds, 2)

    def test_invalid_or_unknown_rewrite_consumes_last_slot_without_next_react(self):
        for response in ({'action': {'type': 'query'}}, RuntimeError('unknown response')):
            with self.subTest(response=type(response).__name__):
                fixture = self.failure(rewrite=response)
                with fixture.bounded():
                    result = fixture.harness.run(state())
                self.assertEqual([node for node, _ in fixture.calls], ['react', 'sql_repair'])
                self.assertEqual(result.plan[0]['sql_generations'], 2)
                self.assertEqual(result.status, 'failed')

    def test_lowered_limit_one_never_dispatches_rewrite_or_next_react(self):
        fixture = self.failure()
        with fixture.bounded(RunBudget('round-test', sql_generation_limit=1)):
            result = fixture.harness.run(state())
        self.assertEqual([node for node, _ in fixture.calls], ['react'])
        self.assertEqual(result.plan[0]['sql_generations'], 1)

    def test_direct_model_dispatch_is_also_guarded_before_call(self):
        fixture = self.failure()
        fixture.task.actions = [{'id': 'A1', 'action_type': 'query', 'status': 'failed',
                                 'sql_generations': 2, 'sql_generation_limit': 2}]
        model = Mock()
        fixture.harness.model = model
        with fixture.bounded():
            with self.assertRaises(BudgetStop):
                ReactInvestigationHarness._call(fixture.harness, fixture.task, 'react', '', {})
        model.chat_json.assert_not_called()

    def test_restore_and_explicit_continue_do_not_reopen_exhausted_query(self):
        fixture = self.failure()
        fixture.task.actions = [{'id': 'A1', 'action_type': 'query', 'kind': 'query',
                                 'status': 'failed', 'attempts': 2, 'sql_generations': 2,
                                 'sql_generation_limit': 2, 'input_result_ids': [], 'result_ids': []}]
        fixture.task.final_status = 'waiting_clarification'
        fixture.task.clarification = {'parameter': 'resume', 'question': '继续？',
                                      'reason': 'fixture', 'options': []}
        with TemporaryDirectory() as directory:
            store = InvestigationStore(Path(directory) / 'state.sqlite3')
            store.save_task('round-test', 'round-user', 'round-session', 'round_fixture',
                            asdict(fixture.task), RunBudget('round-test', react_rounds=3).export_state())
            fixture.harness.records.clear()
            fixture.harness.store = store
            restored = fixture.harness.restore('round-test', 'round-user')
            budget = fixture.harness.workflow.budgets['round-test']
            with fixture.bounded(budget):
                result = fixture.harness.run(restored.state, '__continue__')
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.executed, [])
        self.assertEqual(budget.react_rounds, 3)
        self.assertEqual(result.status, 'failed')

    def test_old_checkpoint_attempt_count_still_stops_before_model(self):
        fixture = self.failure()
        fixture.task.actions = [{'id': 'old-query', 'action_type': 'query', 'status': 'failed',
                                 'attempts': 2}]
        with fixture.bounded():
            result = fixture.harness.run(state())
        self.assertEqual(fixture.calls, [])
        self.assertEqual(result.status, 'failed')


if __name__ == '__main__':
    unittest.main()
