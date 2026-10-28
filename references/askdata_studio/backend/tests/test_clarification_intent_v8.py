"""Controlled entry/resume regression; no live model, network or business SQL."""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.config import Settings
from app.models import QueryResult
from app.services.askdata_service import AskDataService
from app.services.session_context import SessionContext


ORIGINAL = '分析六月份相较于5月GMV'
INVESTIGATION = '2026年6月份相较于5月份GMV下降的原因'


def year_result():
    return QueryResult(
        task_id='waiting-year', status='waiting_clarification', route='database_query',
        message='请选择年份', workflow_mode='single_database_agent',
        clarification={
            'parameter': 'year', 'question': '请指定年份', 'reason': '需要明确年份',
            'options': [
                {'id': '2026', 'label': '2026年', 'description': '2026年数据'},
                {'id': '2025', 'label': '2025年', 'description': '2025年数据'},
            ],
        })


class ControlledRouteModel:
    """Outputs test protocol only; does not establish real Flash accuracy."""

    def __init__(self):
        self.questions = []

    def chat_json(self, system, user):
        if '问数系统的请求预处理器' not in system:
            raise AssertionError('No downstream model call is allowed in this control-flow test')
        question = user.split('当前问题：', 1)[1].split('\n近期轻量上下文：', 1)[0]
        self.questions.append(question)
        investigation = question == INVESTIGATION
        return {
            'action': 'database_query', 'confidence': 1, 'reason': '受控路由输出',
            'standalone_query': question, 'rewritten': False,
            'needs_planner': investigation,
            'business_skills': ['business_diagnosis'] if investigation else [],
            'required_questions': [question], 'retrieval': {'metrics': ['GMV']},
        }


class ClarificationChoiceV8Test(unittest.TestCase):
    def test_parameter_only_year_answers_and_ordinals_still_match(self):
        result = year_result()
        for answer, expected in [('2026年', '2026'), ('2026', '2026'), ('就用2026年', '2026'),
                                 ('第一个', '2026'), ('1', '2026'), ('第二个', '2025'), ('选二', '2025')]:
            with self.subTest(answer=answer):
                self.assertEqual(SessionContext.match_clarification(answer, result), expected)

    def test_existing_short_schema_selection_wrapper_still_matches(self):
        result = year_result()
        result.clarification.options[0].id = 'orders_current'
        result.clarification.options[0].label = '本月当前订单'
        self.assertEqual(SessionContext.match_clarification('就用本月当前订单', result), 'orders_current')

    def test_complete_new_question_is_not_a_choice_or_a_free_answer_fallback(self):
        result = year_result()
        for question in [INVESTIGATION, '2026年和2025年比较一下', '我选择2026年，顺便解释下降的原因']:
            with self.subTest(question=question):
                self.assertIsNone(SessionContext.match_clarification(question, result))
        self.assertFalse(AskDataService._free_clarification(result))

    def test_single_value_affordance_remains_free_form(self):
        result = year_result()
        result.clarification.parameter = 'target'
        result.clarification.options = result.clarification.options[:1]
        result.clarification.options[0].id = 'provide_value'
        self.assertTrue(AskDataService._free_clarification(result))


