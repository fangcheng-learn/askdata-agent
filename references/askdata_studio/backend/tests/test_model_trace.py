"""Private model trace contracts exercised through the real client, with no network."""
from copy import deepcopy
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from app.agent_configuration_runtime import current_configuration, default_snapshot
from app.config import Settings
from app.model_client import ModelClient, ModelRequestNotSent
from app.model_trace import ModelTrace
from app.querying.sql_module import SqlQueryModule
from app.runtime_budget import RunBudget, current_budget
from app.skills import SkillRegistry
from app.structured_output import using_output_schema


class Response:
    def __init__(self, body):
        self.body = body.encode('utf-8') if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def provider_body(content='{"ok":true}', **updates):
    value = {'choices': [{'message': {'content': content,
                                      'reasoning_content': 'provider-returned-reasoning-marker'}}],
             'usage': {'prompt_tokens': 13, 'completion_tokens': 7, 'total_tokens': 20}}
    value.update(updates)
    return json.dumps(value, ensure_ascii=False, indent=2)


class ModelTraceTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name) / 'private-traces'
        self.key = 'test-only-trace-key-9842'
        token = current_budget.set(None)
        self.addCleanup(current_budget.reset, token)
        config_token = current_configuration.set(default_snapshot())
        self.addCleanup(current_configuration.reset, config_token)

    def client(self, **overrides):
        values = {'api_key': self.key, 'model_trace_dir': str(self.directory), 'max_retries': 2}
        values.update(overrides)
        return ModelClient(Settings(**values))

    def budget(self, node='draft'):
        budget = RunBudget('trace-task', model_node=node, step='trace-step')
        token = current_budget.set(budget)
        self.addCleanup(current_budget.reset, token)
        return budget

    def records(self, suffix):
        return [json.loads(path.read_text()) for path in sorted(self.directory.rglob(f'*.{suffix}.json'))]

    def assert_private_files(self):
        for path in self.directory.rglob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
            self.assertEqual(path.parent.stat().st_mode & 0o077, 0, str(path.parent))

    def test_exact_sent_prompt_skill_schema_and_full_provider_reasoning_are_archived(self):
        budget = self.budget()
        module = object.__new__(SqlQueryModule)
        module.skill = SkillRegistry().get('database_query')
        system = module.technical_system('homenova_20260831')
        self.assertIn(module.skill.instructions, system)
        user = json.dumps({'request_marker': 'exact-private-input', 'items': ['中文', 'full context']}, ensure_ascii=False)
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                  'required': ['ok'], 'additionalProperties': False}
        raw = provider_body()
        with using_output_schema('trace_probe', schema), \
             patch('app.model_client.urllib.request.urlopen', return_value=Response(raw)) as send:
            self.assertEqual(self.client().chat_json(system, user), {'ok': True})
        sent = json.loads(send.call_args.args[0].data)
        request_record, = self.records('request')
        outcome, = self.records('outcome')
        self.assertEqual(request_record['request_payload'], sent)
        self.assertEqual(sent['messages'], [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
        self.assertEqual(sent['response_format']['json_schema']['schema'], schema)
        self.assertEqual(outcome['response_body'], raw)
        self.assertIn('provider-returned-reasoning-marker', outcome['response_body'])
        self.assertEqual(outcome['trace_id'], request_record['trace_id'])
        self.assertEqual(outcome['status'], 'success')
        self.assertTrue(outcome['request_attempted'])
        self.assertTrue(outcome['response_received'])
        self.assertFalse(request_record['body_truncated'])
        self.assertFalse(outcome['body_truncated'])
        self.assertEqual((request_record['task_id'], request_record['node'], request_record['step_id']),
                         ('trace-task', 'draft', 'trace-step'))
        public = json.dumps(budget.export_state(), ensure_ascii=False)
        for private in [user, system, 'exact-private-input', 'provider-returned-reasoning-marker', str(self.directory), self.key]:
            self.assertNotIn(private, public)
        self.assertEqual(budget.model_usage[0]['trace_id'], request_record['trace_id'])
        self.assertTrue(budget.model_usage[0]['trace_complete'])
        self.assertNotIn('request_payload', budget.model_usage[0])
        self.assertNotIn('response_body', budget.model_usage[0])
        self.assertEqual(budget.model_tokens, {'chat': 20})
        self.assert_private_files()

    def test_embedding_chunks_and_rerank_preserve_their_complete_inputs_and_outputs(self):
        budget = self.budget('schema_retrieval')
        texts = [f'input-{i}' for i in range(11)]
        documents = ['first-full-document', 'second-full-document']
        bodies = [json.dumps({'data': [{'index': i, 'embedding': [1.0, 0.0]} for i in range(count)],
                             'usage': {'total_tokens': count}}) for count in [10, 1]]
        bodies.append(json.dumps({'results': [{'index': 1, 'relevance_score': 0.9},
                                             {'index': 0, 'relevance_score': 0.3}], 'usage': {'total_tokens': 2}}))
        with patch('app.model_client.urllib.request.urlopen', side_effect=[Response(body) for body in bodies]) as send:
            client = self.client()
            self.assertEqual(len(client.embed(texts)), 11)
            self.assertEqual(client.rerank('full-query', documents, 2), [(1, 0.9), (0, 0.3)])
        sent = [json.loads(call.args[0].data) for call in send.call_args_list]
        requests = self.records('request')
        self.assertEqual(len(requests), 3)
        self.assertCountEqual([record['request_payload'] for record in requests], sent)
        self.assertEqual(sent[0]['input'], texts[:10])
        self.assertEqual(sent[1]['input'], texts[10:])
        self.assertEqual(sent[2]['documents'], documents)
        self.assertEqual(sent[2]['query'], 'full-query')
        self.assertCountEqual([item['response_body'] for item in self.records('outcome')], bodies)
        self.assertEqual(budget.model_calls, {'embedding': 2, 'rerank': 1})

    def test_http_error_body_is_complete_and_nonretryable_error_is_not_replayed(self):
        self.budget('data_qa')
        client = self.client()
        body = json.dumps({'error': {'code': 'Forbidden', 'message': 'provider-detail-' * 200}})
        denied = urllib.error.HTTPError(client.config.chat_url, 403, 'Forbidden', {}, io.BytesIO(body.encode()))
        with patch('app.model_client.urllib.request.urlopen', side_effect=denied) as send:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                client.chat('system', 'question')
        self.assertEqual(send.call_count, 1)
        outcome, = self.records('outcome')
        self.assertEqual(outcome['response_body'], body)
        self.assertTrue(outcome['response_received'])
        self.assertEqual(outcome['http_status'], 403)

    def test_timeout_has_no_invented_response_and_no_retry(self):
        budget = self.budget('qa_delivery_review')
        with patch('app.model_client.urllib.request.urlopen', side_effect=TimeoutError('deadline')) as send, \
             patch('app.model_client.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'deadline'):
                self.client().chat('system', 'question')
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()
        outcome, = self.records('outcome')
        self.assertTrue(outcome['request_attempted'])
        self.assertFalse(outcome['response_received'])
        self.assertIsNone(outcome['response_body'])
        self.assertEqual(outcome['error_type'], 'TimeoutError')
        self.assertIsNone(budget.model_usage[0]['usage'])
        self.assertTrue(budget.model_estimated_tokens)
        self.assertFalse(budget.model_reservations)

    def test_read_timeout_after_headers_is_not_a_complete_response(self):
        self.budget()
        with patch('app.model_client.urllib.request.urlopen', return_value=Response(TimeoutError('body deadline'))) as send:
            with self.assertRaisesRegex(RuntimeError, 'body deadline'):
                self.client().chat('system', 'question')
        self.assertEqual(send.call_count, 1)
        outcome, = self.records('outcome')
        self.assertFalse(outcome['response_received'])
        self.assertIsNone(outcome['response_body'])

    def test_two_retries_each_have_unique_append_only_request_and_outcome(self):
        budget = self.budget()
        client = self.client()
        errors = [urllib.error.HTTPError(client.config.chat_url, 503, 'Unavailable', {},
                                        io.BytesIO(f'failure-{i}'.encode())) for i in range(2)]
        with patch('app.model_client.urllib.request.urlopen', side_effect=[*errors, Response(provider_body())]) as send, \
             patch('app.model_client.time.sleep'):
            self.assertEqual(client.chat_json('system', 'question'), {'ok': True})
        self.assertEqual(send.call_count, 3)
        requests, outcomes = self.records('request'), self.records('outcome')
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(outcomes), 3)
        ids = {item['trace_id'] for item in requests}
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids, {item['trace_id'] for item in outcomes})
        self.assertEqual(ids, {item['trace_id'] for item in budget.model_usage})
        self.assertEqual({item['attempt'] for item in requests}, {1, 2, 3})
        self.assertEqual(sum(item['status'] == 'success' for item in outcomes), 1)
        self.assertEqual(budget.model_calls, {'chat': 3})
        self.assertEqual(budget.model_tokens, {'chat': 20})

    def test_sensitive_values_in_nested_json_are_redacted_without_mutating_actual_payload_or_result(self):
        self.budget()
        credentials = ['nested-password-719', 'nested-refresh-693', 'metadata-key-734', 'response-secret-428']
        payload = {'model': 'test-model', 'messages': [
            {'role': 'system', 'content': f'known-key={self.key}'},
            {'role': 'user', 'content': json.dumps({'level': [{'password': credentials[0],
                'encoded': json.dumps({'refresh_token': credentials[1]})}], 'safe': 'retain-this'})}],
            'metadata': {'api_key': credentials[2]}}
        before = deepcopy(payload)
        raw = provider_body(json.dumps({'encoded': json.dumps({'secret': credentials[3]}), 'safe': 'keep-response'}))
        with patch('app.model_client.urllib.request.urlopen', return_value=Response(raw)) as send:
            result = self.client()._post(self.client().config.chat_url, payload)
        self.assertEqual(json.loads(send.call_args.args[0].data), before)
        self.assertEqual(payload, before)
        self.assertEqual(result, json.loads(raw))
        files = list(self.directory.rglob('*.json'))
        text = '\n'.join(path.read_text() for path in files)
        for sensitive in [self.key, *credentials]:
            self.assertNotIn(sensitive, text)
        self.assertIn('retain-this', text)
        self.assertIn('keep-response', text)
        self.assertIn('[REDACTED]', text)
        self.assertGreater(sum(json.loads(path.read_text())['redaction_count'] for path in files), 0)
        self.assertNotIn('Authorization', text)

    def test_disabled_trace_writes_no_files_or_public_trace_fields(self):
        budget = self.budget()
        with patch('app.model_client.ModelTrace') as trace, \
             patch('app.model_client.urllib.request.urlopen', return_value=Response(provider_body())):
            self.assertEqual(self.client(model_trace_dir='').chat_json('system', 'question'), {'ok': True})
        trace.assert_not_called()
        self.assertFalse(self.directory.exists())
        self.assertNotIn('trace_id', budget.model_usage[0])

    def test_request_write_failure_sends_nothing_and_does_not_increase_budget(self):
        budget = self.budget()
        before = deepcopy(budget.export_state())
        with patch('app.model_trace.ModelTrace._write', side_effect=OSError('private disk unavailable')), \
             patch('app.model_client.urllib.request.urlopen') as send:
            with self.assertRaises(ModelRequestNotSent) as raised:
                self.client().chat('private input', 'question')
        self.assertFalse(raised.exception.request_sent)
        send.assert_not_called()
        self.assertEqual(budget.export_state(), before)

    def test_outcome_write_failure_never_replays_and_keeps_known_usage(self):
        budget = self.budget()
        write = ModelTrace._write
        def fail_outcome(trace, suffix, data):
            if suffix == 'outcome':
                raise OSError('private disk became unavailable')
            return write(trace, suffix, data)
        with patch('app.model_trace.ModelTrace._write', fail_outcome), \
             patch('app.model_client.urllib.request.urlopen', return_value=Response(provider_body())) as send, \
             patch('app.model_client.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '记录写入失败'):
                self.client().chat('private input', 'question')
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(len(self.records('request')), 1)
        self.assertEqual(self.records('outcome'), [])
        self.assertEqual(budget.model_calls, {'chat': 1})
        self.assertEqual(budget.model_tokens, {'chat': 20})
        self.assertFalse(budget.model_estimated_tokens)
        self.assertFalse(budget.model_reservations)
        self.assertEqual(budget.model_usage[0]['status'], 'success')
        self.assertFalse(budget.model_usage[0]['trace_complete'])
        self.assertEqual(budget.model_usage[0]['trace_error_type'], 'OSError')

    def test_unscoped_calls_are_archived_without_a_budget(self):
        with patch('app.model_client.urllib.request.urlopen', return_value=Response(provider_body())):
            self.assertEqual(self.client().chat_json('system', 'question'), {'ok': True})
        request_record, = self.records('request')
        self.assertIsNone(request_record['task_id'])
        self.assertEqual(len(list((self.directory / 'unscoped').glob('*.json'))), 2)

    def test_unparseable_provider_response_is_still_saved_before_json_parsing(self):
        self.budget()
        raw = 'not JSON\nprovider-full-return-marker\n' * 30
        with patch('app.model_client.urllib.request.urlopen', return_value=Response(raw)) as send:
            with self.assertRaises(RuntimeError):
                self.client(max_retries=0).chat('system', 'question')
        self.assertEqual(send.call_count, 1)
        outcome, = self.records('outcome')
        self.assertEqual(outcome['response_body'], raw)
        self.assertTrue(outcome['response_received'])
        self.assertTrue(outcome['response_body_complete'])
        self.assertFalse(outcome['response_partial'])

    def test_incomplete_read_saves_received_partial_bytes_without_claiming_complete(self):
        budget = self.budget('query_coder')
        partial = b'{"choices":[{"message":{"content":"partial-provider-output'
        with patch('app.model_client.urllib.request.urlopen',
                   return_value=Response(IncompleteRead(partial, 50))) as send:
            with self.assertRaises(RuntimeError):
                self.client().chat('system', 'question')
        self.assertEqual(send.call_count, 1)
        outcome, = self.records('outcome')
        self.assertEqual(outcome['response_body'], partial.decode())
        self.assertFalse(outcome['response_received'])
        self.assertFalse(outcome['response_body_complete'])
        self.assertTrue(outcome['response_partial'])
        self.assertEqual(outcome['error_type'], 'IncompleteRead')
        self.assertIsNone(budget.model_usage[0]['usage'])
        self.assertFalse(budget.model_reservations)

    def test_http_error_body_read_timeout_records_absent_body_without_replay(self):
        class TimedOutErrorBody(io.BytesIO):
            def read(self, *_args):
                raise TimeoutError('HTTP error body deadline')
        budget = self.budget('data_qa')
        client = self.client()
        error = urllib.error.HTTPError(client.config.chat_url, 503, 'Unavailable', {}, TimedOutErrorBody())
        with patch('app.model_client.urllib.request.urlopen', side_effect=error) as send, \
             patch('app.model_client.time.sleep') as sleep:
            with self.assertRaises((TimeoutError, RuntimeError)):
                client.chat('system', 'question')
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()
        outcome, = self.records('outcome')
        self.assertEqual(outcome['http_status'], 503)
        self.assertEqual(outcome['error_type'], 'TimeoutError')
        self.assertFalse(outcome['response_received'])
        self.assertFalse(outcome['response_body_complete'])
        self.assertFalse(outcome['response_partial'])
        self.assertIsNone(outcome['response_body'])
        self.assertIsNone(budget.model_usage[0]['usage'])
        self.assertEqual(budget.model_calls, {'chat': 1})
        self.assertFalse(budget.model_reservations)


if __name__ == '__main__':
    unittest.main()
