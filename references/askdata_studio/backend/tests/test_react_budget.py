import io
import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from app.config import Settings
from app.model_client import ModelClient
from app.runtime_budget import RunBudget, BudgetStop, current_budget


class ReactBudgetTest(unittest.TestCase):
    def test_legacy_token_caps_and_closing_reserve_do_not_stop_calls(self):
        b = RunBudget('t', model_token_limit=1000, closing_token_reserve=900, max_output_tokens=10)
        b.start()
        key = b.reserve_model('chat', {'messages': ['这是有真实输入的调查请求' * 5]})
        b.settle_model(key, {'total_tokens': 2000})
        b.phase = 'closing'
        key = b.reserve_model('chat', {'messages': ['报告']})
        b.settle_model(key, {'prompt_tokens': 10, 'completion_tokens': 4})
        self.assertEqual(b.model_tokens['chat'], 2014)
        self.assertIsNone(b.model_budget()['remaining'])

    def test_unknown_usage_is_not_released_as_free(self):
        b = RunBudget('t'); b.start()
        key = b.reserve_model('chat', {'messages': ['输入']})
        reserved = b.model_reservations[key]['tokens']
        b.settle_model(key, None)
        self.assertEqual(b.model_tokens.get('chat', 0), 0)
        self.assertEqual(b.model_estimated_tokens['chat'], reserved)
        b.settle_model(key, {'total_tokens': 1})
        self.assertEqual(b.model_tokens.get('chat', 0), 0)
        self.assertEqual(b.model_estimated_tokens['chat'], reserved, 'settlement is idempotent')

    def test_retrieval_records_usage_without_its_old_shared_limit(self):
        b = RunBudget('t', retrieval_token_limit=300); b.start()
        key = b.reserve_model('embedding', {'input': ['GMV']})
        b.settle_model(key, {'total_tokens': 200})
        key = b.reserve_model('rerank', {'query': 'GMV', 'documents': ['字段定义' * 30]})
        b.settle_model(key, {'total_tokens': 400})
        self.assertEqual(b.model_tokens['retrieval'], 600)

    def test_input_length_is_estimated_without_token_admission_limit(self):
        b = RunBudget('t', max_input_tokens=1, model_token_limit=1, max_output_tokens=1)
        payload = {'messages': [{'role': 'user', 'content': '经营指标口径' * 4000}]}
        upper = b.estimate_tokens(payload)
        self.assertGreater(upper, b.max_input_tokens)
        self.assertGreater(b.estimate_input_tokens(payload), b.max_input_tokens)
        key = b.reserve_model('chat', payload)
        self.assertEqual(b.model_reservations[key]['input_upper_bound'], upper)
        self.assertEqual(b.model_reservations[key]['tokens'], b.estimate_input_tokens(payload))
        self.assertFalse(b.model_budget()['enforced'])

    def test_accumulated_token_usage_is_observation_only(self):
        b = RunBudget('t', model_token_limit=1, model_tokens={'chat': 90000})
        payload = {'messages': [{'role': 'user', 'content': '经营指标口径' * 1000}]}
        key = b.reserve_model('chat', payload)
        b.settle_model(key, {'total_tokens': 3000})
        self.assertEqual(b.model_budget()['used']['chat'], 93000)

    def test_restore_keeps_budget_period_and_charges_unknown_inflight(self):
        b = RunBudget('t', tools=5, repairs={'A1': 1}, spent=10, reference_now='2026-10-29T12:00:00+08:00')
        key = b.reserve_model('chat', {'messages': ['x']})
        saved = json.loads(json.dumps(b.export_state()))
        restored = RunBudget.from_state('t', saved)
        self.assertEqual(restored.tools, 5)
        self.assertEqual(restored.repairs, {'A1': 1})
        self.assertEqual(restored.reference_now, b.reference_now)
        self.assertGreaterEqual(restored.spent, 10)
        self.assertEqual(restored.model_tokens.get('chat', 0), 0)
        self.assertGreater(restored.model_estimated_tokens['chat'], 0)
        self.assertFalse(restored.model_reservations)

    def test_model_dispatch_omits_old_output_limit_and_accounts_provider_total(self):
        b = RunBudget('t', max_output_tokens=1234); b.start()
        token = current_budget.set(b)
        body = {'choices':[{'message':{'content':'{}'}}], 'usage':{'prompt_tokens':50,'completion_tokens':7,'total_tokens':57}}
        class Response:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self): return json.dumps(body).encode()
        seen = []
        def call(request, **kwargs):
            seen.append(json.loads(request.data))
            return Response()
        try:
            with patch('app.model_client.urllib.request.urlopen', side_effect=call):
                self.assertEqual(ModelClient(Settings(api_key='unit-test-only')).chat_json('JSON','test'), {})
            self.assertNotIn('max_tokens', seen[0])
            self.assertEqual(b.model_tokens['chat'], 57)
            self.assertFalse(b.model_reservations)
        finally:
            current_budget.reset(token)

    def test_new_eight_minute_budget_does_not_rewrite_saved_old_task(self):
        fresh = RunBudget('new', spent=419)
        self.assertEqual((fresh.seconds, fresh.investigation_seconds), (480, 420))
        self.assertAlmostEqual(fresh.remaining(), 1)
        fresh.phase = 'closing'
        self.assertAlmostEqual(fresh.remaining(), 61)
        old = RunBudget('old', seconds=300, investigation_seconds=240, spent=125)
        restored = RunBudget.from_state('old', old.export_state())
        self.assertEqual((restored.seconds, restored.investigation_seconds, restored.spent), (300, 240, 125))

    def test_unknown_inflight_tool_time_is_not_reset_on_restore(self):
        b = RunBudget('t', spent=20)
        key = b.begin_tool_call('query_source')
        restored = RunBudget.from_state('t', json.loads(json.dumps(b.export_state())))
        self.assertEqual(restored.tools, 1)
        self.assertGreaterEqual(restored.spent, 240)
        self.assertFalse(restored.pending_tools)

    def test_completed_tool_does_not_keep_inflight_reservation(self):
        b = RunBudget('t', spent=20)
        key = b.begin_tool_call('query_source')
        b.finish_tool_call(key)
        restored = RunBudget.from_state('t', json.loads(json.dumps(b.export_state())))
        self.assertEqual(restored.spent, 20)
        self.assertEqual(restored.tools, 1)

    def test_http_failure_keeps_reservation_and_does_not_retry_forbidden(self):
        b = RunBudget('t'); b.start(); token = current_budget.set(b)
        error = urllib.error.HTTPError('https://example.invalid',403,'Forbidden',{},io.BytesIO(b'account disabled'))
        try:
            with patch('app.model_client.urllib.request.urlopen', side_effect=error) as call:
                with self.assertRaises(RuntimeError):
                    ModelClient(Settings(api_key='unit-test-only')).chat('json','x')
                self.assertEqual(call.call_count,1)
            self.assertEqual(b.model_tokens.get('chat', 0), 0)
            self.assertGreater(b.model_estimated_tokens['chat'],0)
            self.assertFalse(b.model_reservations)
        finally:
            current_budget.reset(token)


if __name__ == '__main__':
    unittest.main()