class ClarificationDispatchV8Test(unittest.TestCase):
    def setup_flow(self):
        model = ControlledRouteModel()
        config = Settings(api_key='', session_archive_enabled=False, short_term_summary_enabled=False)
        service = AskDataService(model, SimpleNamespace(config=config))
        calls = {'retrieval': 0, 'ordinary_execution': 0, 'harness_states': []}

        def retrieve(state):
            calls['retrieval'] += 1
            return {'schema_graph': {'tables': [], 'fields': []}, 'schema_context': '',
                    'retrieval': {'hits': []}, 'database_names': ['ecommerce_ops'], 'clarification': None}

        def prepare(state):
            if not (state.get('workspace') or {}).get('confirmed_parameters', {}).get('year'):
                return {'workflow_mode': 'single_database_agent',
                        'clarification': year_result().clarification.model_dump()}
            return {'workflow_mode': 'single_database_agent', 'clarification': None}

        def execute(state):
            calls['ordinary_execution'] += 1
            result = QueryResult(task_id=state['task_id'], status='completed', route='database_query',
                                 message='受控续查完成', workflow_mode='single_database_agent', final_reviewed=True)
            return {'result': result.model_dump()}

        def investigate(state):
            calls['harness_states'].append(json.loads(json.dumps(state, ensure_ascii=False)))
            return QueryResult(task_id=state['task_id'], status='completed', route='database_query',
                               message='受控Harness分派完成', workflow_mode='investigation_harness_v1',
                               final_reviewed=True, required_questions=state['required_questions'])

        with patch.object(service.workflow, '_retrieve_schema', retrieve), \
                patch.object(service.workflow, '_prepare_single_database', prepare), \
                patch.object(service.workflow, '_execute_single_database', execute):
            service.workflow.graph = service.workflow._compile()
        service.workflow.harness.run = investigate
        return service, model, calls

    def test_full_cause_request_reaches_preprocessor_and_harness_without_resuming_old_sql(self):
        service, model, calls = self.setup_flow()
        workspace = {'datasource_id': 'ecommerce_ops'}
        waiting = service.submit(ORIGINAL, 'cause-dispatch', workspace, 'demo_current_sales')
        self.assertEqual(waiting.status, 'waiting_clarification')
        result = service.submit(INVESTIGATION, 'cause-dispatch', workspace, 'demo_current_sales')
        self.assertNotEqual(result.task_id, waiting.task_id, 'A new full goal must not become the old year value')
        self.assertEqual(model.questions, [ORIGINAL, INVESTIGATION])
        self.assertEqual(result.workflow_mode, 'investigation_harness_v1')
        self.assertEqual(calls['ordinary_execution'], 0)
        self.assertEqual(calls['retrieval'], 1, 'The new request must dispatch into Harness before legacy retrieval')
        self.assertEqual(len(calls['harness_states']), 1)
        state = calls['harness_states'][0]
        self.assertEqual(state['query'], INVESTIGATION)
        self.assertEqual(state['standalone_query'], INVESTIGATION)
        self.assertEqual(state['required_questions'], [INVESTIGATION])
        self.assertTrue(state['needs_planner'])
        self.assertEqual(state['business_skills'], ['business_diagnosis'])
        self.assertNotIn('year', state['workspace']['confirmed_parameters'])
        self.assertEqual(service.tasks[waiting.task_id]['result'].status, 'waiting_clarification')

    def test_plain_year_resumes_same_task_and_remaining_budget_without_new_preprocessing(self):
        for answer in ['2026年', '2026', '第一个']:
            with self.subTest(answer=answer):
                service, model, calls = self.setup_flow()
                workspace = {'datasource_id': 'ecommerce_ops'}
                waiting = service.submit(ORIGINAL, 'year-resume', workspace, 'demo_current_sales')
                budget = service.workflow.budgets[waiting.task_id]
                self.assertIsNone(budget.started)
                budget.spent = 12
                budget.tools = 3
                result = service.submit(answer, 'year-resume', workspace, 'demo_current_sales')
                self.assertEqual(result.task_id, waiting.task_id)
                self.assertIs(service.workflow.budgets[result.task_id], budget)
                self.assertGreaterEqual(budget.spent, 12)
                self.assertEqual(budget.tools, 3)
                self.assertIsNone(budget.started)
                self.assertEqual(model.questions, [ORIGINAL])
                self.assertEqual(calls['ordinary_execution'], 1)
                self.assertEqual(calls['harness_states'], [])
                state = service.workflow.graph.get_state(service.workflow.run_config(result.task_id)).values
                self.assertEqual(state['workspace']['confirmed_parameters']['year'], '2026')


if __name__ == '__main__':
    unittest.main()
