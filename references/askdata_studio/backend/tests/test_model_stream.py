import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app.config import Settings
from app.model_client import ModelClient
from app.model_stream import ChatStream
from app.runtime_budget import RunBudget, current_budget


def event(value):
    return ('data: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + '\n\n').encode()


def delta(**values):
    return event({'choices': [{'index': 0, 'delta': values, 'finish_reason': None}]})


def response(*, done=True, reason='stop'):
    data = delta(reasoning_content='reasoning-marker') + delta(content='{"answer":') + delta(content='"已完成"}')
    if done:
        data += event({'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]})
        data += event({'choices': [], 'usage': {'total_tokens': 27}}) + event('[DONE]')
    return io.BytesIO(data)


class ModelStreamTest(unittest.TestCase):
    def test_complete_stream_collects_content_separate_from_reasoning_and_usage(self):
        stream = ChatStream()
        result = stream.read(response(), deadline=time.monotonic() + 5)
        self.assertEqual(json.loads(result['choices'][0]['message']['content']), {'answer': '已完成'})
        self.assertEqual(result['usage']['total_tokens'], 27)
        self.assertIn(b'reasoning-marker', b''.join(stream.raw))

    def test_incomplete_or_truncated_stream_cannot_be_executed_even_when_json_is_valid(self):
        for body in [response(done=False), response(reason='length')]:
            with self.assertRaises(ConnectionError):
                ChatStream().read(body, deadline=time.monotonic() + 5)

    def test_deadline_applies_to_whole_stream_not_each_chunk(self):
        stream = ChatStream()
        with patch('app.model_stream.time.monotonic', side_effect=[0, 0, 0, 2]):
            with self.assertRaises(TimeoutError):
                stream.read(response(), deadline=1)
        self.assertTrue(stream.raw)
        self.assertFalse(stream.done)

    def test_cancel_interrupts_read(self):
        def stop():
            raise RuntimeError('canceled')
        with self.assertRaisesRegex(RuntimeError, 'canceled'):
            ChatStream().read(response(), deadline=time.monotonic() + 5, check=stop)

    def test_client_archives_stream_request_and_raw_output_and_never_replays_incomplete(self):
        for complete in [True, False]:
            with self.subTest(complete=complete), TemporaryDirectory() as folder:
                budget = RunBudget('stream-test', model_node='draft')
                token = current_budget.set(budget)
                try:
                    client = ModelClient(Settings(api_key='test-secret', llm_stream=True,
                        model_trace_dir=folder, max_retries=2))
                    with patch('app.model_client.urllib.request.urlopen', return_value=response(done=complete)) as send:
                        if complete:
                            self.assertEqual(json.loads(client.chat('system', 'JSON', json_mode=True)), {'answer': '已完成'})
                        else:
                            with self.assertRaises(RuntimeError):
                                client.chat('system', 'JSON', json_mode=True)
                        self.assertEqual(send.call_count, 1)
                    files = list(Path(folder).rglob('*.json'))
                    text = '\n'.join(p.read_text() for p in files)
                    self.assertIn('reasoning-marker', text)
                    self.assertIn('include_usage', text)
                    self.assertNotIn('test-secret', text)
                    self.assertEqual(budget.model_tokens.get('chat'), 27 if complete else None)
                    self.assertFalse(budget.model_reservations)
                finally:
                    current_budget.reset(token)
