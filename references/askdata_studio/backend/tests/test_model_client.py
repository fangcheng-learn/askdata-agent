from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from app.config import Settings
from app.model_client import ModelClient


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": {"content": "好"}}]}).encode()


class ModelClientTest(unittest.TestCase):
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
