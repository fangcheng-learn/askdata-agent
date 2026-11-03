"""Population conflicts must survive a resumed/inherited SQL generation ledger."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from app.homenova import registered_metric
from app.runtime_budget import BudgetStop, RunBudget, current_budget
from tests import test_registered_recipe_dependencies as fixtures


def category_request(*metrics):
    return {'goal': '核实POWER品类会话和成交率', 'execution_mode': 'registered_metrics',
            'metric_requirements': list(metrics or ('sessions_count', 'checkout_session_cvr')),
            'dimensions': ['month'], 'conditions': {'months': ['2026-06', '2026-07'],
                                                  'sku_catalog.category_code': 'POWER'}}


class SessionPopulationGenerationGateTest(unittest.TestCase):
    # Reuse the offline authorized metadata fixture, not its test methods.
    setUp = fixtures.RegisteredRecipeDependencyTest.setUp
    graph = fixtures.RegisteredRecipeDependencyTest.graph
    prepare_route = fixtures.RegisteredRecipeDependencyTest.prepare_route

    def historical_action(self, request):
        for name in ('sessions_count', 'checkout_session_cvr', 'category_view_session_cvr'):
            self.task.metric_pool[name] = deepcopy(registered_metric(fixtures.SOURCE, name))
        self.harness._persist = Mock()
        # Obtain real request identity/key via the controller's binding path,
        # then restore the one-generation historical ledger without SQL/model.
        self.harness._prepare_sql_context = Mock(side_effect=ValueError('fixture bind only'))
        # This fixture represents a saved request from BEFORE population
        # partitioning existed. Only creation bypasses the new dispatcher;
        # resumed execution below exercises the current production path.
        with patch('app.querying.query_scope.partition_registered_request', side_effect=lambda q, _: [q]):
            self.harness._business_query_batch(self.task, {'queries': [request]}, '历史请求')
        self.assertEqual(len(self.task.actions), 1)
        old = self.task.actions[0]
        old.update(status='failed', error='旧SQL未解决统计集合', error_kind='validation',
                   sql_generations=1, attempts=1, model_generations=1)
        self.assertTrue(old['request_identity'])
        self.assertTrue(old['request_key'])
        return old

    def assert_conflict_preserves_history(self, old, *, inherited):
        coder = self.prepare_route()
        # A call is recorded but never sent. Stop after that first wrong branch
        # so a regression exposes ledger inflation without mock retry noise.
        coder.generate_batch.side_effect = RuntimeError('offline: unexpected model SQL fallback')
        budget = RunBudget('T', react_rounds=3, tools=5, spent=29,
                           model_calls={'chat': 7}, repairs={'historical': 1})
        token = current_budget.set(budget)
        try:
            try:
                self.harness._business_query_batch(self.task, {'queries': [category_request()]}, '继续核实')
            except BudgetStop:
                pass  # The forbidden model branch aborts; assertions below fail.
        finally:
            current_budget.reset(token)
        self.assertEqual(len(self.task.actions), 2 if inherited else 1)
        action = self.task.actions[-1]
        if inherited:
            self.assertEqual(action['parent_query_id'], old['id'])
        else:
            self.assertIs(action, old)
            self.assertNotIn('parent_query_id', action)
        outcome = next((r for r in (self.task.observation or {}).get('queries', [])
                        if r.get('action_id') == action['id']), {})
        observed = {'model_batch_calls': coder.generate_batch.call_count,
                    'status': action['status'], 'error_kind': action.get('error_kind'),
                    'sql_generations': action['sql_generations'], 'attempts': action['attempts'],
                    'action_model_generations': action.get('model_generations', 0),
                    'parent_sql_generations': old['sql_generations'],
                    'parent_model_generations': old['model_generations'],
                    'alternative_metric_ids': outcome.get('alternative_metric_ids', [])}
        self.assertEqual(observed, {
            'model_batch_calls': 0, 'status': 'needs_method_decision', 'error_kind': 'metric_scope',
            'sql_generations': 1, 'attempts': 1, 'action_model_generations': 0 if inherited else 1,
            'parent_sql_generations': 1, 'parent_model_generations': 1,
            'alternative_metric_ids': ['category_view_session_cvr']})
        self.harness._query.assert_not_called()
        self.assertEqual(self.retrieval_calls, [])
        self.assertEqual((budget.react_rounds, budget.tools, budget.spent), (4, 5, 29))
        self.assertEqual(budget.model_calls, {'chat': 7})
        self.assertEqual(budget.repairs, {'historical': 1})
        self.assertEqual(budget.sql_generation_limit, 3)
        self.assertEqual(action['business_request']['metric_requirements'],
                         ['sessions_count', 'checkout_session_cvr'])

    def test_existing_one_generation_checks_population_before_old_generation_gate(self):
        old = self.historical_action(category_request())
        self.assert_conflict_preserves_history(old, inherited=False)

    def test_subset_inheriting_one_generation_checks_population_before_old_generation_gate(self):
        old = self.historical_action(category_request(
            'paid_goods_gmv_gross', 'sessions_count', 'checkout_session_cvr'))
        self.assert_conflict_preserves_history(old, inherited=True)


if __name__ == '__main__':
    unittest.main()
