"""Controller/recipe/calculator handoff on a synthetic, independent ledger.

The SQL execution boundary uses in-memory DuckDB; no network or business
snapshot is touched. Persistence, calculation and evidence reuse are real.
"""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.querying.task_measurements import covered_result_ids, canonical_name
from tests import test_react_calculate_contract as calculation_fixture
from tests import test_compiled_inventory as inventory_fixture


class RegisteredInventoryHandoffTest(unittest.TestCase):
    def setUp(self):
        self.f = calculation_fixture.ReactCalculateContractTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.inventory = inventory_fixture.CompiledInventoryTest()
        self.inventory.setUp()
        self.addCleanup(self.inventory.tearDown)
        self.h, self.t, self.b = self.f.harness, self.f.task, self.f.budget
        self.t.protocol_version = 4
        self.t.state.update(datasource_id='homenova_20260831', scenario=inventory_fixture.context()['scenario'])
        self.t.table_pool = {t['id']: t for t in self.inventory.graph['tables']}
        table_ids = {t['name']: t['id'] for t in self.inventory.graph['tables']}
        self.t.field_pool = {f['table_name'] + '.' + f['name']: {
            **f, 'table_id': table_ids[f['table_name']], 'sql_name': f['table_name'] + '.' + f['name']}
            for f in self.inventory.graph['fields']}
        self.t.state['access_scope'].update(allowed_databases=['homenova_20260831'], allowed_tables=['im', 's'])
        self.q = inventory_fixture.request(goal='核实月度库存状态时长', execution_mode='registered_metrics',
            input_result_ids=[], conditions={'months': ['2026-06', '2026-07'],
                                            'sku_catalog.category_code': 'GROUP'})
        self.module = SimpleNamespace(generate_batch=Mock(side_effect=AssertionError('free SQL must not run')))
        self.h._sql_module = Mock(return_value=self.module)
        self.h._prepare_sql_context = Mock(side_effect=lambda task, queries: {
            **inventory_fixture.context(), 'normalized_requests': deepcopy(queries)})
        self.h._query = Mock(side_effect=self.execute_candidate)

    def execute_candidate(self, task, candidate, reason, **kwargs):
        action = next(a for a in task.actions if a['id'] == candidate['repair_action_id'])
        cursor = self.inventory.db.execute(candidate['sql'])
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, [value.isoformat() if hasattr(value, 'isoformat') else value
                                   for value in row])) for row in cursor.fetchall()]
        full = {'user_id': 'U', 'task_id': 'T', 'datasource_id': 'homenova_20260831',
                'columns': columns, 'rows': rows, 'complete': True, 'total_rows': len(rows),
                'sql': candidate['sql'], 'result_ref': 'inventory-fixture-' + action['id']}
        saved = self.h._save_evidence(task, action, full, full)
        action['status'] = 'completed'
        return {'status': 'completed', 'result': self.h._evidence_context(task, saved, 30)}

    def test_completed_operator_result_is_reused_without_retrieval_sql_or_calculation(self):
        typed = {**self.q, 'conditions': {'months': ['2026-06', '2026-07'], 'filters': [
            {'field': 'sku_catalog.category_code', 'operator': 'in', 'values': ['GROUP']}]}}
        self.h._business_query_batch(self.t, {'queries': [typed]}, '核实库存')
        observation = self.t.observation['queries'][0]
        self.assertTrue(observation['calculation_complete'])
        output = observation['result']
        rows = {(r['warehouse_id'], r['period']): r for r in output['rows']}
        self.assertEqual(rows['A', '2026-06']['duration_hours'], 12)
        self.assertEqual(rows['A', '2026-07']['duration_hours'], 36)
        self.assertEqual(rows['B', '2026-07']['duration_hours'], 0)
        self.assertEqual(output['column_metadata']['duration_hours']['measurement_id'], 'stockout_duration')
        before = (self.b.react_rounds, self.b.tools, self.h._query.call_count,
                  self.h._prepare_sql_context.call_count)
        subset = {**self.q, 'goal': '只取已核实的缺货时长', 'metric_requirements': ['stockout_duration']}
        self.h._business_query_batch(self.t, {'queries': [subset]}, '复用已核实指标')
        self.assertEqual(self.t.observation['queries'][0]['status'], 'reused')
        self.assertEqual(self.t.observation['queries'][0]['result_ids'], [output['id']])
        self.assertEqual((self.b.react_rounds, self.b.tools, self.h._query.call_count,
                         self.h._prepare_sql_context.call_count), before)
        self.module.generate_batch.assert_not_called()

    def test_unqualified_sku_uses_authoritative_metadata_for_reuse_not_fk_guessing(self):
        self.h._business_query_batch(self.t, {'queries': [self.q]}, '先取得规范SKU结果')
        output_id = self.t.observation['queries'][0]['result']['id']
        self.assertEqual(canonical_name(self.t, 'sku_id'), 'sku_catalog.sku_id')
        self.assertEqual(canonical_name(self.t, 'inventory_movements.sku_id'), 'inventory_movements.sku_id')
        query = {**self.q, 'dimensions': ['month', 'sku_id'], 'metric_requirements': ['stockout_duration']}
        self.h._prepare_sql_context.reset_mock()
        sql_calls = self.h._query.call_count
        self.h._business_query_batch(self.t, {'queries': [query]}, '同一SKU语义复用')
        self.assertEqual(self.t.observation['queries'][0]['result_ids'], [output_id])
        self.h._prepare_sql_context.assert_not_called()
        self.assertEqual(self.h._query.call_count, sql_calls)

    def test_mixed_inventory_keeps_valid_operator_when_session_dependencies_fail(self):
        query = {**self.q, 'execution_mode': 'exploratory',
                 'metric_requirements': ['stockout_duration', 'sku_view_session_cvr']}
        self.h._retrieve = Mock(side_effect=RuntimeError('session metadata service unavailable'))
        self.h._business_query_batch(self.t, {'queries': [query]}, '多个不同统计集合')
        outcomes = self.t.observation['queries']
        self.assertEqual([r['status'] for r in outcomes], ['blocked', 'completed'])
        self.assertTrue(outcomes[1]['calculation_complete'])
        self.assertEqual(self.h._query.call_count, 1)
        rows = {(r['warehouse_id'], r['period']): r for r in outcomes[1]['result']['rows']}
        self.assertEqual(rows['A', '2026-07']['duration_hours'], 36)
        self.module.generate_batch.assert_not_called()

    def test_category_filter_cannot_silently_change_registered_session_population(self):
        from tests.test_compiled_sessions import context
        self.h._prepare_sql_context = Mock(side_effect=lambda task, queries: {
            **context(), 'normalized_requests': deepcopy(queries)})
        query = {**self.q, 'goal': '核实品类会话转化',
                 'metric_requirements': ['sessions_count', 'checkout_session_cvr'],
                 'dimensions': ['month']}
        self.h._business_query_batch(self.t, {'queries': [query]}, '核实同一统计集合')
        outcome = self.t.observation['queries'][0]
        self.assertEqual(outcome['status'], 'needs_method_decision')
        self.assertEqual(outcome['sql_generations'], 0)
        self.assertIn('category_view_session_cvr', outcome['alternative_metric_ids'])
        self.assertEqual(self.t.actions[-1]['error_kind'], 'metric_scope')
        self.h._query.assert_not_called()
        self.module.generate_batch.assert_not_called()

    def test_malformed_conditions_return_protocol_feedback_before_sql_reservation(self):
        query = {**self.q, 'conditions': {'months': ['2026-06'], "months': ['2026-06']": None}}
        self.h._business_query_batch(self.t, {'queries': [query]}, '畸形协议')
        self.assertEqual(self.t.observation['type'], 'protocol_error')
        self.assertIn('畸形', self.t.observation['error'])
        self.assertFalse(self.t.actions)
        self.assertEqual(self.b.react_rounds, 0)
        self.h._prepare_sql_context.assert_not_called()
        self.module.generate_batch.assert_not_called()

    def test_incomplete_calculation_is_not_registered_as_complete_reusable_measurement(self):
        query = {**self.q, 'conditions': {'months': ['2026-06', '2026-07'],
                                        'sku_catalog.category_code': 'NO_SUCH_CATEGORY'}}
        self.h._business_query_batch(self.t, {'queries': [query]}, '没有台账的对象')
        result = self.t.observation['queries'][0]
        self.assertFalse(result['calculation_complete'])
        self.assertNotEqual(self.t.evidence[0]['pending_calculation']['status'], 'completed')
        source_action = next(a for a in self.t.actions if a['action_type'] == 'query')
        self.assertEqual(covered_result_ids(self.t, source_action, query,
                         lambda value: canonical_name(self.t, value)), [])
        self.assertFalse(any(card.get('kind') == 'registered' for card in
            self.t.evidence[-1].get('column_metadata', {}).values()))
        calls = self.h._query.call_count
        self.h._business_query_batch(self.t, {'queries': [query]}, '不能把空台账当零时长')
        self.assertEqual(self.t.observation['queries'][0]['status'], 'needs_calculation_repair')
        self.assertEqual(self.h._query.call_count, calls)

    def test_another_period_calculated_from_same_raw_source_cannot_satisfy_original_query(self):
        self.h._business_query_batch(self.t, {'queries': [self.q]}, '原始两个月查询')
        source = self.t.evidence[0]
        original = next(a for a in self.t.actions if a['action_type'] == 'query')
        # Model a previous postprocess that needs repair, while its raw source
        # is valid. An independent calculation over that source is not a repair
        # of the original two-month question merely because the input matches.
        self.t.evidence[-1]['check_status'] = 'superseded'
        original['status'] = 'awaiting_calculation'
        source['pending_calculation'] = {'status': 'failed', 'operation': 'state_duration'}
        plan = deepcopy(original['candidates'][0]['postprocess'])
        plan.update(result_id=source['id'], periods=plan['periods'][:1])
        self.h._calculate_action(self.t, plan, '另行仅计算六月')
        other_result = self.t.evidence[-1]['id']
        self.assertTrue(self.t.evidence[-1]['calculation_complete'])
        self.h._business_query_batch(self.t, {'queries': [self.q]}, '恢复原两个月目标')
        result = self.t.observation['queries'][0]
        self.assertEqual(result['status'], 'needs_calculation_repair')
        self.assertNotIn(other_result, result.get('result_ids', []))

    def test_prior_inventory_attempt_cannot_bypass_controlled_operator_on_repair(self):
        action = self.h._new_action(self.t, 'query', self.q['goal'], [])
        action.update(status='failed', business_request=deepcopy(self.q), sql_generations=1,
                      attempts=1, sql_generation_limit=3, error='prior inventory attempt failed')
        self.h._business_query_batch(self.t, {'queries': [self.q]}, '修复库存方法', repair_action=action)
        self.module.generate_batch.assert_not_called()
        self.assertEqual(action['sql_generations'], 2)


if __name__ == '__main__':
    unittest.main()
