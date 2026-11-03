"""Evidence aliases must not discard already retrieved authoritative definitions."""
from types import SimpleNamespace
import unittest

from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord


class SemanticContextTest(unittest.TestCase):
    def context(self, fields, understanding=None):
        # Context assembly itself does not execute SQL or use a model.
        harness = object.__new__(ReactInvestigationHarness)
        harness.workflow = SimpleNamespace()
        task = ReactRecord(state={'task_id': 'context-test', 'query': '解释变化',
                                  'datasource_id': 'context_fixture', 'workspace': {},
                                  'task_understanding': understanding or {},
                                  'standalone_query': '改写问题含额外调查安排'})
        task.metric_pool = {
            'sessions_count': {'id': 'sessions_count', 'label': '访问会话数',
                               'definition': '不同session_id；不是用户数。',
                               'required_fields': ['sessions.session_id'], 'unit': 'sessions'},
            'gmv': {'id': 'gmv', 'definition': '金额',
                    'required_fields': ['orders.paid_amount'], 'unit': 'cents'},
        }
        task.evidence = [{'id': 'context-test:R1', 'check_status': 'basic_valid',
                          'rows': [], 'columns': [], 'complete': True, 'stored_rows': 0,
                          'query_contract': {'metrics': [{'name': 'physical_alias',
                                                         'source_fields': fields}]}}]
        return harness._context(task, closing=True)

    def test_physical_alias_retains_related_authoritative_definition(self):
        payload = self.context(['sessions.session_id'])
        metrics = payload['semantics']['registered_metrics']
        self.assertEqual([item['id'] for item in metrics], ['sessions_count'])
        self.assertIn('不是用户', metrics[0]['definition'])
        # Relevance does not rewrite/certify the result's declared metric.
        self.assertEqual(payload['evidence'][0]['query_contract']['metrics'][0]['name'],
                         'physical_alias')

    def test_unrelated_definitions_are_not_injected(self):
        payload = self.context(['unknown_table.value'])
        self.assertEqual(payload['semantics']['registered_metrics'], [])

    def test_declared_goal_precedes_retrieval_question_rewrite(self):
        payload = self.context([], {'goal': '解释用户指定期间的变化'})
        self.assertEqual(payload['task']['goal'], '解释用户指定期间的变化')
        self.assertEqual(payload['task']['question'], '解释变化')

    def test_final_reviewer_keeps_related_join_grains_that_draft_does_not_need(self):
        from tests.test_query_context import bridge_graph
        source = bridge_graph()
        source['tables'][0].update(primary_key=['key'], grain='one event')
        source['joins'][0].update(cardinality='1:N', duplicate_risk='Parent event repeats per child.')
        harness = object.__new__(ReactInvestigationHarness)
        harness.workflow = SimpleNamespace()
        task = ReactRecord(state={'task_id': 'T', 'query': '解释变化', 'datasource_id': 'test',
            'workspace': {}, 'schema_graph': source, 'access_scope': {'user_id': 'u',
                'allowed_databases': ['test'], 'allowed_tables': ['a', 'bridge', 'c']}}, protocol_version=3)
        task.field_pool = {f['sql_name']: f for f in source['fields']}
        task.table_pool = {t['id']: t for t in source['tables']}
        task.evidence = [{'id': 'T:R1', 'check_status': 'basic_valid', 'rows': [], 'columns': [],
            'complete': True, 'stored_rows': 0, 'query_contract': {'metrics': [
                {'name': 'events', 'source_fields': ['a.amount', 'c.name']}]}}]
        reviewer = harness._context(task, closing=True, role='review')
        graph = reviewer['semantics']['schema_graph']
        self.assertEqual({t['name'] for t in graph['tables']}, {'a', 'bridge', 'c'})
        self.assertEqual(next(t for t in graph['tables'] if t['name'] == 'a')['primary_key'], ['key'])
        self.assertEqual(graph['joins'][0]['cardinality'], '1:N')
        self.assertNotIn('unrelated', str(graph))
        self.assertNotIn('schema_graph', harness._context(task, closing=True)['semantics'])


if __name__ == '__main__':
    unittest.main()
