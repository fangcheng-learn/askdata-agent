"""Offline source choices and real LangGraph resume; no model/network calls."""
from __future__ import annotations

import copy
import csv
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from langgraph.types import Command

from app.config import Settings
from app.database import SCHEMA, physical_table_name
from app.models import Clarification
from app.querying.metric_sources import normalize_source_clarification
from app.security import AccessController
from app.workflows.query_graph import QueryWorkflow


DATABASE = 'ecommerce_ops'
QUESTION = '2026年六月GMV'


def source_graph():
    tables = [copy.deepcopy(table) for table in SCHEMA
              if table.get('database') == DATABASE
              and physical_table_name(table) in {'daily_store_metrics', 'daily_product_metrics'}]
    return {
        'database': DATABASE,
        'tables': [{**table, 'name': physical_table_name(table)} for table in tables],
        'fields': [{'table_id': table['id'], 'name': field['name']}
                   for table in tables for field in table['fields']],
        'joins': [],
    }


def source_options():
    return {
        'parameter': 'gmv_source', 'question': '请问您需要查询哪个维度的GMV？',
        'reason': '数据有不同统计粒度，需要确认来源。',
        'options': [
            {'id': 'store', 'label': '店铺维度GMV', 'description': '按店铺汇总',
             'metric_sources': {'GMV': 'DAILY_STORE_METRICS.GMV'}},
            {'id': 'product', 'label': '商品维度GMV', 'description': '按商品汇总',
             'metric_sources': {'gmv': 'daily_product_metrics.gmv'}},
        ],
    }


class SourceClarificationTest(unittest.TestCase):
    def setUp(self):
        self.graph = source_graph()
        self.scope = AccessController().scoped('demo_current_sales', DATABASE).public()

    def normalize(self, payload=None, **kwargs):
        return normalize_source_clarification(
            payload or source_options(), kwargs.get('database', DATABASE),
            kwargs.get('graph', self.graph), kwargs.get('scope', self.scope))

    def test_options_bind_canonical_physical_sources_and_show_authoritative_labels(self):
        raw = source_options()
        before = copy.deepcopy(raw)
        result = self.normalize(raw)
        store = result['options'][0]
        self.assertEqual(store['metric_sources'], {'gmv': 'daily_store_metrics.gmv'})
        self.assertEqual(store['id'], 'store')
        self.assertEqual(store['label'], '店铺维度GMV')
        self.assertIn('店铺日经营指标', store['description'])
        self.assertIn('daily_store_metrics.gmv', store['description'])
        self.assertEqual(raw, before)
        self.assertEqual(self.normalize(result), result, 'normalization must be idempotent')
        self.assertEqual(Clarification.model_validate(result).options[0].metric_sources, store['metric_sources'])

    def test_legacy_options_remain_usable_without_guessing_a_source(self):
        raw = source_options()
        for option in raw['options']:
            option.pop('metric_sources')
        result = self.normalize(raw)
        self.assertEqual(result['options'][0]['metric_sources'], {})
        self.assertEqual(result['options'][0]['description'], '按店铺汇总')

    def test_name_must_be_a_real_field_name_label_or_alias(self):
        raw = source_options()
        raw['options'][0]['metric_sources'] = {'净利润': 'daily_store_metrics.gmv'}
        with self.assertRaisesRegex(ValueError, '名称与来源字段不匹配'):
            self.normalize(raw)

    def test_sources_cannot_escape_database_or_current_schema_graph(self):
        for field in ['orders_current.paid_amount', 'daily_store_metrics.not_a_field']:
            raw = source_options()
            raw['options'][0]['metric_sources'] = {'gmv': field}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '授权Schema图'):
                self.normalize(raw)
        graph = copy.deepcopy(self.graph)
        graph['fields'] = [item for item in graph['fields'] if not (
            item['table_id'] == 'ecommerce_ops.daily_store_metrics' and item['name'] == 'gmv')]
        with self.assertRaisesRegex(ValueError, '授权Schema图'):
            self.normalize(graph=graph)

    def test_graph_presence_cannot_grant_denied_table_or_database(self):
        for key in ['allowed_tables', 'allowed_databases']:
            scope = copy.deepcopy(self.scope)
            scope[key] = []
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, '授权Schema图'):
                self.normalize(scope=scope)

    def test_ambiguous_duplicate_metric_keys_cannot_overwrite_source(self):
        raw = source_options()
        raw['options'][0]['metric_sources']['gmv'] = 'daily_product_metrics.gmv'
        with self.assertRaisesRegex(ValueError, '冲突来源'):
            self.normalize(raw)

    def test_aliases_cannot_bind_the_same_metric_to_different_sources_in_one_choice(self):
        raw = source_options()
        raw['options'][0]['metric_sources']['商品交易总额'] = 'daily_product_metrics.gmv'
        with self.assertRaisesRegex(ValueError, '冲突来源'):
            self.normalize(raw)

    def test_empty_non_mapping_sources_are_not_silently_accepted(self):
        raw = source_options()
        raw['options'][0]['metric_sources'] = []
        with self.assertRaisesRegex(ValueError, '映射'):
            self.normalize(raw)


