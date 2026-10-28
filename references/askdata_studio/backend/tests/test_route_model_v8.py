"""Separate entry routing from expensive downstream models, without network."""
from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from app.config import Settings
from app.model_client import ModelClient
from app.runtime_budget import RunBudget, current_budget
from app.workflows.query_graph import QueryWorkflow


ROUTE_OUTPUT = {
    'action': 'database_query', 'confidence': 0.95, 'reason': '查询单项指标',
    'needs_planner': False, 'business_skills': [], 'required_questions': ['查询本月GMV'],
    'standalone_query': '查询本月GMV', 'rewritten': False, 'retrieval': {'metrics': ['GMV']},
}


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode('utf-8')


class MinimalProtocolClient:
    """Existing test clients need not accept model keywords or have config."""
    def __init__(self):
        self.calls = 0

    def chat_json(self, system, user):
        self.calls += 1
        return dict(ROUTE_OUTPUT)


class RouteModelV8Test(unittest.TestCase):
    def configuration(self, **changes):
        return Settings(api_key='offline-test-credential', llm_model='qwen3.7-plus',
                        route_model=changes.get('route_model', 'qwen3.7-flash'),
                        llm_enable_thinking=True, max_retries=0, timeout=17)

    def test_only_entry_client_is_reconfigured_and_provider_settings_are_preserved(self):
        config = self.configuration()
        main = ModelClient(config)
        workflow = QueryWorkflow(main, object(), config)
        route = workflow.route_model_client
        self.assertIsNot(route, main)
        self.assertEqual(route.config, replace(config, llm_model='qwen3.7-flash', llm_enable_thinking=False))
        self.assertEqual(main.config.llm_model, 'qwen3.7-plus')
        self.assertTrue(main.config.llm_enable_thinking)
        self.assertIs(workflow.preprocessor.model_client, route)
        self.assertIs(workflow.model_client, main)
        self.assertIs(workflow.single_database_agent.model_client, main)
        self.assertIs(workflow.data_qa_agent.model_client, main)
        self.assertIs(workflow.response_generator.model_client, main)
        self.assertIs(workflow.harness.model, main)
        self.assertEqual(config.public_status()['route_model'], 'qwen3.7-flash')

    def test_actual_wire_payloads_and_shared_budget_record_only_routing_as_flash(self):
        config = self.configuration()
        main = ModelClient(config)
        workflow = QueryWorkflow(main, object(), config)
        sent = []

        def respond(request, **_kwargs):
            payload = json.loads(request.data)
            sent.append((request.full_url, payload))
            if request.full_url == config.embeddings_url:
                return Response({'data': [{'index': 0, 'embedding': [1, 0]}]})
            if request.full_url == config.rerank_url:
                return Response({'results': [{'index': 0, 'relevance_score': 0.9}]})
            return Response({'choices': [{'message': {'content': json.dumps(ROUTE_OUTPUT)}}],
                             'usage': {'input_tokens': 5, 'output_tokens': 3, 'total_tokens': 8}})

        budget = RunBudget('offline-route-model-v8')
        token = current_budget.set(budget)
        budget.start()
        try:
            with patch('app.model_client.urllib.request.urlopen', side_effect=respond):
                decision = workflow.preprocessor.prepare('查询本月GMV', '')
                budget.model_node = 'downstream-check'
                main.chat('独立下游节点', '离线检查')
                main.embed(['公开字段'])
                main.rerank('GMV', ['公开字段'], 1)
        finally:
            budget.pause()
            current_budget.reset(token)
        self.assertEqual(decision.action, 'database_query')
        self.assertEqual([payload['model'] for _, payload in sent],
                         ['qwen3.7-flash', 'qwen3.7-plus', config.embedding_model, config.rerank_model])
        self.assertIs(sent[0][1]['enable_thinking'], False)
        self.assertIs(sent[1][1]['enable_thinking'], True)
        self.assertEqual(budget.model_usage[0]['node'], 'preprocess')
        self.assertEqual(budget.model_usage[0]['model'], 'qwen3.7-flash')
        self.assertEqual(budget.model_usage[1]['model'], 'qwen3.7-plus')
        self.assertEqual(budget.model_calls, {'chat': 2, 'embedding': 1, 'rerank': 1})

    def test_empty_override_reuses_main_client_and_effective_status(self):
        config = self.configuration(route_model='  ')
        main = ModelClient(config)
        workflow = QueryWorkflow(main, object(), config)
        self.assertIs(workflow.route_model_client, main)
        self.assertIs(workflow.preprocessor.model_client, main)
        self.assertEqual(config.public_status()['route_model'], 'qwen3.7-plus')

    def test_existing_injected_clients_keep_the_two_argument_protocol(self):
        fake = MinimalProtocolClient()
        workflow = QueryWorkflow(fake, object(), self.configuration())
        result = workflow.preprocessor.prepare('查询本月GMV', '')
        self.assertEqual(fake.calls, 1)
        self.assertEqual(result.action, 'database_query')
        self.assertIs(workflow.route_model_client, fake)
        self.assertIs(workflow.single_database_agent.model_client, fake)


if __name__ == '__main__':
    unittest.main()
