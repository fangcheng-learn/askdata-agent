"""Offline Qwen-family input estimates, distinct from vendor billing counts.

PAT_STR and the BPE file format derive from QwenLM/Qwen-Agent (Apache-2.0),
Copyright 2023 The Qwen team, Alibaba Group. See tokenization_data/NOTICE and
LICENSE.qwen-agent. Local changes: ordinary-text-only encoding, NFC, 25% margin,
explicit message overhead, asset hash verification, and labelled safe fallback.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any
import unicodedata


PAT_STR = r"""(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"""
VOCAB_PATH = Path(__file__).resolve().parent / 'tokenization_data' / 'qwen.tiktoken'
VOCAB_SHA256 = 'b2b1b8dfb5cc5f024bafc373121c6aba3f66f9a5a0269e243470a1de16a33186'
ESTIMATOR = 'qwen_agent_bpe_nfc_ordinary_margin25_v1'
FALLBACK_ESTIMATOR = 'utf8_byte_upper_bound_fallback'


@lru_cache(maxsize=1)
def _load_encoding() -> tuple[Any | None, str | None]:
    """Only open the vendored file; never use a downloading encoding registry."""
    try:
        tiktoken = importlib.import_module('tiktoken')
        content = VOCAB_PATH.read_bytes()
        if hashlib.sha256(content).hexdigest() != VOCAB_SHA256:
            raise ValueError('asset_hash_mismatch')
        ranks = {}
        for line in content.splitlines():
            if not line:
                continue
            token, rank = line.split()
            ranks[base64.b64decode(token, validate=True)] = int(rank)
        if len(ranks) != 151643 or len(set(ranks.values())) != len(ranks):
            raise ValueError('invalid_official_vocabulary')
        encoding = tiktoken.Encoding(
            name='askdata_qwen_agent_local_v1', pat_str=PAT_STR,
            mergeable_ranks=ranks, special_tokens={},
        )
        return encoding, None
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        # Expose failure kind, not request text or arbitrary exception contents.
        reason = 'asset_hash_mismatch' if str(exc) == 'asset_hash_mismatch' else type(exc).__name__
        return None, reason


def _text(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, separators=(',', ':'), default=str)


def _overhead(payload: Any) -> int:
    messages = payload.get('messages') if isinstance(payload, dict) else None
    return max(128, 64 * len(messages)) if isinstance(messages, list) else 64


def input_token_details(payload: Any) -> dict[str, Any]:
    """Count locally and add headroom; this is not closed Qwen3.7 tokenization.

    JSON containers are counted deterministically with Unicode preserved.
    User-provided special-token spellings are encoded as ordinary text, so they
    cannot reduce admission counts by pretending to be chat control tokens.
    """
    content = _text(payload)
    overhead = _overhead(payload)
    byte_bound = len(content.encode('utf-8', errors='replace')) + overhead
    encoding, failure = _load_encoding()
    if encoding is None:
        return {'raw_local_count': None, 'estimated_count': byte_bound,
                'estimator': FALLBACK_ESTIMATOR, 'exact': False, 'fallback': True,
                'fallback_reason': failure, 'message_overhead': overhead,
                'safety_margin_ratio': None, 'utf8_byte_upper_bound': byte_bound,
                'asset_sha256': VOCAB_SHA256, 'normalization': 'NFC',
                'special_token_policy': 'ordinary_text'}
    # The local tokenizer follows upstream NFC. The independent byte fallback
    # deliberately measures original text, so normalization cannot shrink its
    # conservative bound when the tokenizer is unavailable.
    raw = len(encoding.encode_ordinary(unicodedata.normalize('NFC', content)))
    estimate = (raw * 5 + 3) // 4 + overhead  # ceil(raw * 1.25) without float rounding.
    return {'raw_local_count': raw, 'estimated_count': estimate,
            'estimator': ESTIMATOR, 'exact': False, 'fallback': False,
            'fallback_reason': None, 'message_overhead': overhead,
            'safety_margin_ratio': 0.25, 'utf8_byte_upper_bound': byte_bound,
            'asset_sha256': VOCAB_SHA256, 'normalization': 'NFC',
            'special_token_policy': 'ordinary_text'}


def estimate_input_tokens(payload: Any) -> int:
    return int(input_token_details(payload)['estimated_count'])