class RecordedSourceModel:
    """Fixed protocol outputs only; this is not a live Agent acceptance run."""
    enabled = True

    def __init__(self, *, legacy=False):
        self.legacy = legacy
        self.coder_payloads = []

    def chat_json(self, system, user):
        if system.startswith('你是单数据库问数智能体'):
            payload = json.loads(user)
            self.coder_payloads.append(copy.deepcopy(payload))
            if not payload['confirmed_parameters']:
                clarification = source_options()
                if self.legacy:
                    for option in clarification['options']:
                        option.pop('metric_sources')
                return {'action': 'clarify', 'clarification': clarification}
            return {
                'action': 'call_tool', 'tool_name': 'query_ecommerce_ops',
                'arguments': {'sql': "SELECT SUM(gmv) AS gmv FROM daily_store_metrics "
                                    "WHERE stat_date >= '2026-06-01' AND stat_date < '2026-07-01'"},
                'query_contract': {
                    'original_question': QUESTION, 'data_source': DATABASE,
                    'tables': ['daily_store_metrics'],
                    'metrics': [{'name': 'gmv', 'source_fields': ['daily_store_metrics.gmv'], 'aggregation': 'sum'}],
                    'dimensions': [], 'filters': [],
                    'time_range': {'field': 'daily_store_metrics.stat_date', 'start': '2026-06-01', 'end': '2026-07-01'},
                    'grain': 'overall', 'assumptions': [], 'open_questions': [],
                },
            }
        if '查询结果整理器' in system:
            return {'valid': True, 'reason': '受控离线流程输出', 'title': '6月店铺GMV',
                    'analysis': '结果来自店铺日经营指标的6月汇总。'}
        raise AssertionError('Unexpected model path in offline test: ' + system[:80])

    def embed(self, *_args, **_kwargs):
        raise AssertionError('Offline resume tests must not call embedding')

    def rerank(self, *_args, **_kwargs):
        raise AssertionError('Offline resume tests must not call reranking')


class RealGraphSourceResumeTest(unittest.TestCase):
    def run_resume(self, *, legacy=False):
        model = RecordedSourceModel(legacy=legacy)
        workflow = QueryWorkflow(model, object(), Settings(api_key='', session_archive_enabled=False))
        graph = source_graph()
        counts = {'preprocess': 0, 'retrieval': 0}

        def preprocess(state):
            counts['preprocess'] += 1
            return {'intent': {'action': 'database_query', 'confidence': 1, 'reason': 'offline replay'},
                    'standalone_query': state['query'], 'extraction': {}, 'needs_planner': False}

        def retrieve(state):
            counts['retrieval'] += 1
            return {'schema_graph': graph, 'schema_context': json.dumps(graph, ensure_ascii=False),
                    'database_names': [DATABASE], 'retrieval': {'hits': []}, 'clarification': None}

        with patch.object(workflow, '_preprocess', preprocess), patch.object(workflow, '_retrieve_schema', retrieve):
            workflow.graph = workflow._compile()
            task_id = 'offline-legacy-source' if legacy else 'offline-structured-source'
            payload = {'task_id': task_id, 'query': QUESTION, 'session_id': task_id,
                       'datasource_id': DATABASE, 'workspace': {'datasource_id': DATABASE},
                       'access_scope': AccessController().scoped('demo_current_sales', DATABASE).public()}
            waiting = workflow.invoke(payload, task_id)
            self.assertEqual(waiting.status, 'waiting_clarification')
            self.assertEqual(waiting.task_id, task_id)
            self.assertEqual(waiting.clarification.options[0].metric_sources,
                             {} if legacy else {'gmv': 'daily_store_metrics.gmv'})
            budget = workflow.budgets[task_id]
            self.assertIsNone(budget.started, 'waiting must pause active time')
            result = workflow.invoke(Command(resume={'option_id': 'store'}), task_id)
            self.assertIs(workflow.budgets[task_id], budget)
            self.assertEqual(result.task_id, task_id)
            self.assertEqual(result.status, 'completed', result.analysis)
            self.assertTrue(result.final_reviewed)
            self.assertEqual(counts, {'preprocess': 1, 'retrieval': 2})
            self.assertEqual(len(model.coder_payloads), 2, 'no repeated SQL repairs expected')
            self.assertEqual(model.coder_payloads[-1]['original_question'], QUESTION)
            self.assertEqual(model.coder_payloads[-1]['confirmed_parameters'], {'gmv_source': 'store'})
            self.assertIn('用户补充确认', model.coder_payloads[-1]['query'])
            state = workflow.graph.get_state(workflow.run_config(task_id)).values
            self.assertEqual(state['query'], QUESTION)
            history = state['workspace']['clarification_history']
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['label'], '店铺维度GMV')
            if not legacy:
                self.assertEqual(state['workspace']['confirmed_metric_sources'], {
                    'database': DATABASE, 'task_id': task_id,
                    'sources': {'gmv': 'daily_store_metrics.gmv'}})
            else:
                self.assertNotIn('confirmed_metric_sources', state['workspace'])
            csv_file = Path(__file__).resolve().parents[1] / 'data/databases/ecommerce_ops/daily_store_metrics.csv'
            with csv_file.open(encoding='utf-8-sig', newline='') as handle:
                expected = sum((Decimal(row['gmv']) for row in csv.DictReader(handle)
                                if '2026-06-01' <= row['stat_date'] < '2026-07-01'), Decimal(0))
            self.assertEqual(len(result.rows), 1)
            self.assertAlmostEqual(float(expected), result.rows[0]['gmv'], places=5)

    def test_structured_source_choice_resumes_same_task_and_executes_real_local_sql(self):
        self.run_resume()

    def test_legacy_choice_resumes_without_claiming_structural_source_confirmation(self):
        self.run_resume(legacy=True)


if __name__ == '__main__':
    unittest.main()
