"""Pending query checks survive execution dependencies, review and follow-ups."""
import json
from pathlib import Path
import tempfile
import unittest

from app.config import Settings
from app.models import QueryResult
from app.querying.models import SqlExecution
from app.querying.response_generator import ResponseGenerator
from app.services.session_context import SessionContext
import test_harness as fixture


VALIDATION = dict(business_definition_verified=False,
                  pending_checks=[dict(code='distinct_count_not_proven', message='核对上游是否去重')],
                  normalizations=[], sql_summary=dict(tables=['payments']))


class ValidationContextTest(unittest.TestCase):
    def test_pending_checks_reach_next_executor_and_final_reviewer(self):
        helper = fixture.HarnessTest()
        model = fixture.ScriptModel([
            dict(conditions={}, steps=[fixture.step(), fixture.step('S2', ['S1'])]),
            fixture.reviewed(ids=['T1:R1', 'T1:R2'], final=True),
        ])
        first = helper.execution()
        first['tool_trace'] = [dict(query_contract=dict(validation=VALIDATION))]
        harness, calls = helper.harness(model, [first, helper.execution()])
        result = helper.bounded(harness, helper.state())
        self.assertEqual(result.status, 'completed')
        dependency = calls[1][1]['task_context']['dependency_results'][0]
        self.assertEqual(dependency['query_contract']['validation'], VALIDATION)
        review_input = json.loads(model.calls[-1][1])
        self.assertEqual(review_input['evidence'][0]['query_contract']['validation'], VALIDATION)
        self.assertTrue(all(item['final'] for item in result.reviews))

    def test_simple_result_draft_and_review_receive_pending_checks(self):
        model = fixture.ScriptModel([dict(valid=True, analysis='草稿'), dict(valid=True, analysis='已核对上游去重')])
        generator = ResponseGenerator(model, Settings(api_key=''))
        execution = SqlExecution('SELECT 1 AS n', True, ['n'], [{'n': 1}])
        result = generator.finalize('查订单量', execution, '', '', query_validation=VALIDATION)
        self.assertIn('distinct_count_not_proven', model.calls[0][1])
        self.assertEqual(json.loads(model.calls[-1][1])['query_validation'], VALIDATION)
        self.assertEqual(result['analysis'], '已核对上游去重')

    def test_pending_checks_survive_archive_and_follow_up_without_cross_session_leak(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Settings(api_key='', session_archive_enabled=True,
                              session_archive_path=str(Path(folder) / 'sessions.db'), short_term_summary_enabled=False)
            model = fixture.ScriptModel([])
            context = SessionContext(model, config)
            result = QueryResult(task_id='trace-1', status='completed', route='database_query', message='受控结果',
                                 sql='SELECT 1 AS n', columns=['n'], rows=[{'n': 1}],
                                 tool_calls=[dict(arguments=dict(query_contract=dict(validation=VALIDATION)))])
            context.remember('session-a', result, '查订单量', {})
            restored = SessionContext(model, config)
            recent = json.loads(restored.recent_result_context('session-a'))
            self.assertEqual(recent['query_validations'], [VALIDATION])
            tables, _ = restored.analysis_context('session-a', {'analysis_table_ids': ['trace-1']})
            self.assertEqual(json.loads(tables)[0]['query_validations'], [VALIDATION])
            supplied = dict(analysis_table_ids=['trace-1'], analysis_tables=[dict(task_id='trace-1', columns=['n'], rows=[{'n': 1}])])
            tables, _ = restored.analysis_context('session-b', supplied)
            self.assertEqual(json.loads(tables)[0]['query_validations'], [])
            self.assertFalse(json.loads(tables)[0]['validation_metadata_available'])
            self.assertEqual(model.calls, [])

    def test_harness_evidence_metadata_survives_selected_historical_table(self):
        context = SessionContext(fixture.ScriptModel([]), Settings(api_key='', session_archive_enabled=False, short_term_summary_enabled=False))
        result = QueryResult(task_id='harness-1', status='completed', route='database_query', message='受控结果',
                             sql='SELECT 1 AS n', columns=['n'], rows=[{'n': 1}],
                             evidence=[dict(check_status='superseded', query_contract=dict(validation={'pending_checks': ['obsolete']})),
                                       dict(check_status='reviewed', query_contract=dict(validation=VALIDATION))])
        context.remember('s1', result, '查订单量', {})
        tables, _ = context.analysis_context('s1', {'analysis_table_ids': ['harness-1']})
        self.assertEqual(json.loads(tables)[0]['query_validations'], [VALIDATION])
        self.assertTrue(json.loads(tables)[0]['validation_metadata_available'])
