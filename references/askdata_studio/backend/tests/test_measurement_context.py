"""Public measurement contracts, deterministic selection and repair feedback."""
from copy import deepcopy
import unittest

from app.querying.measurement_context import (compact_metric, measurement_card, output_measurements,
                                            repair_diagnostics, resolve_query_requirements)
from app.querying.query_context import assemble_query_context
from app.querying.sql_module import SqlQueryModule
from app.querying.duckdb_engine import DuckDbEngine
from app.skills import SkillRegistry
from tests.test_sql_module import ScriptModel, item


def graph():
    tables = ['orders', 'payments', 'sessions', 'shipments']
    return {'database': 'fixture', 'tables': [{'id': 'idx_' + name, 'name': name, 'database': 'fixture'} for name in tables],
            'fields': [{'id': 'idx_' + table + '.' + name, 'table_id': 'idx_' + table,
                        'sql_name': table + '.' + name, 'name': name, 'role': role, 'unit': unit,
                        'samples': ['captured'] if name == 'status' else []}
                       for table, name, role, unit in [
                           ('orders', 'order_id', 'dimension', 'orders'),
                           ('orders', 'checkout_session_id', 'dimension', 'sessions'),
                           ('payments', 'order_id', 'dimension', 'orders'),
                           ('payments', 'captured_at', 'time', 'timestamp'),
                           ('payments', 'status', 'filter', 'enum'),
                           ('sessions', 'session_id', 'dimension', 'sessions'),
                           ('sessions', 'started_at', 'time', 'timestamp'),
                           ('shipments', 'shipped_at', 'time', 'timestamp')]],
            'joins': [{'left_table': 'idx_orders', 'left_field': 'order_id',
                       'right_table': 'idx_payments', 'right_field': 'order_id', 'relation_type': 'one_to_many'},
                      {'left_table': 'idx_orders', 'left_field': 'checkout_session_id',
                       'right_table': 'idx_sessions', 'right_field': 'session_id', 'relation_type': 'many_to_one'}]}


def metrics():
    definition = '成功支付的不同订单数'
    return [{'id': 'paid_order_count', 'label': '支付订单数', 'aliases': ['订单量'],
             'definition': definition, 'formula': definition, 'unit': 'orders',
             'required_fields': [{'table': 'payments', 'table_id': 'idx_payments', 'field': 'order_id', 'field_id': 'idx_payments.order_id'},
                                 {'table': 'payments', 'field': 'status'}, {'table': 'payments', 'field': 'captured_at'}],
             'time_fields': [{'table': 'payments', 'field': 'captured_at'}],
             'required_predicates': ["payments.status='captured'"], 'maturity': '已观察',
             'component_policies': [{'metric_id': 'paid_order_count', 'definition': definition,
                                     'required_predicates': ["payments.status='captured'"], 'maturity': '已观察',
                                     'time_fields': [{'table': 'payments', 'field': 'captured_at'}]}]},
            {'id': 'sessions_count', 'label': '会话数', 'unit': 'sessions', 'definition': '有效会话数',
             'required_fields': ['sessions.session_id', 'sessions.started_at'],
             'time_fields': [{'table': 'sessions', 'field': 'started_at'}]}]


