"""Configuration reaches real prompt assembly; all model outputs are controlled.

These tests never execute a SQL candidate or call a model provider. They verify
configuration plumbing, not real-model investigation or business correctness.
"""
from concurrent.futures import Future
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.agent_configuration_runtime import (
    configuration_snapshot, configuration_store, current_configuration, default_snapshot,
)
from app import prompt_defaults as defaults
from app.preprocessing import RequestPreprocessor
from app.querying.data_qa_agent import DataQaAgent
from app.querying.models import SqlExecution
from app.querying.response_generator import ResponseGenerator
from app.querying.sql_module import SqlQueryModule
from app.runtime_budget import current_budget
from app.services.short_term_memory import ShortTermMemory
from app.skills import SkillRegistry
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord
from app.workflows.react_prompts import REVIEW_SYSTEM


class RecordingModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, system, user):
        self.calls.append({'system': system, 'user': user})
        if not self.responses:
            raise AssertionError('Unexpected additional model call')
        return deepcopy(self.responses.pop(0))

    def chat(self, system, user):
        self.calls.append({'system': system, 'user': user})
        return '受控摘要'


class DeferredExecutor:
    """Queue without starting a thread so configuration can change before run."""
    def submit(self, function, *args, **kwargs):
        self.queued = (function, args, kwargs)
        return Future()

    def run(self):
        function, args, kwargs = self.queued
        function(*args, **kwargs)


class AgentConfigurationRuntimeTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'overrides.json'
        environment = patch.dict(os.environ, {'AGENT_CONFIGURATION_PATH': str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)
        token = current_configuration.set(None)
        self.addCleanup(current_configuration.reset, token)
        budget_token = current_budget.set(None)
        self.addCleanup(current_budget.reset, budget_token)
        network = patch('socket.socket.connect', side_effect=AssertionError('No network in configuration tests'))
        network.start()
        self.addCleanup(network.stop)

    def save(self, *, prompts=None, skills=None):
        store = configuration_store()
        return store.update(expected_revision=store.snapshot()['revision'],
                            prompts=prompts, skills=skills, actor='configuration-test')

    @staticmethod
    def no_query(*args, **kwargs):
        raise AssertionError('No database execution in configuration tests')

    def sql_module(self, model, skill=None):
        engine = SimpleNamespace(execute=self.no_query, query=self.no_query)
        return SqlQueryModule(model, self.no_query, skill or SkillRegistry().get('database_query'), engine)

    def test_entry_uses_default_then_persisted_override(self):
        response = {'action': 'direct_response', 'response': '受控入口回答'}
        model = RecordingModel(response, response)
        entry = RequestPreprocessor(model)
        entry.prepare('普通交流', '')
        self.assertEqual(model.calls[0]['system'], defaults.PREPROCESS_SYSTEM)
        self.save(prompts={'preprocess': '入口覆盖，JSON格式仍由受控输出满足'})
        entry.prepare('普通交流', '')
        self.assertEqual(model.calls[1]['system'], '入口覆盖，JSON格式仍由受控输出满足')

    def test_sql_first_generation_and_repair_share_overridden_template_and_skill(self):
        self.save(prompts={'sql_query': '统一SQL规则 [[SKILL:database_query]]'},
                  skills={'database_query': '当前SQL Skill'})
        model = RecordingModel({'queries': []}, {'queries': []})
        module = self.sql_module(model)
        requests = [{'query_id': 'A1', 'goal': '只检查组装路径'}]
        module.generate_batch(requests, {'database': 'test'})
        module.generate_batch(requests, {'database': 'test'},
                              repair={'A1': {'error': '受控SQL诊断', 'sql': 'SELECT 1'}})
        self.assertEqual([call['system'] for call in model.calls],
                         ['统一SQL规则 当前SQL Skill', '统一SQL规则 当前SQL Skill'])
        self.assertNotIn('failed_queries', json.loads(model.calls[0]['user']))
        self.assertIn('failed_queries', json.loads(model.calls[1]['user']))

    def test_data_qa_uses_actual_authorized_tools_and_selected_business_methods(self):
        self.save(prompts={'data_qa': '问答 [[SKILL:data_qa]] 工具=[[DISPLAY_TOOLS]]'},
                  skills={'data_qa': '当前报告 Skill'})
        model = RecordingModel({'action': 'answer', 'answer': '受控回答'})
        client = SimpleNamespace(list_tools=lambda: [
            {'name': 'build_bar_chart'}, {'name': 'query_database'}])
        qa = DataQaAgent(model, lambda scope: client, SkillRegistry().get('data_qa'))
        qa.run('解释已有结果', {'business_methods': '本任务实际业务方法'}, {})
        self.assertEqual(model.calls[0]['system'],
                         '问答 当前报告 Skill 工具=[{"name": "build_bar_chart"}]'
                         '\n当前选中的业务Skill规则（必须遵守）：\n本任务实际业务方法')
        self.assertNotIn('query_database', model.calls[0]['system'])

    def test_investigation_report_uses_report_template_and_current_skill(self):
        self.save(prompts={'investigation_report': '调查报告 [[SKILL:data_qa]]'},
                  skills={'data_qa': '当前报告 Skill'})
        qa = DataQaAgent(RecordingModel(), self.no_query, SkillRegistry().get('data_qa'))
        calls = []

        def draft(system, payload):
            calls.append((system, payload))
            return {'markdown': '仅验证配置组装的受控报告', 'visualizations': []}

        qa.draft_investigation('调查目标', {'evidence': []}, {}, call_model=draft)
        self.assertEqual(calls[0][0], '调查报告 当前报告 Skill')
        self.assertEqual(calls[0][1]['mode'], 'investigation_report')

    def test_plain_result_draft_and_review_keep_dynamic_methods(self):
        self.save(prompts={'result_draft': '结果草稿规则', 'simple_review': '简单审核规则'})
        model = RecordingModel({'valid': True, 'analysis': '草稿'},
                               {'valid': True, 'analysis': '审核定稿'})
        result = ResponseGenerator(model).finalize('原问题', SqlExecution('SELECT 1', True), '', '',
                                                   business_methods={'business_diagnosis': '实际选择的方法'})
        methods = '\n当前选中的业务Skill规则：\n实际选择的方法'
        self.assertEqual([call['system'] for call in model.calls],
                         ['结果草稿规则' + methods, '简单审核规则' + methods])
        self.assertEqual(result['analysis'], '审核定稿')

    def test_react_keeps_selected_skill_and_final_review_uses_its_own_rules(self):
        self.save(prompts={'react': '经营决策覆盖', 'review': '最终审核覆盖'})
        harness = ReactInvestigationHarness.__new__(ReactInvestigationHarness)
        harness.model = RecordingModel({'action': {'type': 'deliver'}}, {'action': 'partial'})
        task = ReactRecord({'task_id': 'T1'}, protocol_version=3)
        task.skills = {'business_diagnosis': {'instructions': '已读取的方法', 'version': 'v-test'}}
        harness._call(task, 'react', 'ignored default', {})
        harness._call(task, 'delivery_review', REVIEW_SYSTEM, {})
        self.assertEqual([call['system'] for call in harness.model.calls], [
            '经营决策覆盖\n\n【已加载 Skill：business_diagnosis，版本 v-test】\n已读取的方法',
            '最终审核覆盖'])

    def test_restoring_defaults_updates_components_created_with_old_skill_override(self):
        self.save(skills={'database_query': '启动时SQL覆盖', 'data_qa': '启动时报告覆盖'})
        registry = SkillRegistry()
        sql = self.sql_module(RecordingModel(), registry.get('database_query'))
        qa_model = RecordingModel({'action': 'answer', 'answer': '受控回答'})
        qa = DataQaAgent(qa_model, lambda scope: SimpleNamespace(list_tools=lambda: []), registry.get('data_qa'))
        self.assertIn('启动时SQL覆盖', sql.technical_system('test'))
        self.save(skills={'database_query': None, 'data_qa': None})
        self.assertEqual(sql.technical_system('test'), defaults.SQL_QUERY_SYSTEM.replace(
            '[[SKILL:database_query]]', sql.skill.default_instructions))
        qa.run('解释已有结果', {}, {})
        self.assertNotIn('启动时报告覆盖', qa_model.calls[0]['system'])
        self.assertIn(qa.skill.default_instructions, qa_model.calls[0]['system'])
        self.assertNotIn('default_instructions', qa.skill.public())

    def test_legacy_query_policy_does_not_reuse_startup_override(self):
        harness = ReactInvestigationHarness.__new__(ReactInvestigationHarness)
        harness.query_instructions = '内置旧查询技术正文'
        harness.model = RecordingModel({'action': {'type': 'deliver'}})
        self.save(skills={'database_query': '服务启动时覆盖'})
        token = current_configuration.set(default_snapshot())
        try:
            task = ReactRecord({'task_id': 'legacy'}, protocol_version=1)
            harness._call(task, 'react', '', {})
            system = harness.model.calls[0]['system']
            self.assertIn('内置旧查询技术正文', system)
            self.assertNotIn('服务启动时覆盖', system)
        finally:
            current_configuration.reset(token)

    def test_frozen_context_retains_its_version_after_new_save(self):
        first = self.save(prompts={'preprocess': '旧任务入口'})
        token = current_configuration.set(first)
        try:
            # Save directly to the store, just as a concurrent admin request does.
            self.save(prompts={'preprocess': '后续任务入口'})
            model = RecordingModel({'action': 'direct_response', 'response': '受控回答'})
            RequestPreprocessor(model).prepare('当前任务', '')
            self.assertEqual(model.calls[0]['system'], '旧任务入口')
            self.assertEqual(configuration_snapshot()['revision'], first['revision'])
        finally:
            current_configuration.reset(token)
        self.assertEqual(configuration_snapshot()['prompts']['preprocess'], '后续任务入口')

    def test_queued_summary_keeps_prompt_from_queue_time(self):
        first = self.save(prompts={'memory_summary': '排队时摘要规则'})
        model = RecordingModel()
        memory = ShortTermMemory(model, trigger_tokens=1, batch_tokens=1, min_recent_turns=1)
        memory._executor.shutdown(wait=True)
        queue = DeferredExecutor()
        memory._executor = queue
        token = current_configuration.set(first)
        try:
            turns = [{'task_id': str(index), 'query': '已完成问题', 'analysis': '已完成结果',
                      'columns': [], 'row_count': 0} for index in range(2)]
            self.assertTrue(memory.maybe_schedule('session', turns))
            current_configuration.set(default_snapshot())
            self.save(prompts={'memory_summary': '之后保存的新规则'})
            queue.run()
            self.assertEqual(model.calls[0]['system'], '排队时摘要规则')
        finally:
            current_configuration.reset(token)


if __name__ == '__main__':
    unittest.main()
