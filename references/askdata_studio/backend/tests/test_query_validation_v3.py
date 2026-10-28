"""Compatibility and hard-boundary checks without any model or Agent calls."""
from __future__ import annotations

import copy
import unittest

from app import homenova
from app.database import SCHEMA, physical_table_name
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.runtime_budget import RunBudget, current_budget
from app.security import AccessController, AccessScope
from app.task_decisions import validate_step_decision
import test_latest_harness as latest


class QueryValidationV3Test(unittest.TestCase):
    def setUp(self):
        self.engine = DuckDbEngine()
        self.validator = QueryContractValidator(self.engine)
        self.scope = AccessController().resolve('demo_analyst').public()
        self.graph = {'tables': [{**table, 'name': physical_table_name(table)} for table in SCHEMA
                                 if table.get('database') == 'askdata_mock'
                                 and table['id'] == 'orders_current']}

    def contract(self):
        return dict(original_question='查询销售额', data_source='askdata_mock', tables=['orders_current'],
                    metrics=[dict(name='销售额', source_fields=['orders_current.paid_amount'], aggregation='sum')],
                    dimensions=[], filters=[], time_range=None, grain='overall', assumptions=[], open_questions=[])

    def validate(self, contract, sql, *, database='askdata_mock', graph=None, scope=None):
        return self.validator.validate(contract, sql, contract['original_question'], database,
                                       graph or self.graph, scope if scope is not None else self.scope)

    @staticmethod
    def pending_codes(result):
        return {item['code'] for item in result['validation']['pending_checks']}

    def home_contract(self, metric_id='paid_goods_gmv_gross'):
        source = 'homenova_20260831'
        fields = (['payments.order_id'] if metric_id == 'paid_order_count'
                  else ['order_items.line_paid_gross_cents'])
        tables = ['payments'] if metric_id == 'paid_order_count' else ['payments', 'order_items']
        contract = dict(original_question='核对成功支付商品指标', data_source=source, tables=tables,
                        metrics=[dict(name=metric_id, source_fields=fields,
                                      aggregation='count_distinct' if metric_id == 'paid_order_count' else 'sum')],
                        dimensions=[], filters=[dict(field='payments.status', operator='=', value='captured')],
                        time_range=None, grain='overall', assumptions=[], open_questions=[])
        graph = {'tables': [table for table in homenova.schema_payload(source)['tables'] if table['name'] in tables]}
        scope = AccessController().scoped('demo_admin', source).public()
        return contract, graph, scope

    @staticmethod
    def home_gmv_sql():
        return """SELECT SUM(oi.line_paid_gross_cents) AS gmv
                  FROM payments p JOIN order_items oi ON p.order_id=oi.order_id
                  WHERE p.status='captured'"""

    def test_physical_definition_is_filled_without_model_repeating_schema_text(self):
        contract = self.contract()
        result = self.validate(contract, 'SELECT SUM(paid_amount) AS sales FROM orders_current')
        field = next(field for field in self.graph['tables'][0]['fields'] if field['name'] == 'paid_amount')
        self.assertEqual(result['metrics'][0]['definition'], field['description'])
        self.assertTrue(result['validation']['normalizations'])
        self.assertIsInstance(result['validation']['sql_summary'], dict)
        self.assertTrue(result['validation']['sql_summary'])
        self.assertNotIn('definition', contract['metrics'][0], 'normalization must not rewrite the original model audit')

    def test_registered_definition_is_bound_from_public_metric_even_if_omitted_or_reworded(self):
        for definition in [None, '成功付款后商品金额合计，排除运费']:
            contract, graph, scope = self.home_contract()
            if definition is not None:
                contract['metrics'][0]['definition'] = definition
            with self.subTest(definition=definition):
                result = self.validate(contract, self.home_gmv_sql(), database=contract['data_source'], graph=graph, scope=scope)
                canonical = homenova.registered_metric(contract['data_source'], 'paid_goods_gmv_gross')
                self.assertEqual(result['metrics'][0]['definition'], canonical['definition'])
                self.assertFalse(result['validation']['business_definition_verified'])

    def test_technical_or_chinese_display_names_do_not_invent_a_new_business_definition(self):
        for label in ['sales_total', '本次实付合计']:
            contract = self.contract()
            contract['metrics'][0].update(name=label, definition='换一种说法描述实付金额')
            with self.subTest(label=label):
                result = self.validate(contract, 'WITH x AS (SELECT paid_amount AS v FROM orders_current) '
                                               'SELECT SUM(v) AS another_label FROM x')
                self.assertEqual(result['metrics'][0]['name'], 'paid_amount')
                self.assertNotEqual(result['metrics'][0]['definition'], '换一种说法描述实付金额')
                self.assertFalse(result['validation']['business_definition_verified'])

    def test_equivalent_filter_operators_are_normalized_before_contract_validation(self):
        for operator, canonical, field, value, predicate in [
            (' <> ', '!=', 'status', '已取消', "status <> '已取消'"),
            ('IN', 'in', 'region', ['华东'], "region IN ('华东')"),
            ('LIKE', 'like', 'region', '华%', "region LIKE '华%'"),
        ]:
            contract = self.contract()
            contract['filters'] = [dict(field='orders_current.' + field, operator=operator, value=value)]
            with self.subTest(operator=operator):
                result = self.validate(contract, 'SELECT SUM(paid_amount) AS total FROM orders_current WHERE ' + predicate)
                self.assertEqual(result['filters'][0]['operator'], canonical)
                self.assertTrue(result['validation']['normalizations'])

    def test_additional_sql_filter_is_visible_pending_check_instead_of_generation_retry(self):
        contract = self.contract()
        sql = ('WITH totals AS (SELECT SUM(paid_amount) AS amount FROM orders_current) '
               'SELECT amount AS total FROM totals WHERE amount > 0')
        result = self.validate(contract, sql)
        self.assertIn('undeclared_sql_predicate', self.pending_codes(result))
        self.assertFalse(result['validation']['business_definition_verified'])
        execution = self.engine.execute('askdata_mock', sql, AccessScope.from_dict(self.scope))
        self.assertTrue(execution.success, execution.error)
        self.assertTrue(execution.complete)

    def test_pre_deduplicated_order_count_is_executable_without_forcing_count_distinct_syntax(self):
        contract, graph, scope = self.home_contract('paid_order_count')
        sql = ("WITH paid AS (SELECT order_id FROM payments WHERE status='captured' GROUP BY order_id) "
               'SELECT SUM(1) AS n FROM paid')
        result = self.validate(contract, sql, database=contract['data_source'], graph=graph, scope=scope)
        self.assertIn('distinct_count_not_proven', self.pending_codes(result))
        self.assertFalse(result['validation']['business_definition_verified'])
        resolved = AccessScope.from_dict(scope)
        execution = self.engine.execute(contract['data_source'], sql, resolved)
        direct = self.engine.execute(contract['data_source'], "SELECT COUNT(DISTINCT order_id) AS n FROM payments WHERE status='captured'", resolved)
        self.assertTrue(execution.success, execution.error)
        self.assertTrue(direct.success, direct.error)
        self.assertEqual(execution.rows, direct.rows)

    def test_quantity_cannot_impersonate_registered_money_metric(self):
        contract, graph, scope = self.home_contract()
        contract['metrics'][0]['source_fields'] = ['order_items.quantity']
        sql = self.home_gmv_sql().replace('oi.line_paid_gross_cents', 'oi.quantity')
        with self.assertRaises(ValueError):
            self.validate(contract, sql, database=contract['data_source'], graph=graph, scope=scope)

    def test_unrelated_money_column_cannot_hide_quantity_aggregation_for_gmv(self):
        contract, graph, scope = self.home_contract()
        contract['metrics'][0]['source_fields'] = ['order_items.quantity', 'order_items.line_paid_gross_cents']
        sql = self.home_gmv_sql().replace('SUM(oi.line_paid_gross_cents) AS gmv',
                                         'SUM(oi.quantity) AS gmv, MAX(oi.line_paid_gross_cents) AS unrelated_amount')
        with self.assertRaises(ValueError):
            self.validate(contract, sql, database=contract['data_source'], graph=graph, scope=scope)

    def test_money_only_in_case_condition_cannot_be_claimed_as_summed_amount(self):
        contract, graph, scope = self.home_contract()
        contract['metrics'][0]['source_fields'] = ['order_items.quantity', 'order_items.line_paid_gross_cents']
        direct = self.home_gmv_sql().replace('SUM(oi.line_paid_gross_cents)',
                    'SUM(CASE WHEN oi.line_paid_gross_cents > 0 THEN oi.quantity ELSE 0 END)')
        renamed = ("WITH values_to_add AS (SELECT CASE WHEN oi.line_paid_gross_cents > 0 "
                   "THEN oi.quantity ELSE 0 END AS v FROM payments p JOIN order_items oi "
                   "ON p.order_id=oi.order_id WHERE p.status='captured') "
                   'SELECT SUM(v) AS gmv FROM values_to_add')
        for sql in [direct, renamed]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.validate(contract, sql, database=contract['data_source'], graph=graph, scope=scope)

    def test_case_insensitive_identifiers_normalize_without_changing_table_permissions(self):
        contract = self.contract()
        contract['tables'] = ['ORDERS_CURRENT']
        contract['metrics'][0]['source_fields'] = ['ORDERS_CURRENT.PAID_AMOUNT']
        contract['dimensions'] = ['ORDERS_CURRENT.REGION']
        contract['grain'] = 'dimension'
        contract['filters'] = [dict(field='ORDERS_CURRENT.STATUS', operator=' IN ', value=['已支付'])]
        contract['time_range'] = dict(field='ORDERS_CURRENT.ORDER_DATE', start='2026-08-01', end='2026-09-01')
        sql = ("SELECT REGION AS region, SUM(PAID_AMOUNT) AS sales FROM ORDERS_CURRENT "
               "WHERE STATUS IN ('已支付') AND ORDER_DATE>='2026-08-01' AND ORDER_DATE<'2026-09-01' GROUP BY REGION")
        result = self.validate(contract, sql)
        self.assertEqual(result['tables'], ['orders_current'])
        self.assertEqual(result['metrics'][0]['source_fields'], ['orders_current.paid_amount'])
        self.assertEqual(result['dimensions'], ['orders_current.region'])
        self.assertEqual(result['filters'][0]['field'], 'orders_current.status')
        self.assertEqual(result['time_range']['field'], 'orders_current.order_date')
        self.assertTrue(result['validation']['normalizations'])
        denied = copy.deepcopy(self.scope); denied['allowed_tables'] = []
        with self.assertRaises(ValueError):
            self.validate(contract, sql, scope=denied)

    def test_wrong_aggregation_and_missing_actual_measure_still_block(self):
        for sql in ['SELECT AVG(paid_amount) AS total FROM orders_current',
                    'SELECT SUM(order_amount) AS total FROM orders_current']:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.validate(self.contract(), sql)

    def test_declared_business_filter_missing_from_sql_still_blocks(self):
        contract, graph, scope = self.home_contract()
        for sql in [self.home_gmv_sql().replace("WHERE p.status='captured'", ''),
                    self.home_gmv_sql().replace("p.status='captured'", "p.status='failed'")]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.validate(contract, sql, database=contract['data_source'], graph=graph, scope=scope)

    def test_equivalent_timezone_boundary_passes_but_actual_one_hour_shift_blocks(self):
        contract, graph, scope = self.home_contract()
        contract['time_range'] = dict(field='payments.captured_at', start='2026-08-01', end='2026-09-01')
        sql = self.home_gmv_sql() + " AND p.captured_at>='2026-07-31T22:00:00Z' AND p.captured_at<'2026-08-31T22:00:00Z'"
        self.validate(contract, sql, database=contract['data_source'], graph=graph, scope=scope)
        with self.assertRaises(ValueError):
            self.validate(contract, sql.replace('2026-07-31T22:00:00Z', '2026-07-31T23:00:00Z'),
                          database=contract['data_source'], graph=graph, scope=scope)

    def test_contract_source_mismatch_and_actual_cross_database_reference_still_block(self):
        contract, graph, scope = self.home_contract()
        other = copy.deepcopy(contract); other['data_source'] = 'homenova_20261015'
        with self.assertRaises(ValueError):
            self.validate(other, self.home_gmv_sql(), database=contract['data_source'], graph=graph, scope=scope)
        with self.assertRaises(ValueError):
            self.validate(contract, self.home_gmv_sql().replace('FROM payments', 'FROM homenova_20261015.payments'),
                          database=contract['data_source'], graph=graph, scope=scope)

    def test_table_permissions_and_read_only_are_not_relaxed(self):
        denied = copy.deepcopy(self.scope); denied['allowed_tables'] = []
        with self.assertRaises(ValueError):
            self.validate(self.contract(), 'SELECT SUM(paid_amount) AS total FROM orders_current', scope=denied)
        with self.assertRaises(ValueError):
            self.validate(self.contract(), 'DELETE FROM orders_current')

    def test_cte_name_cannot_hide_a_denied_physical_table(self):
        scope = AccessScope(user_id='audit-only', roles=(), allowed_databases=frozenset({'askdata_mock'}),
                            allowed_tables=frozenset({'orders_current'}))
        contract = self.contract()
        sql = ('WITH orders_history AS (SELECT paid_amount FROM orders_history) '
               'SELECT SUM(h.paid_amount) AS total FROM orders_history h '
               'CROSS JOIN (SELECT paid_amount FROM orders_current LIMIT 1) a')
        with self.assertRaises(ValueError):
            self.engine._validate_sql('askdata_mock', sql, scope)
        with self.assertRaises(ValueError):
            self.validate(contract, sql, scope=scope.public())

    def test_pending_dependency_can_support_more_evidence_but_not_skip_or_delivery(self):
        context = latest.ApplicabilityContractTest().context()
        context['dependency_results'][0]['query_contract'] = {'validation': {'pending_checks': [
            {'code': 'distinct_count_not_proven', 'message': '仍需核实订单粒度'}]}}
        for action in ['skip_step', 'request_delivery']:
            proposal = latest.stop_proposal(); proposal['action'] = action
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_step_decision(proposal, context)
        decision = validate_step_decision(dict(action='execute_step', evidence_ids=['T1:R1'],
                                              reason='继续核实上游实际计算范围'), context)
        self.assertEqual(decision.action, 'execute_step')

    def single_decision(self, contract):
        return dict(action='call_tool', tool_name='query_askdata_mock',
                    arguments={'sql': 'SELECT SUM(paid_amount) AS total FROM orders_current'},
                    query_contract=contract, reason='统计实付金额')

    def test_repeated_invalid_artifact_stops_after_two_calls_without_query(self):
        from app.errors import PipelineStageError
        contract = self.contract(); contract['metrics'][0]['aggregation'] = 'avg'
        decision = self.single_decision(contract)
        agent, model, calls = latest.SingleCallExecutorTest().agent([copy.deepcopy(decision) for _ in range(3)])
        budget = RunBudget('no-progress'); budget.start(); token = current_budget.set(budget)
        try:
            with self.assertRaises(PipelineStageError) as caught:
                agent.prepare(contract['original_question'], 'askdata_mock', self.graph, '', {}, {}, self.scope)
        finally:
            current_budget.reset(token)
        self.assertTrue(caught.exception.context.get('no_progress'))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(caught.exception.context['validation_attempts']), 2)
        self.assertEqual(calls, [])
        self.assertEqual(budget.repairs, {'entry': 1})

    def test_unchanged_sql_with_effective_contract_repair_can_succeed(self):
        invalid = self.contract(); invalid['metrics'][0]['aggregation'] = 'avg'
        first = self.single_decision(invalid)
        fixed = self.single_decision(self.contract())
        agent, model, calls = latest.SingleCallExecutorTest().agent([first, fixed])
        budget = RunBudget('contract-repair'); budget.start(); token = current_budget.set(budget)
        try:
            result = agent.prepare(invalid['original_question'], 'askdata_mock', self.graph, '', {}, {}, self.scope)
        finally:
            current_budget.reset(token)
        self.assertEqual(result['action'], 'executed')
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(calls), 1)
        attempts = result['validation_attempts']
        self.assertEqual([item['success'] for item in attempts], [False, True])
        self.assertEqual(attempts[0]['sql'], attempts[1]['sql'])
        self.assertEqual(budget.repairs, {'entry': 1})


if __name__ == '__main__':
    unittest.main()
