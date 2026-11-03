from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import patch

from app.config import Settings
from app.model_client import ModelClient
from app.runtime_budget import RunBudget, current_budget


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": {"content": "好"}}]}).encode()


class ModelClientTest(unittest.TestCase):
    def test_every_role_timeout_stops_replay_preserves_unknown_usage_and_does_not_trip_shared_endpoint(self):
        for node in ['draft', 'data_qa', 'delivery_review', 'qa_delivery_review', 'preprocess', 'other-role']:
            for error in [TimeoutError('read timed out'), socket.timeout('read timed out'),
                          urllib.error.URLError(TimeoutError('read timed out'))]:
                with self.subTest(node=node, error=type(error).__name__):
                    budget = RunBudget(node, model_node=node)
                    token = current_budget.set(budget)
                    try:
                        client = ModelClient(Settings(api_key='test-key', max_retries=2))
                        with patch('app.model_client.urllib.request.urlopen', side_effect=[error, _Response()]) as send, \
                             patch('app.model_client.time.sleep') as sleep:
                            with self.assertRaisesRegex(RuntimeError, 'read timed out'):
                                client.chat('system', 'first')
                            self.assertEqual(send.call_count, 1)
                            sleep.assert_not_called()
                            self.assertEqual(budget.model_calls, {'chat': 1})
                            self.assertEqual(budget.repairs, {})
                            self.assertEqual(len(budget.model_usage), 1)
                            self.assertEqual(budget.model_usage[0]['status'], 'transport_error')
                            self.assertIsNone(budget.model_usage[0]['usage'])
                            self.assertFalse(budget.model_tokens)
                            self.assertTrue(budget.model_estimated_tokens)
                            self.assertFalse(budget.model_reservations)
                            self.assertEqual(client.chat('system', 'different next request'), '好')
                    finally:
                        current_budget.reset(token)

    def test_timeout_after_response_headers_does_not_replay_or_invent_usage(self):
        class TimedOutBody(_Response):
            def read(self):
                raise socket.timeout('response body timed out')
        budget = RunBudget('body-timeout', model_node='draft')
        token = current_budget.set(budget)
        try:
            client = ModelClient(Settings(api_key='test-key', max_retries=2))
            with patch('app.model_client.urllib.request.urlopen', return_value=TimedOutBody()) as send:
                with self.assertRaisesRegex(RuntimeError, 'response body timed out'):
                    client.chat('system', 'test')
            self.assertEqual(send.call_count, 1)
            self.assertEqual(len(budget.model_usage), 1)
            self.assertEqual(budget.model_usage[0]['status'], 'transport_error')
            self.assertIsNone(budget.model_usage[0]['usage'])
            self.assertEqual(budget.repairs, {})
        finally:
            current_budget.reset(token)

    def test_timeout_without_task_budget_does_not_replay(self):
        client = ModelClient(Settings(api_key='test-key', max_retries=2))
        with patch('app.model_client.urllib.request.urlopen', side_effect=TimeoutError('read timed out')) as send, \
             patch('app.model_client.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'read timed out'):
                client.chat('system', 'test')
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()

    def test_configured_transport_timeout_is_reserved_before_send_and_restored_if_interrupted(self):
        for phase, spent, configured, expected in [('investigation', 10, 120, 120),
                                                  ('investigation', 10, 180, 180),
                                                  ('investigation', 400, 120, 20),
                                                  ('investigation', 400, 180, 20),
                                                  ('closing', 450, 120, 30),
                                                  ('closing', 450, 180, 30),
                                                  ('investigation', 0, 60, 60)]:
            with self.subTest(phase=phase, spent=spent, configured=configured):
                budget = RunBudget('request', phase=phase, spent=spent, model_node='draft')
                saved = []
                budget.checkpoint_callback = lambda: saved.append(budget.export_state())
                token = current_budget.set(budget)
                try:
                    client = ModelClient(Settings(api_key='test-key', max_retries=2, timeout=configured))
                    def send(_request, *, timeout):
                        self.assertEqual(timeout, expected)
                        checkpoint = next(item for item in reversed(saved) if item['model_reservations'])
                        reservation = next(iter(checkpoint['model_reservations'].values()))
                        self.assertEqual(reservation['seconds_reserved'], expected)
                        self.assertEqual(checkpoint['model_usage'][-1]['timeout_seconds'], expected)
                        restored = RunBudget.from_state('request', json.loads(json.dumps(checkpoint)))
                        self.assertEqual(restored.spent, spent + expected)
                        self.assertEqual(restored.model_calls, {'chat': 1})
                        self.assertFalse(restored.model_reservations)
                        self.assertFalse(restored.model_tokens)
                        self.assertTrue(restored.model_estimated_tokens)
                        self.assertEqual(restored.seconds, 480)
                        self.assertEqual(restored.investigation_seconds, 420)
                        return _Response()
                    with patch('app.model_client.urllib.request.urlopen', side_effect=send):
                        self.assertEqual(client.chat('system', 'test'), '好')
                    self.assertEqual(budget.model_usage[0]['timeout_seconds'], expected)
                    self.assertEqual(budget.model_calls, {'chat': 1})
                finally:
                    current_budget.reset(token)

    def test_403_remains_terminal_even_with_retry_allowance(self):
        client = ModelClient(Settings(api_key='test-key', max_retries=2))
        denied = urllib.error.HTTPError(client.config.chat_url, 403, 'Forbidden', {}, io.BytesIO(b'Forbidden'))
        with patch('app.model_client.urllib.request.urlopen', side_effect=denied) as send:
            with self.assertRaisesRegex(RuntimeError, r'HTTP 403 \(Forbidden\): Forbidden'):
                client.chat('system', 'test')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(client._blocked_until, {})

    def test_single_request_timeout_does_not_trip_shared_endpoint(self):
        client=ModelClient(Settings(api_key='test-key',max_retries=0))
        client._blocked_until.clear()
        with patch('app.model_client.urllib.request.urlopen',side_effect=[TimeoutError('read timed out'),_Response()]):
            with self.assertRaisesRegex(RuntimeError,'read timed out'):
                client.chat('system','first')
            self.assertEqual(client.chat('system','second'),'好')

    def test_server_failure_still_trips_circuit(self):
        client=ModelClient(Settings(api_key='test-key',max_retries=0))
        client._blocked_until.clear()
        self.addCleanup(client._blocked_until.clear)
        error=urllib.error.HTTPError(client.config.chat_url,503,'Unavailable',{},io.BytesIO(b'unavailable'))
        with patch('app.model_client.urllib.request.urlopen',side_effect=error) as send:
            with self.assertRaisesRegex(RuntimeError,'503'):client.chat('system','first')
            from app.model_client import ModelRequestNotSent
            with self.assertRaisesRegex(ModelRequestNotSent,'熔断') as blocked:client.chat('system','second')
            self.assertFalse(blocked.exception.request_sent)
            self.assertEqual(send.call_count,1)

    def test_chat_uses_configured_non_thinking_mode(self) -> None:
        client = ModelClient(Settings(api_key="test-key", llm_enable_thinking=False))
        with patch("app.model_client.urllib.request.urlopen", return_value=_Response()) as send:
            self.assertEqual(client.chat("系统", "你好"), "好")
        payload = json.loads(send.call_args.args[0].data)
        self.assertIs(payload["enable_thinking"], False)

    def test_nonretryable_403_does_not_block_next_request(self) -> None:
        client = ModelClient(Settings(api_key="test-key", max_retries=0))
        denied = urllib.error.HTTPError(
            client.config.chat_url, 403, "Forbidden", {}, io.BytesIO(b"Forbidden")
        )
        with patch("app.model_client.urllib.request.urlopen", side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                client.chat("系统", "你好")
        with patch("app.model_client.urllib.request.urlopen", return_value=_Response()):
            self.assertEqual(client.chat("系统", "你好"), "好")

    def test_http_error_keeps_full_response_body(self) -> None:
        client = ModelClient(Settings(api_key="test-key", max_retries=0))
        body = "Forbidden: " + "provider-detail-" * 60
        denied = urllib.error.HTTPError(
            client.config.chat_url, 403, "Forbidden", {}, io.BytesIO(body.encode())
        )
        with patch("app.model_client.urllib.request.urlopen", side_effect=denied):
            with self.assertRaises(RuntimeError) as raised:
                client.chat("系统", "你好")
        self.assertIn(body, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
