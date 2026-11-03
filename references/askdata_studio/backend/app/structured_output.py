"""One task-local schema transport shared by model-producing nodes.

Schemas describe protocol shape only; permissions, evidence and business
semantics continue to be checked by their execution boundaries.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re

current_output_schema: ContextVar[dict | None] = ContextVar('output_schema', default=None)


@contextmanager
def using_output_schema(name: str, schema: dict):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name) or not isinstance(schema, dict):
        raise ValueError('结构化输出需有效schema名称和对象')
    copied = deepcopy(schema)
    token = current_output_schema.set({'name': name, 'schema': copied,
        'sha256': hashlib.sha256(json.dumps(copied, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
    try:
        yield
    finally:
        current_output_schema.reset(token)


def schema_response_format(*, enabled: bool = True) -> dict:
    selected = current_output_schema.get()
    if selected is None or not enabled:
        return {'type': 'json_object'}
    return {'type': 'json_schema', 'json_schema': {
        'name': selected['name'], 'strict': True, 'schema': deepcopy(selected['schema'])}}
