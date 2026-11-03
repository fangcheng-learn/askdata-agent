"""Business input/protocol regressions; no external model, database or MCP."""
import copy
import unittest

from app.preprocessing import RequestPreprocessor
from app.querying.data_qa_agent import DataQaAgent
from app.skills import SkillRegistry
from app.workflows.react_prompts import BUSINESS_REACT_SYSTEM, REVIEW_SYSTEM


class BusinessReportingV3Test(unittest.TestCase):
    def test_investigation_keeps_original_goal_and_only_grounded_user_findings(self):
        question = '7月GMV为什么比6月少了？感觉销量还可以啊'
        preprocessor = RequestPreprocessor(None)
        result = preprocessor._parse({
            'action': 'database_query', 'needs_investigation': True,
            'standalone_query': '在销量未下降的前提下排查折扣退款，用户存在认知偏差',
            'rewritten': True, 'required_questions': ['退款和折扣是不是原因？'],
            'task_understanding': {'goal': '证明用户感觉有误', 'period': '2026年6月和7月',
                'scope': '店铺A', 'questions': ['退款是否异常'],
                'known_findings': ['存在认知偏差', '感觉销量还可以啊',
                    {'quote': '6月店铺A', 'status': 'verified'},
                    {'quote': '销量未下降', 'source': 'current_user_message'}]},
        }, question, '此前范围为6月店铺A')
        self.assertEqual(result.standalone_query, question)
        self.assertFalse(result.rewritten)
        self.assertEqual(result.required_questions, [question])
        self.assertEqual(result.task_understanding['goal'], question)
        self.assertEqual(result.task_understanding['scope'], '店铺A')
        self.assertEqual(result.task_understanding['period'], '2026年6月和7月')
        findings = result.task_understanding['known_findings']
        self.assertEqual([item['quote'] for item in findings], ['感觉销量还可以啊', '6月店铺A'])
        self.assertTrue(all(item['status'] == 'unverified' for item in findings))
        self.assertEqual(findings[1]['source'], 'recent_context')

    def agent_and_evidence(self):
        def no_tools(*args):
            raise AssertionError('Report composition must not call tools')
        return DataQaAgent(None, no_tools, SkillRegistry().get('data_qa')), [
            {'id': 'task:R1', 'version': 'v1', 'task_id': 'task', 'datasource_id': 'source',
             'check_status': 'basic_valid', 'complete': True, 'stored_rows': 2, 'total_rows': 2,
             'columns': ['month', 'value'], 'rows': [{'month': '6月', 'value': 100}, {'month': '7月', 'value': 80}]}]

    def test_report_schema_is_actual_and_version_is_mechanically_bound(self):
        agent, evidence = self.agent_and_evidence()
        chart = {'type': 'bar', 'title': '对比', 'source_result_id': 'task:R1',
                 'category_field': 'month', 'value_field': 'value'}
        calls = []
        def call(system, payload):
            calls.append((system, payload))
            return {'draft': '变化[task:R1]', 'visualizations': [chart]}
        result = agent.draft_investigation('为什么变化', {'evidence': evidence},
            {'task_id': 'task', 'datasource_id': 'source'}, call_model=call)
        self.assertEqual(len(calls), 1)
        self.assertIn('visualization_schema', calls[0][1]['output_protocol'])
        self.assertEqual(result.report.visualizations[0].source_version, 'v1')
        self.assertEqual(result.diagnostics, [])
        self.assertNotIn('source_version', chart, 'The provider output is not silently mutated')
        self.assertIn('正向抵消', calls[0][0])
        self.assertIn('先用一至三段给当前结论', calls[0][0])
        self.assertIn('numeric_reading_view', calls[0][1]['context'])

    def test_invalid_chart_records_actual_path_and_candidate_without_another_call(self):
        agent, evidence = self.agent_and_evidence()
        chart = {'type': 'line', 'title': '不支持的图', 'source_result_id': 'task:R1',
                 'category_field': 'month', 'value_field': 'value'}
        result = agent.draft_investigation('为什么变化', {'evidence': evidence}, {},
            call_model=lambda *_: {'draft': '变化[task:R1]', 'visualizations': [chart]})
        self.assertEqual(result.report.visualizations, [])
        self.assertEqual(result.diagnostics[0]['errors'][0]['loc'], ('type',))
        self.assertEqual(result.diagnostics[0]['errors'][0]['type'], 'literal_error')
        self.assertIn('line', result.diagnostics[0]['candidate_json'])
        self.assertIn('type', result.warnings[0])

    def test_explicit_wrong_or_blank_version_is_not_overwritten(self):
        agent, evidence = self.agent_and_evidence()
        for version in ('wrong', ''):
            with self.subTest(version=version):
                chart = {'type': 'bar', 'title': '对比', 'source_result_id': 'task:R1',
                         'source_version': version, 'category_field': 'month', 'value_field': 'value'}
                result = agent.draft_investigation('为什么变化', {'evidence': copy.deepcopy(evidence)}, {},
                    call_model=lambda *_: {'draft': '变化[task:R1]', 'visualizations': [chart]})
                self.assertEqual(result.report.visualizations, [])
                self.assertTrue(result.diagnostics)

    def test_core_gap_and_competing_explanations_are_part_of_existing_calls(self):
        self.assertIn('progress.core_questions', BUSINESS_REACT_SYSTEM)
        self.assertIn('progress.explanations', BUSINESS_REACT_SYSTEM)
        self.assertIn('首次＋2次修复', BUSINESS_REACT_SYSTEM)
        self.assertIn('question_assessments', REVIEW_SYSTEM)
        self.assertIn('explanation_coverage_ack', REVIEW_SYSTEM)
        self.assertIn('blocked', REVIEW_SYSTEM)
        self.assertIn('不要求唯一根因', BUSINESS_REACT_SYSTEM + REVIEW_SYSTEM)


if __name__ == '__main__':
    unittest.main()