class MeasurementContextTest(unittest.TestCase):
    def test_registered_card_has_one_physical_dependency_and_correct_entity_time(self):
        card = measurement_card(metrics()[0], graph())
        self.assertEqual(card['statistical_entity'], 'orders')
        self.assertEqual(card['entity_basis'], 'registered_unit')
        self.assertEqual(card['event_time'], ['payments.captured_at'])
        self.assertIn('payments.order_id', card['source_fields'])
        self.assertNotIn('idx_payments', str(card))
        self.assertNotIn('formula', card)
        self.assertNotIn('component_policies', card)

    def test_resolved_requirements_select_current_metric_not_whole_cache(self):
        resolved = resolve_query_requirements([{'query_id': 'Q1', 'goal': '核对订单量',
            'metric_requirements': ['订单量'], 'dimensions': ['month']}], graph(), metrics())
        self.assertEqual(resolved['metric_ids'], ['paid_order_count'])
        self.assertEqual(resolved['unresolved_requirements'], [])
        bundle = assemble_query_context(graph(), metrics=metrics(), metric_ids=resolved['metric_ids'],
                                        requested_fields=resolved['requested_fields'])
        self.assertEqual([m['id'] for m in bundle['registered_metrics']], ['paid_order_count'])
        self.assertEqual([t['name'] for t in bundle['schema_graph']['tables']], ['payments'])
        self.assertNotIn('shipments', str(bundle['schema_graph']))
        self.assertIn('captured', str(bundle['schema_graph']))

    def test_unknown_business_measure_is_not_marked_unsupported_or_registered(self):
        result = resolve_query_requirements([{'query_id': 'Q1', 'metric_requirements': ['加购会话数']}], graph(), metrics())
        self.assertEqual(result['metric_ids'], [])
        self.assertEqual(result['unresolved_requirements'][0]['kind'], 'unresolved_measure')
        self.assertEqual(result['queries'][0]['measurements'], [])

    def test_field_measure_keeps_time_candidates_without_guessing_event_clock(self):
        result = resolve_query_requirements([{'query_id': 'Q1', 'metric_requirements': ['order_id']}],
            {'fields': [{'table_id': 't', 'name': 'order_id', 'sql_name': 'orders.order_id'},
                        {'table_id': 't', 'name': 'ordered_at', 'sql_name': 'orders.ordered_at', 'sql_type': 'TIMESTAMP'}]}, [])
        self.assertIn('orders.ordered_at', result['requested_fields'])
        card = result['queries'][0]['measurements'][0]
        self.assertEqual(card['event_time_candidates'], ['orders.ordered_at'])
        self.assertNotIn('event_time', card)

    def test_unresolved_dimension_does_not_disappear_after_metric_resolution(self):
        result = resolve_query_requirements([{'query_id': 'Q1', 'metric_requirements': ['paid_order_count'],
                                              'dimensions': ['业务分组未匹配']}], graph(), metrics())
        self.assertEqual(result['unresolved_requirements'][0]['kind'], 'unresolved_dimension')

    def test_same_alias_with_different_registered_meanings_stays_ambiguous(self):
        other = deepcopy(metrics()[0]); other['id'] = 'different_order_count'
        result = resolve_query_requirements([{'query_id': 'Q1', 'metric_requirements': ['订单量']}], graph(), [*metrics(), other])
        self.assertEqual(result['unresolved_requirements'][0]['kind'], 'ambiguous_measure')
        self.assertEqual(result['metric_ids'], [])

    def test_different_time_bases_remain_visible_in_same_business_batch(self):
        result = resolve_query_requirements([{'query_id': 'Q1',
            'metric_requirements': ['paid_order_count', 'sessions_count']}], graph(), metrics())
        groups = result['grouping'][0]['time_basis_groups']
        self.assertEqual(len(groups), 2)
        self.assertEqual({g['event_time'][0] for g in groups}, {'payments.captured_at', 'sessions.started_at'})

    def test_repair_diagnostic_names_actual_object_expected_entity_and_event_time(self):
        resolved = resolve_query_requirements([{'query_id': 'Q1', 'metric_requirements': ['paid_order_count']}], graph(), metrics())
        diagnostic = repair_diagnostics('指标来源超出公开依赖：paid_order_count / orders.checkout_session_id',
            sql='SELECT COUNT(DISTINCT o.checkout_session_id) AS paid_order_count FROM orders o',
            query_contract={'metrics': [{'name': 'paid_order_count', 'source_fields': ['idx_orders.checkout_session_id']}]},
            measurements=resolved, schema_graph=graph())
        conflict = diagnostic['measurement_conflicts'][0]
        self.assertEqual(conflict['actual_source_fields'], ['orders.checkout_session_id'])
        self.assertEqual(conflict['actual_field_units'], {'orders.checkout_session_id': 'sessions'})
        self.assertEqual(conflict['expected_statistical_entity'], 'orders')
        self.assertEqual(conflict['expected_event_time'], ['payments.captured_at'])
        self.assertIn('COUNT(DISTINCT', diagnostic['actual_aggregate_expressions'][0])

    def test_model_context_excludes_manifest_and_repeated_definition_and_keeps_real_error(self):
        model = ScriptModel([{'queries': [item('Q1')]}])
        module = SqlQueryModule(model, lambda scope: None, SkillRegistry().get('database_query'), DuckDbEngine())
        context = {'database': 'fixture', 'schema_graph': graph(), 'registered_metrics': metrics(),
                   'manifest': {'should_remain_backend_only': True}}
        module.generate_batch([{'query_id': 'Q1', 'goal': '核对', 'metric_requirements': ['paid_order_count']}], context,
            repair={'Q1': {'sql': 'SELECT COUNT(DISTINCT o.checkout_session_id) FROM orders o',
                           'query_contract': {'metrics': [{'name': 'paid_order_count', 'source_fields': ['orders.checkout_session_id']}]},
                           'error': '指标来源超出公开依赖：paid_order_count / orders.checkout_session_id'}})
        payload = model.calls[0][1]
        self.assertNotIn('manifest', payload)
        self.assertNotIn('formula', payload['registered_metrics'][0])
        self.assertEqual(payload['measurement_requests'][0]['measurements'], [{'id': 'paid_order_count', 'kind': 'registered'}])
        self.assertIn('expected_event_time', str(payload['failed_queries']))

    def test_output_alias_binding_does_not_guess_generic_value_or_cte_currency(self):
        metric = {'id': 'gmv', 'label': 'GMV', 'unit': 'EUR_cents_gross',
                  'definition': '支付商品金额', 'required_fields': ['order_items.amount_cents']}
        contract = {'metrics': [{'name': 'gmv'}]}
        money_graph = {'fields': [{'sql_name': 'order_items.amount_cents', 'unit': 'EUR_cents_gross'}]}
        direct = output_measurements('SELECT SUM(i.amount_cents)/100 AS gmv FROM order_items i', contract, [metric], money_graph)
        self.assertEqual(direct['gmv']['currency'], 'EUR')
        self.assertEqual(direct['gmv']['unit'], 'EUR_gross')
        generic = output_measurements('SELECT SUM(i.amount_cents) AS value FROM order_items i', contract, [metric], {})
        self.assertEqual(generic, {})
        cte = output_measurements('WITH x AS (SELECT amount_cents/100 AS v FROM order_items) SELECT SUM(v)/100 AS gmv FROM x', contract, [metric], {})
        self.assertNotIn('unit', cte['gmv'])
        self.assertEqual(cte['gmv']['source_unit'], 'EUR_cents_gross')


if __name__ == '__main__':
    unittest.main()
