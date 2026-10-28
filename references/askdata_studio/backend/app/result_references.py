"""Resolve a result identity within an already authorized input collection.

Storage references stay internal. Logical IDs and legacy storage aliases are
accepted only when they identify exactly one active result in the caller's
scope; no database or global result lookup is performed here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def model_result_context(value: Any) -> Any:
    """Give models one result identity while retaining storage refs internally."""
    if isinstance(value, dict):
        result_record = ('id' in value and 'check_status' in value) or ('rows' in value and 'columns' in value)
        internal = {'result_ref', 'source_result_id', 'source_task_id'} if result_record else set()
        return {key: deepcopy(item) if key == 'rows' else model_result_context(item)
                for key, item in value.items() if key not in internal}
    if isinstance(value, list):
        return [model_result_context(item) for item in value]
    return value


def resolve_result_ids(references: list[str], context: dict[str, Any],
                       normalizations: list[dict[str, Any]] | None = None) -> list[str]:
    results = context.get('dependency_results') or []
    resolved = []
    for reference in references:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError('结果引用必须是非空证据编号')
        matches = [item for item in results if reference in {item.get('id'), item.get('result_ref')}]
        if len(matches) != 1:
            raise ValueError('结果引用未授权、不是本步依赖或存在歧义')
        result = matches[0]
        for key in ['task_id', 'user_id', 'datasource_id']:
            expected = context.get(key)
            if expected is not None and result.get(key) != expected:
                raise ValueError(f'结果引用的{key}范围不匹配或缺失')
        expected_snapshot = context.get('snapshot') or {}
        actual_snapshot = result.get('snapshot') or {}
        for key in ['snapshot_id', 'observation_cutoff_at']:
            if key in expected_snapshot and actual_snapshot.get(key) != expected_snapshot[key]:
                raise ValueError('结果引用的快照截点不匹配或缺失')
        if result.get('check_status') not in {'basic_valid', 'reviewed'} or not result.get('complete'):
            raise ValueError('结果引用已失效、不完整或未通过基础检查')
        logical_id = result.get('id')
        if not logical_id:
            raise ValueError('结果缺少有效证据编号')
        if reference != logical_id and normalizations is not None:
            normalizations.append({'from': reference, 'to': logical_id, 'reason': '当前授权输入中的同一结果'})
        if logical_id not in resolved:
            resolved.append(logical_id)
    return resolved
