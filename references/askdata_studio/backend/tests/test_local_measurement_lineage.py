"""Column meaning survives query normalization; no provider requests or writes."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from app.querying.duckdb_engine import DuckDbEngine
from app.querying.measurement_context import (compact_metric, measurement_lineage,
                                              output_measurements, repair_diagnostics)
from app.querying.sql_module import SqlQueryModule
from app.security import AccessController
from app.skills import SkillRegistry
from tests.test_query_protocol import graph, metric, SOURCE


def local(identity='local_gmv', **changes):
    return {'id': identity, 'definition': '本范围日经营GMV合计', 'scope': '授权店铺2026年6、7月',
            'source_fields': ['daily_store_metrics.gmv'], 'event_time': ['daily_store_metrics.stat_date'],
            'unit': 'EUR_cents_gross', **changes}


def registered(identity='registered_gmv'):
    return {'id': identity, 'label': '登记GMV', 'definition': '登记经营金额之和',
            'source_fields': ['daily_store_metrics.gmv'], 'unit': 'EUR_cents_gross',
            'time_fields': ['daily_store_metrics.stat_date'], 'semantic_version': 'public-v7',
            'duplicate_risks': ['重复商品行须去重'], 'as_of_policy': '按授权观察截点'}


class LocalMeasurementLineageTest(unittest.TestCase):
    def test_task_local_binds_only_matching_real_alias_and_keeps_semantics(self):
        definition = local()
        before = deepcopy(definition)
        result = measurement_lineage('SELECT SUM(gmv) AS local_gmv FROM daily_store_metrics',
            {'metrics': [metric('local_gmv')]}, [], graph(), local_measurements=[definition])
        column = result['column_metadata']['local_gmv']
        self.assertEqual(column['kind'], 'task_local')
        self.assertEqual(column['measurement_id'], 'local_gmv')
        self.assertEqual(column['metric_id'], 'local_gmv')
        for field in ('definition', 'scope', 'source_fields', 'event_time', 'unit'):
            self.assertEqual(column[field], definition[field])
        self.assertTrue(column['requires_review'])
        self.assertEqual(len(column['semantic_hash']), 64)
        self.assertEqual(column['measurement_version'], column['semantic_hash'])
        self.assertEqual(result['task_local_measurements'][0]['bound_output_columns'], ['local_gmv'])
        self.assertEqual(definition, before)

    def test_unbound_local_retains_definition_without_assigning_same_source_outputs(self):
        result = measurement_lineage('SELECT SUM(gmv) AS value, AVG(gmv) AS average FROM daily_store_metrics',
            {'metrics': [metric('local_gmv')]}, [], graph(), local_measurements=[local()])
        self.assertEqual(result['column_metadata'], {})
        self.assertEqual(result['task_local_measurements'][0]['definition'], local()['definition'])
        self.assertEqual(result['task_local_measurements'][0]['bound_output_columns'], [])
        self.assertEqual(result['unbound_measurements'][0]['measurement_id'], 'local_gmv')
        self.assertIn('measurement_output_unbound', [item['code'] for item in result['pending_checks']])

    def test_recorded_validator_binding_preserves_business_id_after_field_normalization(self):
        contract = {'metrics': [metric('gmv')], 'validation': {'metric_output_bindings': [
            {'declared_name': 'gmv', 'output_aliases': ['value']}]}}
        original = [metric('local_gmv')]
        result = measurement_lineage('SELECT SUM(gmv) AS value FROM daily_store_metrics', contract, [],
            graph(), declared_metrics=original, local_measurements=[local()])
        self.assertEqual(result['column_metadata']['value']['measurement_id'], 'local_gmv')
        without_binding = measurement_lineage('SELECT SUM(gmv) AS gmv FROM daily_store_metrics',
            {'metrics': [metric('gmv')]}, [], graph(), declared_metrics=original, local_measurements=[local()])
        self.assertFalse(without_binding['column_metadata'], 'A physical field spelling is not a recorded binding')

    def test_formal_identity_version_and_risks_survive_validation_normalization(self):
        contract = {'metrics': [metric('gmv')], 'validation': {'metric_output_bindings': [
            {'declared_name': 'gmv', 'output_aliases': ['value']}]}}
        result = measurement_lineage('SELECT SUM(gmv) AS value FROM daily_store_metrics', contract,
            [registered()], graph(), declared_metrics=[metric('registered_gmv')])
        bound = result['column_metadata']['value']
        self.assertEqual(bound['kind'], 'registered')
        self.assertEqual(bound['measurement_id'], 'registered_gmv')
        self.assertEqual(bound['metric_id'], 'registered_gmv')
        self.assertEqual(bound['measurement_version'], 'public-v7')
        self.assertEqual(bound['duplicate_risks'], registered()['duplicate_risks'])
        self.assertEqual(bound['as_of_policy'], registered()['as_of_policy'])

    def test_unknown_name_cannot_become_registered_by_physical_label_coincidence(self):
        result = measurement_lineage('SELECT SUM(gmv) AS custom FROM daily_store_metrics',
            {'metrics': [metric('gmv')]}, [registered('gmv')], graph(),
            declared_metrics=[metric('custom')])
        self.assertEqual(result['column_metadata']['custom']['kind'], 'unresolved')
        self.assertIn('task_local_definition_missing', [item['code'] for item in result['pending_checks']])

    def test_missing_definition_stays_queryable_and_visible(self):
        result = measurement_lineage('SELECT SUM(gmv)/COUNT(*) AS average FROM daily_store_metrics',
            {'metrics': [metric('average', aggregation='derived')]}, [], graph())
        self.assertEqual(result['column_metadata']['average']['kind'], 'unresolved')
        self.assertEqual(result['column_metadata']['average']['semantic_status'], 'task_local_definition_needed')
        self.assertTrue(result['pending_checks'])

    def test_semantic_version_changes_for_definition_scope_and_unit_not_alias_binding(self):
        def version(definition, alias='local_gmv'):
            result = measurement_lineage('SELECT SUM(gmv) AS ' + alias + ' FROM daily_store_metrics',
                {'metrics': [metric('local_gmv')], 'validation': {'metric_output_bindings': [
                    {'declared_name': 'local_gmv', 'output_aliases': [alias]}]}}, [], graph(),
                local_measurements=[definition])
            return result['task_local_measurements'][0]['semantic_hash']
        first = version(local())
        self.assertEqual(first, version(local(), alias='displayed'))
        for definition in (local(definition='不同集合'), local(scope='不同期间'), local(unit='EUR')):
            self.assertNotEqual(first, version(definition))

    def test_conflicting_definitions_and_registered_collision_are_not_guessed(self):
        for definitions, metrics in (([local(), local(definition='另一含义')], []),
                                      ([local('registered_gmv')], [registered()])):
            identity = definitions[0]['id']
            result = measurement_lineage('SELECT SUM(gmv) AS ' + identity + ' FROM daily_store_metrics',
                {'metrics': [metric(identity)]}, metrics, graph(), local_measurements=definitions)
            self.assertNotIn(identity, result['column_metadata'])
            self.assertIn('measurement_output_ambiguous', [item['code'] for item in result['pending_checks']])
            self.assertEqual(len(result['task_local_measurements']), len(definitions))

    def test_component_policy_risks_are_not_lost_while_compacting(self):
        spec = registered()
        spec['component_policies'] = [{'metric_id': 'component', 'counting_key': 'order_id',
            'duplicate_risks': ['重试事件'], 'as_of_policy': '观察截点', 'population': '有效事件'}]
        compact = compact_metric(spec, graph())
        self.assertEqual(compact['component_policies'], spec['component_policies'])

    def test_repair_diagnostic_distinguishes_value_sources_and_new_cohort(self):
        diagnostic = repair_diagnostics('指标来源超出公开依赖：some_ratio / dim.category',
            query_contract={'metrics': [{'name': 'some_ratio', 'source_fields': ['dim.category']}]},
            measurements={'measurements': [{'id': 'some_ratio', 'grain': 'SKU', 'cohort': '同SKU浏览及购买'}]},
            schema_graph={'fields': [{'sql_name': 'dim.category', 'role': 'dimension'}]})
        detail = diagnostic['measurement_conflicts'][0]
        self.assertEqual(detail['expected_grain'], 'SKU')
        self.assertEqual(detail['declared_source_roles']['dim.category'], 'dimension')
        self.assertIn('任务内定义', detail['instruction'])
        self.assertIn('不能只删报错字段', detail['instruction'])


class LocalMeasurementExecutionBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.module = SqlQueryModule(SimpleNamespace(), lambda scope: None,
            SkillRegistry().get('database_query'), DuckDbEngine())
        self.scope = AccessController().scoped('demo_current_sales', SOURCE).public()

    def prepare(self, *, definition=None, identity='local_gmv'):
        return self.module.validate_candidate(sql='SELECT SUM(gmv) AS ' + identity + ' FROM daily_store_metrics',
            query_contract={'metrics': [metric(identity)]}, question='汇总经营金额', database=SOURCE,
            schema_graph=graph(), access_scope=self.scope,
            validation_context={'local_measurements': [definition] if definition else []})

    def test_validation_and_execution_keep_local_identity_and_scope_without_model_call(self):
        prepared = self.prepare(definition=local())
        validation = prepared['query_contract']['validation']
        self.assertEqual(validation['column_metadata']['local_gmv']['measurement_id'], 'local_gmv')
        self.assertEqual(validation['task_local_measurements'][0]['scope'], local()['scope'])
        client = SimpleNamespace(call_tool=lambda *args: {'success': True, 'columns': ['local_gmv'],
                                                         'rows': [{'local_gmv': 123}]})
        result = self.module.execute_prepared(database=SOURCE, access_scope=self.scope, mcp_client=client, **prepared)
        self.assertEqual(result['execution']['column_metadata']['local_gmv']['kind'], 'task_local')
        self.assertFalse(validation['business_definition_verified'])

    def test_missing_local_does_not_fail_real_sql_preflight(self):
        prepared = self.prepare()
        validation = prepared['query_contract']['validation']
        self.assertTrue(validation['sql_compiled'])
        self.assertEqual(validation['column_metadata']['local_gmv']['kind'], 'unresolved')
        self.assertIn('task_local_definition_missing', [item['code'] for item in validation['pending_checks']])

    def test_next_candidate_does_not_inherit_prior_metadata(self):
        first = self.prepare(definition=local())
        second = self.prepare()
        self.assertTrue(first['query_contract']['validation']['task_local_measurements'])
        self.assertEqual(second['query_contract']['validation']['task_local_measurements'], [])
        self.assertEqual(second['query_contract']['validation']['column_metadata']['local_gmv']['kind'], 'unresolved')


if __name__ == '__main__':
    unittest.main()
