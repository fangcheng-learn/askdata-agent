from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app import token_counting as counting


class TokenCountingTest(unittest.TestCase):
    def tearDown(self):
        counting._load_encoding.cache_clear()

    def test_official_asset_and_pattern_are_pinned_and_counts_stable(self):
        self.assertEqual(hashlib.sha256(counting.VOCAB_PATH.read_bytes()).hexdigest(), counting.VOCAB_SHA256)
        source = counting.VOCAB_PATH.with_name('tokenization_qwen.py.upstream')
        tree = ast.parse(source.read_text())
        official_pattern = next(node.value.value for node in tree.body
                                if isinstance(node, ast.Assign)
                                and any(isinstance(t, ast.Name) and t.id == 'PAT_STR' for t in node.targets))
        self.assertEqual(counting.PAT_STR, official_pattern)
        details = counting.input_token_details('你好，世界！')
        self.assertFalse(details['fallback'])
        self.assertFalse(details['exact'])
        self.assertEqual(details['raw_local_count'], 4)
        self.assertEqual(details['estimated_count'], 69)
        self.assertEqual(counting.estimate_input_tokens('你好，世界！'), 69)

    def test_chinese_payload_is_not_mislabelled_as_utf8_bytes(self):
        payload = {'messages': [{'role': 'system', 'content': '只依据真实数据回答。'},
                                {'role': 'user', 'content': '经营指标订单GMV分析。' * 2000}]}
        details = counting.input_token_details(payload)
        self.assertFalse(details['fallback'])
        self.assertGreater(details['utf8_byte_upper_bound'], 48000)
        self.assertLess(details['estimated_count'], 48000)
        raw = details['raw_local_count']
        self.assertEqual(details['estimated_count'], (raw * 5 + 3) // 4 + 128)

    def test_nfc_equivalent_strings_have_same_counts(self):
        a = counting.input_token_details('cafe\u0301 数据')
        b = counting.input_token_details('café 数据')
        self.assertEqual(a['raw_local_count'], b['raw_local_count'])
        self.assertEqual(a['estimated_count'], b['estimated_count'])
        self.assertEqual(a['normalization'], 'NFC')

    def test_user_special_token_strings_are_ordinary_text(self):
        details = counting.input_token_details('<|im_start|><|endoftext|><|im_end|>')
        self.assertEqual(details['raw_local_count'], 17)
        self.assertGreater(details['raw_local_count'], 3)
        self.assertEqual(details['special_token_policy'], 'ordinary_text')

    def test_missing_dependency_has_labelled_nonzero_conservative_fallback(self):
        counting._load_encoding.cache_clear()
        with patch.object(counting.importlib, 'import_module', side_effect=ImportError('controlled')):
            details = counting.input_token_details('缺少依赖时不能免除输入限制')
        self.assertTrue(details['fallback'])
        self.assertIsNone(details['raw_local_count'])
        self.assertEqual(details['estimator'], counting.FALLBACK_ESTIMATOR)
        self.assertEqual(details['estimated_count'], details['utf8_byte_upper_bound'])
        self.assertGreater(details['estimated_count'], 64)
        self.assertFalse(details['exact'])
        counting._load_encoding.cache_clear()
        decomposed = 'e\u0301' * 1000
        with patch.object(counting.importlib, 'import_module', side_effect=ImportError('controlled')):
            fallback = counting.input_token_details(decomposed)
        self.assertGreaterEqual(fallback['estimated_count'], len(decomposed.encode('utf-8')) + 64)

    def test_missing_or_changed_asset_falls_back_instead_of_downloading(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / 'qwen.tiktoken'
            for changed in (False, True):
                if changed:
                    missing.write_bytes(b'not-the-pinned-vocabulary')
                counting._load_encoding.cache_clear()
                with patch.object(counting, 'VOCAB_PATH', missing), patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
                    details = counting.input_token_details({'input': ['GMV定义']})
                self.assertTrue(details['fallback'])
                self.assertEqual(details['fallback_reason'], 'asset_hash_mismatch' if changed else 'FileNotFoundError')
                self.assertEqual(details['estimated_count'], details['utf8_byte_upper_bound'])

    def test_cold_load_and_parallel_counting_need_no_runtime_network(self):
        counting._load_encoding.cache_clear()
        with patch('socket.socket.connect', side_effect=AssertionError('runtime network forbidden')), patch('urllib.request.urlopen', side_effect=AssertionError('runtime download forbidden')):
            baseline = counting.estimate_input_tokens({'messages': [{'role': 'user', 'content': '核对GMV和支付订单口径'}]})
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(counting.estimate_input_tokens,
                                       [{'messages': [{'role': 'user', 'content': '核对GMV和支付订单口径'}]}] * 8))
        self.assertEqual(values, [baseline] * 8)


if __name__ == '__main__':
    unittest.main()
