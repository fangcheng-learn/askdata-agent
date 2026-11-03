"""Shared query protocol, real local MCP, and independent public CSV arithmetic.

Model responses in controller tests are controlled fixtures, not paid acceptance.
"""
from collections import defaultdict
from copy import deepcopy
import csv
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from app import homenova
from app.database import SCHEMA, physical_table_name
from app.mcp_runtime import LocalMcpClient, create_local_mcp_server
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.querying.query_protocol import canonical_sql, complete_contract, model_contract_schema, sql_facing_graph
from app.retrieval.graph import SchemaGraphBuilder
from app.runtime_budget import RunBudget, current_budget
from app.security import AccessController
from app.services.investigation_store import InvestigationStore
from app.skills import SkillRegistry
from app.workflows.query_graph import QueryWorkflow
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord
from app.workflows.react_prompts import REACT_SYSTEM, SQL_REPAIR_SYSTEM


BACKEND = Path(__file__).resolve().parents[1]
SOURCE = 'ecommerce_ops'
QUESTION = '比较2026年6月和7月的经营指标'


def table(name, database=SOURCE):
    return next(t for t in SCHEMA if t.get('database') == database and physical_table_name(t) == name)


def graph(names=('orders', 'daily_store_metrics')):
    tables = [table(name) for name in names]
    return {'database': SOURCE,
            'tables': [{'id': t['id'], 'name': physical_table_name(t), 'database': SOURCE} for t in tables],
            'fields': [{**f, 'id': t['id']+'.'+f['name'], 'table_id': t['id'],
                        'sql_name': physical_table_name(t)+'.'+f['name']}
                       for t in tables for f in t['fields']], 'joins': []}


def metric(name='gmv', field='daily_store_metrics.gmv', aggregation='sum'):
    return {'name': name, 'source_fields': [field], 'aggregation': aggregation}


def monthly_sql(source='daily_store_metrics', field='gmv', date='stat_date'):
    return (f"SELECT strftime({date}, '%Y-%m') AS month, SUM({field}) AS value FROM {source} "
            f"WHERE {date}>='2026-06-01' AND {date}<'2026-08-01' GROUP BY month ORDER BY month")


class QueryProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = DuckDbEngine()
        cls.validator = QueryContractValidator(cls.engine)
        cls.graph = graph()
        cls.scope = AccessController().scoped('demo_current_sales', SOURCE)

    def validate(self, raw, sql, question=QUESTION):
        return self.validator.validate(raw, sql, question, SOURCE, self.graph, self.scope.public())

    def test_minimal_metric_declaration_completes_real_sql_and_keeps_audit(self):
        raw = {'metrics': [metric()]}
        checked = self.validate(raw, monthly_sql())
        self.assertEqual(raw, {'metrics': [metric()]}, 'Input is not mutated')
        self.assertEqual(checked['tables'], ['daily_store_metrics'])
        self.assertEqual(checked['dimensions'], ['daily_store_metrics.stat_date'])
        self.assertEqual(checked['time_range'], {'field': 'daily_store_metrics.stat_date',
            'start': '2026-06-01', 'end': '2026-08-01', 'end_inclusive': False})
        self.assertTrue(checked['validation']['sql_compiled'])
        self.assertFalse(checked['validation']['business_definition_verified'])
        self.assertTrue(any(n['path'] == 'time_range' for n in checked['validation']['normalizations']))

    def test_legacy_boundary_keys_and_unique_field_are_mechanically_compatible(self):
        checked = self.validate({'metrics': [metric()], 'time_range': {
            'start': '2026-06-01', 'end': '2026-08-01', 'inclusive_start': True, 'inclusive_end': False}}, monthly_sql())
        self.assertEqual(checked['time_range']['field'], 'daily_store_metrics.stat_date')
        self.assertFalse(checked['time_range']['end_inclusive'])

    def test_conflicting_explicit_conditions_are_not_replaced(self):
        cases = [
            {'data_source': 'homenova_20260831'},
            {'tables': ['orders']},
            {'time_range': {'field': 'daily_store_metrics.stat_date', 'start': '2026-05-01',
                            'end': '2026-08-01', 'end_inclusive': False}},
            {'filters': [{'field': 'daily_store_metrics.store_id', 'operator': '=', 'value': 'never-filtered'}]},
            {'time_range': {'field': 'daily_store_metrics.stat_date', 'start': '2026-06-01',
                            'end': '2026-08-01', 'end_inclusive': False, 'inclusive_end': True}},
            {'time_range': {'field': 'daily_store_metrics.stat_date', 'start': '2026-06-01',
                            'end': '2026-08-01', 'inclusive_start': False, 'inclusive_end': False}},
        ]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises((ValueError, PermissionError)):
                self.validate({'metrics': [metric()], **extra}, monthly_sql())

    def test_unknown_field_write_cross_source_and_missing_business_declaration_still_reject(self):
        cases = [
            ({'metrics': [metric('paid_amount', 'orders.paid_amount')]}, monthly_sql('orders', 'paid_amount', 'order_time')),
            ({'metrics': [metric()]}, 'DELETE FROM daily_store_metrics'),
            ({'metrics': [metric()]}, monthly_sql().replace('FROM daily_store_metrics', 'FROM other.daily_store_metrics')),
            ({'metrics': [metric()]}, monthly_sql() + '; SELECT 1 AS injected'),
            ({}, monthly_sql()),
            ({'metrics': [{'name': 'gmv', 'aggregation': 'sum'}]}, monthly_sql()),
        ]
        before = set(self.engine.results)
        for raw, sql in cases:
            with self.subTest(sql=sql, raw=raw), self.assertRaises((ValueError, PermissionError)):
                self.validate(raw, sql)
        self.assertEqual(set(self.engine.results), before)

    def test_unknown_order_time_is_reported_with_real_alternatives(self):
        with self.assertRaises(ValueError) as error:
            self.validate({'metrics': [metric('paid_amount', 'orders.paid_amount')],
                'time_range': {'field': 'orders.order_time', 'start': '2026-06-01',
                               'end': '2026-08-01', 'end_inclusive': False}},
                monthly_sql('orders', 'paid_amount', 'order_time'))
        self.assertIn('orders.order_time', str(error.exception))
        self.assertIn('orders.ordered_at', str(error.exception))

    def test_cte_group_alias_and_ordinal_resolve_to_physical_lineage(self):
        for grouping in ('month', '1'):
            sql = ("WITH x AS (SELECT stat_date AS dt, gmv AS amount FROM daily_store_metrics "
                   "WHERE stat_date>='2026-06-01' AND stat_date<'2026-08-01') "
                   f"SELECT strftime(dt, '%Y-%m') AS month, SUM(amount) AS value FROM x GROUP BY {grouping}")
            checked = self.validate({'metrics': [metric()]}, sql)
            self.assertEqual(checked['dimensions'], ['daily_store_metrics.stat_date'])
            self.assertTrue(checked['validation']['sql_compiled'])

    def test_sql_identifier_case_and_unambiguous_source_alias_are_normalized(self):
        sql = monthly_sql().replace('FROM daily_store_metrics', 'FROM DAILY_STORE_METRICS').replace('stat_date', 'STAT_DATE')
        checked = self.validate({'metrics': [metric()]}, sql)
        self.assertEqual(checked['time_range']['field'], 'daily_store_metrics.stat_date')
        sql = monthly_sql().replace('FROM daily_store_metrics', 'FROM daily_store_metrics m')
        checked = self.validate({'metrics': [metric(field='m.gmv')]}, sql)
        self.assertEqual(checked['metrics'][0]['source_fields'], ['daily_store_metrics.gmv'])

    def test_malformed_json_shape_stays_a_specific_contract_error(self):
        cases = [
            {'metrics': [{'name': 'gmv', 'source_fields': None, 'aggregation': 'sum'}]},
            {'metrics': [metric()], 'tables': [{'wrong': 'type'}]},
            {'metrics': [metric()], 'time_ranges': {'wrong': 'type'}},
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'query_contract结构'):
                self.validate(raw, monthly_sql())

    def test_multiple_time_fields_are_not_guessed_for_partial_declaration(self):
        sql = ("SELECT SUM(m.gmv) AS gmv FROM daily_store_metrics m JOIN orders o ON m.store_id=o.store_id "
               "WHERE m.stat_date>='2026-06-01' AND m.stat_date<'2026-08-01' "
               "AND o.ordered_at>='2026-06-01' AND o.ordered_at<'2026-08-01'")
        raw = {'metrics': [metric()], 'time_range': {'start': '2026-06-01', 'end': '2026-08-01', 'end_inclusive': False}}
        completed, _ = complete_contract(raw, sql, QUESTION, SOURCE, self.graph)
        self.assertNotIn('field', completed['time_range'])
        with self.assertRaises(ValueError):
            self.validate(raw, sql)

    def test_or_time_predicate_requires_explicit_declaration(self):
        sql = ("SELECT SUM(gmv) AS value FROM daily_store_metrics "
               "WHERE (stat_date>='2026-06-01' AND stat_date<'2026-07-01') "
               "OR (stat_date>='2026-07-01' AND stat_date<'2026-08-01')")
        with self.assertRaisesRegex(ValueError, '时间范围'):
            self.validate({'metrics': [metric()]}, sql)

    def test_exact_authorized_metadata_id_maps_but_unknown_prefix_does_not(self):
        internal = table('orders')['id']
        sql = monthly_sql(internal, 'paid_amount', 'ordered_at')
        normalized, events = canonical_sql(sql, SOURCE, self.graph, self.scope.public())
        self.assertTrue(events)
        checked = self.validate({'metrics': [metric('paid_amount', 'orders.paid_amount')]}, normalized)
        self.assertEqual(checked['tables'], ['orders'])
        for bad in ('homenova_20261015.orders', 'other.orders'):
            text = sql.replace(internal, bad)
            normalized, events = canonical_sql(text, SOURCE, self.graph, self.scope.public())
            self.assertEqual(normalized, text)
            self.assertFalse(events)
        restricted = AccessController().scoped('demo_current_sales', SOURCE).public()
        restricted['allowed_tables'] = []
        unchanged, events = canonical_sql(sql, SOURCE, self.graph, restricted)
        self.assertEqual(unchanged, sql)
        self.assertFalse(events)

    def test_model_schema_exposes_physical_names_only(self):
        physical = sql_facing_graph(self.graph)
        self.assertEqual({t['name'] for t in physical['tables']}, {'orders', 'daily_store_metrics'})
        self.assertTrue(all('id' not in t and 'database' not in t for t in physical['tables']))
        self.assertTrue(all('table_id' not in f and 'id' not in f for f in physical['fields']))
        self.assertIn('orders.ordered_at', {f['sql_name'] for f in physical['fields']})
        self.assertNotIn('orders.order_time', {f['sql_name'] for f in physical['fields']})
        schema = model_contract_schema()
        self.assertEqual(schema['required'], ['metrics'])
        self.assertIn('field', schema['$defs']['TimeRangeContract']['required'])

    def test_real_demo_mcp_matches_independent_csv_not_model_guesses(self):
        expected = defaultdict(Decimal)
        with (BACKEND/'data/databases/ecommerce_ops/daily_store_metrics.csv').open(newline='', encoding='utf-8-sig') as stream:
            for row in csv.DictReader(stream):
                if '2026-06-01' <= row['stat_date'] < '2026-08-01':
                    expected[row['stat_date'][:7]] += Decimal(row['gmv'])
        checked = self.validate({'metrics': [metric()]}, monthly_sql())
        client = LocalMcpClient(create_local_mcp_server(self.engine, self.scope))
        result = client.call_tool('query_'+SOURCE, {'sql': checked['validation']['sql_summary']['normalized_sql']})
        self.assertTrue(result['success'], result.get('error'))
        self.assertTrue(result['complete'])
        self.assertEqual(result['total_rows'], 2)
        for row in result['rows']:
            self.assertAlmostEqual(row['value'], float(expected[row['month']]), places=4)

    def test_real_homenova_mcp_minimal_contract_and_metadata_id_match_csv(self):
        from tests.test_react_v1_sql_probes import PROBES
        database = 'homenova_20260831'
        graph = homenova.schema_payload(database)
        graph = {**graph, 'tables': [t for t in graph['tables'] if t['name'] in {'payments', 'order_items'}]}
        graph['fields'] = [{**f, 'id': t['id']+'.'+f['name'], 'table_id': t['id'],
                            'sql_name': t['name']+'.'+f['name']} for t in graph['tables'] for f in t['fields']]
        scope = AccessController().scoped('demo_admin', database)
        probe = next(p for p in PROBES.probes() if p['name'] == 'aug_gmv_direct')
        sql = probe['sql'].replace('FROM payments p', 'FROM hn08_payments p').replace('JOIN order_items oi', 'JOIN hn08_order_items oi')
        normalized, changes = canonical_sql(sql, database, graph, scope.public())
        self.assertEqual(len(changes), 2)
        checked = self.validator.validate({'metrics': deepcopy(probe['contract']['metrics'])}, normalized,
            probe['contract']['original_question'], database, graph, scope.public())
        result = LocalMcpClient(create_local_mcp_server(self.engine, scope)).call_tool('query_'+database, {'sql': normalized})
        self.assertTrue(result['success'], result.get('error'))
        self.assertTrue(result['complete'])
        self.assertTrue(PROBES.equal_values(result['rows'], result['columns'], PROBES.independent_csv()['aug_gmv']))
        self.assertFalse(checked['validation']['business_definition_verified'])
        bad = deepcopy(probe['contract']['metrics'])
        bad[0]['source_fields'] = ['order_items.quantity']
        with self.assertRaises(ValueError):
            self.validator.validate({'metrics': bad}, normalized, probe['contract']['original_question'], database, graph, scope.public())


class QueryContextWiringTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = InvestigationStore(Path(self.temp.name)/'state.sqlite3')
        self.workflow = SimpleNamespace(model_client=None, database_engine=DuckDbEngine(), skills=SkillRegistry(), budgets={})
        self.harness = ReactInvestigationHarness(self.workflow, self.store)
        self.task = ReactRecord(state={'task_id': 'context-protocol', 'query': QUESTION,
            'datasource_id': SOURCE, 'session_id': 'context', 'workspace': {},
            'access_scope': AccessController().scoped('demo_current_sales', SOURCE).public(),
            'extraction': {'time_expressions': ['2026年6月', '2026年7月']},
            'task_understanding': {'goal': QUESTION, 'period': '2026年7月', 'baseline': '2026年6月'}})

    def test_real_schema_builder_receives_inherited_time_and_supplies_ordered_at(self):
        definition = table('orders')
        hits = [{'doc_id': definition['id']+'.paid_amount', 'table_id': definition['id'],
                 'database_id': SOURCE, 'field_name': 'paid_amount', 'field_label': '实付金额',
                 'field_type': '数值', 'field_role': 'metric', 'score': 1.0}]
        received = []

        def retrieve(state, charge_budget=True):
            received.append(deepcopy(state))
            return QueryWorkflow._retrieve_schema(self.workflow, state, charge_budget=charge_budget)

        self.workflow.schema_index = SimpleNamespace(
            retrieve=lambda *a, **kw: {'hits': hits, 'selected_count': 1, 'table_candidates': [], 'low_confidence_candidates': []},
            include_workspace=lambda r, *a: r)
        self.workflow.graph_builder = SchemaGraphBuilder()
        self.workflow._retrieve_schema = retrieve
        self.harness._retrieve(self.task, {'goal': '核对金额和支付订单', 'fields': [], 'metric_ids': []})
        self.assertIn('2026年6月', received[0]['extraction']['time_expressions'])
        self.assertIn('2026年7月', received[0]['extraction']['time_expressions'])
        self.assertIn('orders.ordered_at', {f['sql_name'] for f in self.task.field_pool.values()})
        context = self.harness._context(self.task)
        self.assertIn('orders.ordered_at', {f['sql_name'] for f in context['semantics']['schema_graph']['fields']})
        self.harness._persist(self.task)
        saved = self.store.load_task('context-protocol', self.task.state['access_scope']['user_id'])
        self.assertEqual(saved['payload']['state']['extraction'], self.task.state['extraction'])

    def test_first_generation_and_repair_share_technical_policy_and_schema_once(self):
        calls = []
        self.harness.model = SimpleNamespace(chat_json=lambda system, user: calls.append((system, json.loads(user))) or {})
        skill = self.workflow.skills.get('database_query')
        self.task.skills['database_query'] = {'version': 'fixture', 'instructions': skill.instructions}
        budget = RunBudget('context-protocol')
        token = current_budget.set(budget)
        try:
            self.harness._call(self.task, 'react', REACT_SYSTEM, {})
            self.harness._call(self.task, 'sql_repair', SQL_REPAIR_SYSTEM, {})
        finally:
            current_budget.reset(token)
        self.assertEqual(calls[0][1]['query_protocol'], calls[1][1]['query_protocol'])
        for system, _ in calls:
            self.assertEqual(system.count('## 简明查询契约与检查'), 1)
            self.assertIn('不加数据库或 schema 前缀', system)
            self.assertIn('query_protocol.query_contract_schema', system)
        self.assertEqual(sum(e.get('skill') == 'database_query' for e in budget.events if e.get('stage') == 'skill_injection'), 2)
        self.assertNotIn('database_query', {s['name'] for s in self.harness._context(self.task)['skill_catalog']})


if __name__ == '__main__':
    unittest.main()
