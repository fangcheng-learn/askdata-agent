"""Controlled model protocol plus real local readonly MCP; no paid calls."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from app.database import SCHEMA, physical_table_name
from app.errors import ModelOutputError, PipelineStageError
from app.model_client import ModelClient
from app.mcp_runtime import LocalMcpClient, create_local_mcp_server
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.single_database_agent import SingleDatabaseAgent
from app.querying.sql_module import SqlQueryModule
from app.runtime_budget import RunBudget, current_budget
from app.security import AccessController
from app.skills import SkillRegistry


class ScriptModel:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def chat_json(self, system, user):
        self.calls.append((system, json.loads(user)))
        if not self.values:
            raise AssertionError('unexpected extra model request')
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def item(qid, sql='SELECT SUM(gmv) AS gmv FROM daily_store_metrics'):
    return {'query_id': qid, 'sql': sql,
            'query_contract': {'metrics': [{'name': 'gmv', 'source_fields': ['daily_store_metrics.gmv'],
                                           'aggregation': 'sum'}]}}


class SqlModuleProtocolTest(unittest.TestCase):
    def test_calculation_missing_column_returns_specific_error_before_sql_execution(self):
        from unittest.mock import patch, Mock
        from tests.test_state_duration import request
        module, model = self.module([])
        module.contract_validator = Mock()
        sql='SELECT item,warehouse,at,seq,delta FROM ledger'
        with patch('app.querying.sql_module.canonical_sql',return_value=(sql,[])):
            with self.assertRaisesRegex(ValueError,'未在SQL最终结果输出：kind'):
                module.validate_candidate(sql=sql,query_contract={},question='state interval',database='fixture',
                    schema_graph={},access_scope={},validation_context={'postprocess':{**request(),'result_id':'query_result'}})
        module.contract_validator.validate.assert_not_called()
        self.assertEqual(model.calls,[])

    def module(self, values):
        model = ScriptModel(values)
        return SqlQueryModule(model, lambda scope: None, SkillRegistry().get('database_query'),
                              DuckDbEngine()), model

    def test_batch_is_one_call_and_failed_subset_repair_keeps_same_protocol(self):
        module, model = self.module([
            {'queries': [item('Q1'), {'query_id': 'Q2', 'query_contract': {}}]},
            {'queries': [item('Q2')]},
        ])
        context = {'database': 'ecommerce_ops', 'schema_graph': {'fields': [{'sql_name': 't.x', 'samples': ['ok']}]},
                   'internal_schema_graph': {'fields': [{'secret_duplicate': True}]},
                   'conditions': {'start': '2026-06-01'}}
        first = module.generate_batch([{'query_id': 'Q1', 'goal': 'GMV'}, {'query_id': 'Q2', 'goal': 'orders'}], context)
        self.assertEqual(set(first['candidates']), {'Q1'})
        self.assertEqual(set(first['errors']), {'Q2'})
        second = module.generate_batch([{'query_id': 'Q2', 'goal': 'orders'}], context,
                                       repair=[{'query_id': 'Q2', 'error': first['errors']['Q2']}])
        self.assertEqual(set(second['candidates']), {'Q2'})
        self.assertEqual(len(model.calls), 2)
        self.assertEqual([q['query_id'] for q in model.calls[1][1]['queries']], ['Q2'])
        self.assertEqual(model.calls[0][1]['schema_graph'], model.calls[1][1]['schema_graph'])
        self.assertEqual(model.calls[0][1]['query_protocol'], model.calls[1][1]['query_protocol'])
        self.assertNotIn('internal_schema_graph', model.calls[0][1])
        self.assertNotIn('failed_queries', model.calls[0][1])

    def test_missing_unknown_and_duplicate_ids_cannot_add_execution_candidates(self):
        module, _ = self.module([{'queries': [item('Q1'), item('Q1'), item('UNAUTHORIZED')]}])
        result = module.generate_batch([{'query_id': 'Q1'}, {'query_id': 'Q2'}], {})
        self.assertEqual(result['candidates'], {})
        self.assertEqual(set(result['errors']), {'Q1', 'Q2'})
        self.assertTrue(result['protocol_errors'])

    def test_unknown_network_response_is_not_retried_inside_module(self):
        module, model = self.module([RuntimeError('connection lost'), {'queries': [item('Q1')]}])
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            module.generate_batch([{'query_id': 'Q1'}], {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(model.values), 1)

    def test_malformed_batch_is_per_query_error_without_inner_format_retry(self):
        module, model = self.module([{'unexpected': []}])
        result = module.generate_batch([{'query_id': 'Q1'}, {'query_id': 'Q2'}], {})
        self.assertEqual(set(result['errors']), {'Q1', 'Q2'})
        self.assertEqual(len(model.calls), 1)

    def test_typed_invalid_json_is_one_per_query_failure_without_inner_retry(self):
        module, model = self.module([ModelOutputError('大模型未返回有效 JSON')])
        result = module.generate_batch([{'query_id': 'Q1'}, {'query_id': 'Q2'}], {})
        self.assertEqual(set(result['errors']), {'Q1', 'Q2'})
        self.assertTrue(all(e['kind'] == 'output_format' for e in result['errors'].values()))
        self.assertEqual(len(model.calls), 1)

    def test_two_invalid_json_responses_use_two_slots_and_never_reach_database(self):
        class InvalidJsonModel(ModelClient):
            def __init__(self):
                self.calls = []

            def chat(self, system, user, **kwargs):
                self.calls.append(json.loads(user))
                return 'not JSON'

        model = InvalidJsonModel()
        calls = []
        client = SimpleNamespace(list_tools=lambda: [{'name': 'query_ecommerce_ops'}],
                                 call_tool=lambda *a: calls.append(a))
        ordinary = SingleDatabaseAgent(model, lambda scope: client,
                                       SkillRegistry().get('database_query'), DuckDbEngine())
        budget = RunBudget('two-bad-json', sql_generation_limit=2)
        token = current_budget.set(budget)
        try:
            with self.assertRaises(PipelineStageError) as failure:
                ordinary.prepare('查询GMV', 'ecommerce_ops', {}, '', {}, {}, {})
        finally:
            current_budget.reset(token)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(budget.direct_sql_generations, {'ecommerce_ops:direct': 2})
        self.assertEqual(calls, [])
        self.assertIn('大模型未返回有效 JSON', str(failure.exception))
        self.assertEqual(model.calls[1]['output_validation_error'], '大模型未返回有效 JSON')

    def test_clarification_and_missing_context_are_distinct_from_bad_sql(self):
        module, _ = self.module([{'queries': [
            {'query_id': 'Q1', 'status': 'clarify', 'clarification': {'question': '哪家店？'}},
            {'query_id': 'Q2', 'status': 'missing_context', 'missing_requests': ['时间字段']},
        ]}])
        result = module.generate_batch([{'query_id': 'Q1'}, {'query_id': 'Q2'}], {})
        self.assertEqual(result['errors']['Q1']['kind'], 'clarification')
        self.assertEqual(result['errors']['Q2']['kind'], 'missing_context')
        self.assertFalse(result['candidates'])

    def test_storage_failure_remains_typed_and_is_not_rewritten(self):
        module, model = self.module([])
        client = SimpleNamespace(call_tool=lambda *a: {'success': False, 'error_kind': 'storage', 'error': 'disk failed'})
        with self.assertRaises(PipelineStageError) as failure:
            module.execute_prepared(database='ecommerce_ops', sql='SELECT 1', access_scope={},
                                    query_contract={}, mcp_client=client)
        self.assertEqual(failure.exception.stage, 'result_storage')
        self.assertEqual(model.calls, [])


class SqlModuleLocalMcpTest(unittest.TestCase):
    def setUp(self):
        self.engine = DuckDbEngine()
        self.scope = AccessController().scoped('demo_current_sales', 'ecommerce_ops')
        table = next(t for t in SCHEMA if t.get('database') == 'ecommerce_ops'
                     and physical_table_name(t) == 'daily_store_metrics')
        self.graph = {'database': 'ecommerce_ops',
                      'tables': [{'id': table['id'], 'name': 'daily_store_metrics', 'database': 'ecommerce_ops'}],
                      'fields': [{**f, 'id': table['id'] + '.' + f['name'], 'table_id': table['id'],
                                  'sql_name': 'daily_store_metrics.' + f['name']} for f in table['fields']], 'joins': []}
        self.client = LocalMcpClient(create_local_mcp_server(self.engine, self.scope))

    def test_ordinary_single_batch_and_investigation_module_share_generation_and_execution(self):
        qid = 'ecommerce_ops:direct'
        model = ScriptModel([{'queries': [item(qid)]}])
        ordinary = SingleDatabaseAgent(model, lambda scope: self.client,
                                       SkillRegistry().get('database_query'), self.engine)
        budget = RunBudget('shared-module-direct')
        token = current_budget.set(budget)
        try:
            result = ordinary.prepare('查询GMV', 'ecommerce_ops', self.graph, '', {}, {}, self.scope.public())
        finally:
            current_budget.reset(token)
        self.assertTrue(result['execution']['success'], result['execution'].get('error'))
        self.assertGreater(result['execution']['rows'][0]['gmv'], 0)
        self.assertEqual(result['sql_generations'], 1)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][1]['queries'][0]['query_id'], qid)
        self.assertIs(ordinary.sql_module.contract_validator, ordinary.contract_validator)

    def test_invalid_physical_field_and_write_never_reach_mcp(self):
        calls = []
        module = SqlQueryModule(ScriptModel([]), lambda scope: self.client,
                                SkillRegistry().get('database_query'), self.engine)
        spy = SimpleNamespace(call_tool=lambda *a: calls.append(a))
        for bad_sql in ['SELECT SUM(nonexistent_gmv) FROM daily_store_metrics',
                        'DELETE FROM daily_store_metrics']:
            with self.subTest(sql=bad_sql), self.assertRaises(ValueError):
                module.execute_candidate(sql=bad_sql, query_contract=item('Q')['query_contract'], question='查询GMV',
                                         database='ecommerce_ops', schema_graph=self.graph,
                                         access_scope=self.scope.public(), mcp_client=spy)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
