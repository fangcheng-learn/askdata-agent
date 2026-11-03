"""Prompt examples exercise actual protocols, without model or database calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.agent_configuration_runtime import current_configuration, default_snapshot
from app.runtime_budget import BudgetStop, current_budget
from app.services.agent_configuration_catalog import configuration_catalog
from app.skills import SkillRegistry
from app.workflows.investigation_progress import merge_progress_records
from app.workflows.progress_protocol import ProgressUpdate, normalize_progress
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord
from app.workflows.react_prompts import BUSINESS_REACT_SYSTEM


class BusinessReactPromptExamplesTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {
            'AGENT_CONFIGURATION_PATH': str(Path(temporary.name) / 'overrides.json')})
        environment.start()
        self.addCleanup(environment.stop)
        configuration_token = current_configuration.set(None)
        self.addCleanup(current_configuration.reset, configuration_token)
        budget_token = current_budget.set(None)
        self.addCleanup(current_budget.reset, budget_token)
        network = patch('socket.socket.connect', side_effect=AssertionError('Offline prompt protocol test'))
        network.start()
        self.addCleanup(network.stop)
        self.examples = [json.loads(line) for line in BUSINESS_REACT_SYSTEM.splitlines()
                         if line.startswith('{')]
        self.original_question = {'id': 'Q1', 'question': '核实并解释指定范围的经营变化',
            'importance': 'core', 'status': 'open', 'original': True, 'evidence_ids': []}

    def harness_and_task(self):
        harness = ReactInvestigationHarness.__new__(ReactInvestigationHarness)
        harness.workflow = SimpleNamespace(skills=SkillRegistry())
        harness.model = SimpleNamespace(chat_json=lambda *args, **kwargs: self.fail('No model call'))
        harness._persist = lambda *args, **kwargs: None
        harness._protocol_error = lambda *args, **kwargs: self.fail('Prompt example failed actual action validation')
        task = ReactRecord({'task_id': 'example', 'datasource_id': 'test-source',
            'access_scope': {'user_id': 'test-user'}, 'query': self.original_question['question']},
            protocol_version=3)
        task.progress['core_questions'] = [deepcopy(self.original_question)]
        return harness, task

    def test_start_examples_are_complete_json_decisions_with_actual_progress_schema(self):
        self.assertEqual(len(self.examples), 3)
        for example in self.examples[:2]:
            self.assertEqual(set(example), {'action_reason', 'progress', 'action'})
            self.assertTrue(example['action_reason'].strip())
            ProgressUpdate.model_validate(example['progress'])
        self.assertEqual([example['action']['type'] for example in self.examples[:2]],
                         ['load_skill', 'query_batch'])

    def test_load_skill_example_uses_registered_body_and_actual_dispatch(self):
        example = self.examples[0]
        harness, task = self.harness_and_task()
        action = example['action']
        registered = {item['name'] for item in harness.workflow.skills.list()}
        self.assertTrue(set(action['names']) <= registered)
        harness._dispatch(task, deepcopy(action), example['action_reason'])
        self.assertEqual(task.observation['type'], 'skills_loaded')
        self.assertEqual(task.actions[0]['status'], 'completed')
        for name in action['names']:
            self.assertEqual(task.skills[name]['instructions'], harness.workflow.skills.get(name).instructions)
            self.assertTrue(task.skills[name]['instructions'].strip())

    def test_query_first_example_passes_real_batch_validation_with_no_business_skill(self):
        example = self.examples[1]
        harness, task = self.harness_and_task()
        prepared = []

        def stop_after_action_validation(record, requests):
            prepared.extend(deepcopy(requests))
            raise BudgetStop('Prompt example accepted; stop before SQL/model execution')

        harness._prepare_sql_context = stop_after_action_validation
        harness._sql_module = lambda: self.fail('No SQL module/model call in prompt validation')
        with self.assertRaisesRegex(BudgetStop, 'stop before SQL/model execution'):
            harness._dispatch(task, deepcopy(example['action']), example['action_reason'])
        self.assertFalse(task.skills)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]['goal'], example['action']['queries'][0]['goal'])
        self.assertEqual(task.actions[0]['sql_generations'], 0)
        for query in prepared:
            self.assertNotIn('sql', query)
            self.assertNotIn('query_contract', query)
        self.assertEqual(task.actions[0]['question_ids'], ['Q1'])

    def test_result_template_uses_real_ids_and_actual_progress_merge_contract(self):
        result_id = 'example:R1'
        substitutions = {
            '<真实结果中已核实的观察>': '已取得当前比较结果，尚不足以解释原因',
            '<目录中的完整真实R结果ID>': result_id,
            '<适用对象和期间>': '当前授权范围及已确认期间',
            '<调用方原问题ID>': self.original_question['id'],
            '<完整真实R结果ID>': result_id,
            '<已核实什么，原因回答还缺什么>': '变化已核实，主要解释仍待检验',
            '<需要检验的解释>': '当前变化可能与结构变化有关',
            '<能支持或反驳此解释的下一项证据>': '同口径构成对比与反证',
        }
        template = json.dumps(self.examples[2], ensure_ascii=False)
        for placeholder, replacement in substitutions.items():
            template = template.replace(placeholder, replacement)
        self.assertNotIn('<', template)
        changes = json.loads(template)
        ProgressUpdate.model_validate(changes)
        existing = {'core_questions': [deepcopy(self.original_question)], 'facts': [], 'explanations': []}
        normalized = normalize_progress(changes, existing_progress=existing,
            evidence=[{'id': result_id, 'check_status': 'basic_valid'}])
        self.assertFalse(normalized['rejected_fields'], normalized['rejected_fields'])
        updated = merge_progress_records(existing, normalized['progress'], {result_id})
        self.assertEqual(updated['core_questions'][0]['id'], 'Q1')
        self.assertEqual(updated['core_questions'][0]['question'], self.original_question['question'])
        self.assertEqual(updated['core_questions'][0]['importance'], 'core')
        self.assertEqual(updated['core_questions'][0]['status'], 'open')
        self.assertEqual(updated['core_questions'][0]['evidence_ids'], [result_id])
        self.assertEqual(updated['explanations'][0]['status'], 'open')
        self.assertFalse(updated['explanations'][0]['supporting_evidence_ids'])
        self.assertEqual(normalized['progress']['facts'][0]['evidence_ids'], [result_id])

    def test_unsubstituted_placeholders_cannot_become_fact_evidence(self):
        existing = {'core_questions': [deepcopy(self.original_question)], 'facts': []}
        normalized = normalize_progress(self.examples[2], existing_progress=existing,
            evidence=[{'id': 'example:R1', 'check_status': 'basic_valid'}])
        self.assertTrue(normalized['rejected_fields'])
        self.assertFalse(normalized['progress']['facts'])

    def test_configuration_page_and_runtime_share_current_default(self):
        catalog = configuration_catalog(snapshot=default_snapshot())
        prompt = next(entry for entry in catalog['prompts'] if entry['id'] == 'react')
        self.assertEqual(prompt['default_text'], BUSINESS_REACT_SYSTEM)
        self.assertEqual(prompt['effective_text'], BUSINESS_REACT_SYSTEM)
        self.assertFalse(prompt['overridden'])
        self.assertIn('明确的事实核实可先query_batch', prompt['effective_text'])
        self.assertNotIn('先load_skill实际读取白名单方法', prompt['effective_text'])


if __name__ == '__main__':
    unittest.main()
