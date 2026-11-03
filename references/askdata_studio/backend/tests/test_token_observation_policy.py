"""No paid calls: token observation and per-decision request boundaries."""
from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import patch

from app.config import Settings
from app.model_client import ModelClient
from app.runtime_budget import BudgetStop, RunBudget, current_budget


class TokenObservationPolicyTest(unittest.TestCase):
    def test_every_legacy_token_gate_is_inactive_in_both_phases(self):
        budget = RunBudget('observe', model_token_limit=1, retrieval_token_limit=1,
                           max_input_tokens=1, max_output_tokens=1, closing_token_reserve=1,
                           model_tokens={'chat': 999999, 'retrieval': 999999})
        for phase in ['investigation', 'closing']:
            budget.phase = phase
            for kind in ['chat', 'embedding', 'rerank']:
                key = budget.reserve_model(kind, {'messages': ['上下文材料' * 2000]})
                budget.settle_model(key, {'total_tokens': 5000})
        public = budget.model_budget()
        self.assertEqual(public['mode'], 'observe_only')
        self.assertFalse(public['enforced'])
        for key in ['limit', 'retrieval_limit', 'max_input', 'max_output', 'closing_reserve', 'remaining']:
            self.assertIsNone(public[key])
        self.assertEqual(public['used']['chat'], 1009999)
        self.assertEqual(public['used']['retrieval'], 1019999)

    def test_missing_usage_keeps_estimate_separate_from_actual(self):
        budget = RunBudget('observe')
        key = budget.reserve_model('chat', {'messages': ['输入']})
        estimate = budget.model_reservations[key]['input_estimate']
        budget.settle_model(key, None)
        budget.settle_model(key, {'total_tokens': 100})  # Already settled: no duplicate.
        self.assertEqual(budget.model_budget()['used'], {})
        self.assertEqual(budget.model_budget()['estimated_unknown'], {'chat': estimate})
        event = budget.events[-1]
        self.assertFalse(event['actual_usage_known'])
        self.assertFalse(event['output_tokens_known'])

    def test_known_zero_usage_is_not_misclassified_as_unknown(self):
        budget = RunBudget('observe')
        key = budget.reserve_model('embedding', {'input': ['输入']})
        budget.settle_model(key, {'total_tokens': 0})
        self.assertEqual(budget.model_tokens, {'retrieval': 0})
        self.assertEqual(budget.model_estimated_tokens, {})

    def test_legacy_mixed_accounting_is_not_reported_as_all_actual(self):
        payload = {'model_tokens': {'chat': 100, 'retrieval': 30},
                   'model_usage': [{'kind': 'chat', 'node': 'react', 'usage': {'total_tokens': 60}},
                                   {'kind': 'rerank', 'usage': None}],
                   'model_token_limit': 1, 'max_input_tokens': 1}
        restored = RunBudget.from_state('legacy', payload)
        self.assertEqual(restored.model_tokens, {'chat': 60})
        self.assertEqual(restored.model_estimated_tokens, {'chat': 40, 'retrieval': 30})
        self.assertEqual(restored.react_rounds, 1)
        key = restored.reserve_model('chat', {'messages': ['仍可继续发起调用']})
        restored.settle_model(key, {'total_tokens': 5})
        self.assertEqual(restored.model_tokens['chat'], 65)

    def test_unknown_inflight_model_is_not_automatically_replayed(self):
        budget = RunBudget('observe', spent=5)
        budget.reserve_model('chat', {'messages': ['输入']})
        saved = json.loads(json.dumps(budget.export_state()))
        restored = RunBudget.from_state('observe', saved)
        self.assertFalse(restored.model_reservations)
        self.assertGreater(restored.spent, 5)
        self.assertTrue(restored.model_estimated_tokens)
        self.assertFalse(restored.model_tokens)

    def test_legacy_default_reservation_remains_sixty_seconds(self):
        budget = RunBudget('legacy-call', spent=5)
        key = budget.reserve_model('chat', {'messages': ['输入']})
        self.assertEqual(budget.model_reservations[key]['seconds_reserved'], 60)
        restored = RunBudget.from_state('legacy-call', json.loads(json.dumps(budget.export_state())))
        self.assertEqual(restored.spent, 65)
        self.assertFalse(restored.model_reservations)

    def test_five_react_slots_are_reserved_before_calls_and_survive_restore(self):
        budget = RunBudget('rounds', react_round_limit=5)
        saved = []
        budget.checkpoint_callback = lambda: saved.append(budget.export_state())
        for number in range(1, 6):
            budget.react_decision()
            budget.query_round()
            self.assertEqual(saved[-1]['react_rounds'], number)
        with self.assertRaises(BudgetStop):
            budget.react_decision()
        restored = RunBudget.from_state('rounds', json.loads(json.dumps(budget.export_state())))
        self.assertEqual(restored.public()['react_rounds'], 5)
        self.assertEqual(restored.public()['react_round_limit'], 5)
        self.assertEqual(restored.public()['sql_generation_limit'], 3)
        with self.assertRaises(BudgetStop):
            restored.react_decision()

    def test_old_checkpoint_rounds_are_derived_without_counting_sql_repair(self):
        restored = RunBudget.from_state('old', {'model_usage': [
            {'node': 'react'}, {'node': 'sql_repair'}, {'node': 'react'}, {'node': 'delivery_review'}]})
        self.assertEqual(restored.react_rounds, 2)

    def test_time_and_tool_limits_are_still_enforced(self):
        with self.assertRaises(BudgetStop):
            RunBudget('expired', seconds=300, investigation_seconds=240, spent=300).reserve_model('chat', {'messages': ['x']})
        with self.assertRaises(BudgetStop):
            RunBudget('tools', tool_limit=1, tools=1).tool('query')

    def test_react_and_sql_repair_never_implicitly_retry_unknown_response(self):
        for node in ['react', 'sql_repair', 'query_coder']:
            with self.subTest(node=node):
                budget = RunBudget(node, model_node=node)
                token = current_budget.set(budget)
                try:
                    client = ModelClient(Settings(api_key='offline-test-key', max_retries=2))
                    with patch('app.model_client.urllib.request.urlopen',
                               side_effect=urllib.error.URLError('controlled offline failure')) as call:
                        with self.assertRaises(RuntimeError):
                            client.chat('JSON', 'test')
                    self.assertEqual(call.call_count, 1)
                    self.assertEqual(budget.model_calls, {'chat': 1})
                    self.assertEqual(budget.repairs, {})
                    self.assertEqual(len(budget.model_usage), 1)
                    self.assertFalse(budget.model_tokens)
                    self.assertTrue(budget.model_estimated_tokens)
                finally:
                    current_budget.reset(token)


if __name__ == '__main__':
    unittest.main()
