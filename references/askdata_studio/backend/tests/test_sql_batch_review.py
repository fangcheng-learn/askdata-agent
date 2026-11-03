"""Regression cases independently found during the SQL controller review."""
import unittest

from app.workflows.react_harness import ReactRecord
from tests import test_sql_batch_controller as base
from tests.test_sql_batch_controller import business, generated
from tests.test_react_harness import action, review


class SqlBatchReviewTest(unittest.TestCase):
    setUp = base.SqlBatchControllerTest.setUp
    harness = base.SqlBatchControllerTest.harness
    run_bounded = base.SqlBatchControllerTest.run_bounded
    state = staticmethod(base.SqlBatchControllerTest.state)
    new_run = base.SqlBatchControllerTest.new_run
    def test_duplicate_batch_members_reserve_one_attempt_and_one_query_id(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']),
            business(('核实GMV', 'gmv'), ('检查一下同一GMV', 'gmv')),
            generated(('A2', None)), action('deliver'), {'draft': '已核实'}, review(),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        queries = [a for a in result.plan if a.get('action_type') == 'query']
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]['sql_generations'], 1)
        self.assertEqual(len(model.calls[2][1]['queries']), 1)
        self.assertEqual(budget.react_rounds, 1)

    def test_explicit_time_on_failed_query_does_not_reopen_generation_allowance(self):
        from app.workflows.sql_batch import _key
        h, model, state = self.new_run([])
        task = ReactRecord(state, protocol_version=4)
        request = {'goal': '核实GMV', 'metric_requirements': ['gmv'], 'dimensions': [], 'input_result_ids': []}
        identity = {'source': state['datasource_id'], 'snapshot': state.get('scenario'),
                    'conditions': {}, 'understanding': {},
                    'terms': {'metric_requirements': ['gmv'], 'dimensions': []},
                    'inputs': [], 'query_conditions': {}}
        task.actions = [{'id': 'A1', 'action_type': 'query', 'goal': request['goal'],
                         'business_request': request, 'request_identity': identity,
                         'request_key': _key(identity), 'status': 'failed', 'sql_generations': 3,
                         'sql_generation_limit': 3, 'attempts': 3}]
        h._business_query_batch(task, {'queries': [{**request, 'conditions': {
            'time_range': {'start': '2026-06-01', 'end': '2026-08-01'}}}]}, '补充时间')
        self.assertEqual(len(task.actions), 1)
        self.assertTrue(task.actions[0]['generation_closed'])
        self.assertEqual(task.actions[0]['sql_generations'], 3)
        self.assertEqual(model.calls, [])
        self.assertEqual(task.observation['type'], 'query_exhausted')

    def test_genuinely_changed_explicit_period_or_object_is_not_a_refinement(self):
        from app.workflows.sql_batch import is_time_condition_refinement
        old = {'conditions': {'time_range': {'start': '2026-06-01', 'end': '2026-07-01'}}}
        new = {'conditions': {'time_range': {'start': '2026-07-01', 'end': '2026-08-01'}}}
        self.assertFalse(is_time_condition_refinement(old, new))
        self.assertFalse(is_time_condition_refinement({}, {'conditions': {
            'time_range': {'start': '2026-07-01'}, 'store_id': 'another'}}))
        self.assertFalse(is_time_condition_refinement({}, {**new, 'comparison_purpose': '历史同期对照'}))

    def test_missing_dimension_retrieves_even_when_metric_is_cached(self):
        h, _, state = self.new_run([])
        task = ReactRecord(state, protocol_version=2)
        h._inherit_semantics(task, state)
        task.metric_pool['cached_gmv'] = {'id': 'cached_gmv', 'label': 'GMV', 'definition': '测试登记定义'}
        calls = []
        original = h.workflow._retrieve_schema

        def track(retrieval_state, charge_budget=True):
            calls.append(retrieval_state)
            return original(retrieval_state, charge_budget=charge_budget)

        h.workflow._retrieve_schema = track
        h._prepare_sql_context(task, [{'goal': '按新维度检查GMV', 'metric_requirements': ['GMV'],
                                      'dimensions': ['尚未缓存的业务维度'], 'input_result_ids': []}])
        self.assertEqual(len(calls), 1, 'A cached metric must not bypass required field retrieval')
        self.assertIn('尚未缓存的业务维度', calls[0]['standalone_query'])
        self.assertIn('cached_gmv', task.metric_pool, 'New retrieval does not delete cached definitions')


if __name__ == '__main__':
    unittest.main()
