"""Declared entry clarification uses durable main-agent tasks, not a fake answer.

Controlled model protocol; real service, task persistence and resume bookkeeping.
No network, schema retrieval, database query or private acceptance data.
"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.models import QueryResult
from app.preprocessing import RequestPreprocessor
from app.runtime_budget import current_budget
from app.services.askdata_service import AskDataService
from app.workflows.query_graph import QueryWorkflow


QUESTION = '最近怎么下滑了？'


def entry_output():
    return {'action': 'direct_response', 'confidence': .9, 'reason': '缺少指标和明确比较期间',
        'needs_investigation': False, 'response_type': 'clarification',
        'response': '请补充指标和比较期间。', 'required_questions': ['请补充指标和比较期间。'],
        'task_understanding': {'goal': '', 'scope': {'kind': 'unknown', 'name_or_id': '', 'source_text': ''},
                               'period': '', 'baseline': '', 'known_findings': [], 'questions': []}}


def clarify(parameter, question):
    return {'action_reason': '保留原经营目标并确认缺失参数',
            'action': {'type': 'clarify', 'parameter': parameter, 'question': question,
                       'reason': '当前条件不足以确定应核实的真实数据'}}


class ControlledModel:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def chat_json(self, system, user):
        budget = current_budget.get()
        node = budget.model_node if budget else None
        self.calls.append({'node': node, 'user': user})
        if budget:
            budget.model_calls['chat'] = budget.model_calls.get('chat', 0) + 1
            budget.model_usage.append({'node': node, 'kind': 'chat', 'model': 'controlled-offline',
                                      'status': 'success', 'usage': {'total_tokens': 1}})
        if not self.outputs:
            raise AssertionError('No extra routing, SQL or final-answer call is expected')
        return deepcopy(self.outputs.pop(0))


class EntryClarificationHandoffTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Settings(api_key='', session_archive_enabled=False, short_term_summary_enabled=False,
                               harness_state_path=str(self.root / 'tasks.sqlite3'))

    def service(self, model):
        service = AskDataService(model, SimpleNamespace(config=self.config))
        service.workflow._retrieve_schema = Mock(side_effect=AssertionError('No schema retrieval before clarification'))
        service.workflow.database_engine.execute = Mock(side_effect=AssertionError('No SQL before clarification'))
        return service

    def test_declared_clarification_preserves_unknown_goal_without_keyword_guessing(self):
        for question in [QUESTION, '这个变化要怎么看', '比较对象还没选好', 'Which scope should I use?']:
            with self.subTest(question=question):
                prepared = RequestPreprocessor(ControlledModel([]))._parse(entry_output(), question)
                self.assertTrue(prepared.needs_planner)
                self.assertEqual(prepared.required_questions, [question])
                self.assertEqual(prepared.task_understanding['goal'], question)
                self.assertEqual(prepared.task_understanding['period'], '')
                self.assertEqual(prepared.task_understanding['baseline'], '')
                self.assertEqual(prepared.retrieval.metrics, [])

    def test_ordinary_answer_and_parameter_complete_lookup_keep_existing_route(self):
        parser = RequestPreprocessor(ControlledModel([]))
        answer = parser._parse({'action': 'direct_response', 'response_type': 'answer',
                               'response': '您好，需要帮助吗？'}, '你好')
        query = parser._parse({'action': 'database_query', 'response_type': 'answer',
                              'standalone_query': '明确期间的订单数'}, '明确期间的订单数')
        self.assertFalse(answer.needs_planner)
        self.assertFalse(query.needs_planner)
        self.assertEqual(answer.action, 'direct_response')
        self.assertEqual(query.action, 'database_query')

    def test_missing_period_answer_can_remain_waiting_for_metric_in_same_task_and_budget(self):
        model = ControlledModel([entry_output(), clarify('other', '期间已明确，您指哪个指标下滑？')])
        service = self.service(model)
        first = service.submit(QUESTION, 'entry-clarify', {'datasource_id': 'ecommerce_ops'}, 'demo_current_sales')
        self.assertEqual(first.status, 'waiting_clarification')
        self.assertEqual(first.workflow_mode, 'investigation_react_v1')
        self.assertIsNotNone(first.clarification)
        self.assertEqual(first.required_questions, [QUESTION])
        saved = service.workflow.investigation_store.load_task(first.task_id, 'demo_current_sales')
        self.assertEqual(saved['payload']['final_status'], 'waiting_clarification')
        budget = service.workflow.budgets[first.task_id]
        prior_usage = deepcopy(budget.model_usage)
        budget.spent = 12
        budget.tools = 3
        second = service.clarify(first.task_id, '7月和6月比', user_id='demo_current_sales')
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(second.status, 'waiting_clarification')
        self.assertIn('哪个指标', second.clarification.question)
        self.assertIs(service.workflow.budgets[first.task_id], budget)
        self.assertGreaterEqual(budget.spent, 12)
        self.assertEqual(budget.tools, 3)
        self.assertEqual(budget.model_usage[:len(prior_usage)], prior_usage)
        self.assertEqual([c['node'] for c in model.calls], ['preprocess', 'react'])
        payload = json.loads(model.calls[-1]['user'])
        self.assertEqual(payload['task']['question'], QUESTION)
        self.assertEqual(payload['task']['scope_and_parameters']['other'], '7月和6月比')
        self.assertEqual(payload['task']['contextual_findings']['period'], '', '补充原话不被代码改成猜测日期')
        self.assertEqual(service.workflow.harness.records[first.task_id].metric_pool, {})
        service.workflow._retrieve_schema.assert_not_called()
        service.workflow.database_engine.execute.assert_not_called()

    def test_restart_restores_waiting_goal_and_usage_without_repeating_entry(self):
        initial = ControlledModel([entry_output(), clarify('time_range', '请明确比较期间。')])
        service = self.service(initial)
        first = service.submit(QUESTION, 'restart-entry', {'datasource_id': 'ecommerce_ops'}, 'demo_current_sales')
        saved = service.workflow.investigation_store.load_task(first.task_id, 'demo_current_sales')
        resumed_model = ControlledModel([clarify('other', '还需要明确哪个指标。')])
        restarted = self.service(resumed_model)
        restored = restarted.get_task(first.task_id, 'demo_current_sales')
        self.assertEqual(restored.status, 'waiting_clarification')
        self.assertEqual(resumed_model.calls, [])
        result = restarted.clarify(first.task_id, '7月和6月比', user_id='demo_current_sales')
        self.assertEqual(result.task_id, first.task_id)
        self.assertEqual(result.status, 'waiting_clarification')
        self.assertEqual([c['node'] for c in resumed_model.calls], ['react'])
        budget = restarted.workflow.budgets[first.task_id]
        self.assertEqual(budget.model_usage[:len(saved['budget']['model_usage'])], saved['budget']['model_usage'])
        self.assertGreaterEqual(budget.elapsed(), saved['budget']['spent'])
        self.assertEqual(restarted.workflow.harness.records[first.task_id].state['query'], QUESTION)

    def test_unbound_entry_keeps_existing_reply_without_inventing_a_persistence_source(self):
        model = ControlledModel([entry_output()])
        service = self.service(model)
        with patch.object(service.workflow.harness, 'run', side_effect=AssertionError('Unbound source cannot persist')):
            result = service.submit(QUESTION, 'unbound-entry', user_id='demo_current_sales')
        self.assertEqual(result.status, 'completed', '旧入口回复状态，不是已完成的经营调查')
        self.assertEqual(result.workflow_mode, 'direct_response')
        self.assertIsNone(result.datasource_id)
        self.assertIsNone(result.clarification)
        self.assertEqual([call['node'] for call in model.calls], ['preprocess'])
        event = next(item for item in result.execution_log if item['stage'] == 'entry_clarification')
        self.assertEqual(event['mode'], 'legacy_unbound')
        self.assertFalse(event['durable_investigation'])
        self.assertIsNone(service.workflow.investigation_store.load_task(result.task_id, 'demo_current_sales'))
        service.workflow._retrieve_schema.assert_not_called()
        service.workflow.database_engine.execute.assert_not_called()

    def test_bound_direct_response_cannot_complete_a_server_generated_clarification(self):
        workflow = QueryWorkflow.__new__(QueryWorkflow)
        result = QueryResult(task_id='T', status='waiting_clarification', route='database_query',
                             message='请补充对象', workflow_mode='investigation_react_v1')
        workflow.harness = SimpleNamespace(run=Mock(return_value=result))
        state = {'task_id': 'T', 'query': '继续看上一份结果', 'datasource_id': 'ecommerce_ops',
                 'intent': {'response_type': 'clarification'},
                 'direct_response': '请指定引用结果。'}
        with patch('app.workflows.query_graph.ResultBuilder.direct_response', side_effect=AssertionError('Not a completed reply')):
            output = workflow._respond_directly(state)
        self.assertEqual(output['result']['status'], 'waiting_clarification')
        workflow.harness.run.assert_called_once_with(state)


if __name__ == '__main__':
    unittest.main()
