"""Deterministic, scoped inputs shared by ordinary SQL and investigations.

Only already acquired public metadata is considered. This module neither
searches the repository nor treats a model's summary as query evidence.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from ..security import AccessScope
from .query_protocol import sql_facing_graph


VALUE_METADATA = (
    'samples', 'profile', 'data_profile', 'aliases', 'enum_values', 'enum_descriptions', 'enum_metadata_source', 'enum_metadata_conflict', 'unit',
    'aggregation', 'sql_type', 'nullable', 'coverage', 'null_semantics',
    'snapshot_id', 'version', 'metadata_version', 'schema_version', 'content_hash',
)
IMMUTABLE_METADATA = (
    'table_id', 'sql_name', 'sql_type', 'unit', 'aggregation', 'enum_values', 'enum_descriptions', 'enum_metadata_source', 'enum_metadata_conflict',
    'snapshot_id', 'version', 'metadata_version', 'schema_version',
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _key(item: dict) -> str:
    return str(item.get('id') or item.get('sql_name') or item.get('name') or '')


def _metric_refs(metric: dict) -> set[str]:
    refs: set[str] = set()
    for key in ('required_fields', 'source_fields', 'time_fields'):
        for entry in metric.get(key) or []:
            if isinstance(entry, str):
                refs.add(entry)
            elif isinstance(entry, dict):
                if entry.get('table') and entry.get('field'):
                    refs.add(f"{entry['table']}.{entry['field']}")
                elif entry.get('field'):
                    refs.add(str(entry['field']))
    refs.update(re.findall(r'\b[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*\b',
                          _json({key: metric.get(key) for key in (
                              'formula', 'required_predicates', 'component_policies', 'as_of_policy')})))
    return refs


def _merge(items: list[dict], category: str, conflicts: list[dict]) -> list[dict]:
    """Merge complementary attributes; retain the first authoritative conflict."""
    result: dict[str, dict] = {}
    for item in items:
        key = _key(item)
        if not key:
            continue
        incoming = deepcopy(item)
        if key not in result:
            result[key] = incoming
            continue
        previous = result[key]
        different = [name for name in IMMUTABLE_METADATA
                     if previous.get(name) is not None and incoming.get(name) is not None
                     and previous[name] != incoming[name]]
        if category == 'metrics':
            different.extend(name for name in ('definition', 'formula', 'component_policies', 'time_fields')
                             if previous.get(name) is not None and incoming.get(name) is not None
                             and previous[name] != incoming[name])
        if different:
            conflicts.append({'category': category, 'id': key, 'attributes': sorted(set(different)),
                              'existing': previous, 'incoming': incoming,
                              'reason': '相同语义编号的来源、版本或定义冲突，未静默覆盖'})
            continue
        for name, value in incoming.items():
            if name in {'score', 'source'} and name in previous:
                continue
            if name in {'samples', 'aliases'} and isinstance(value, list):
                previous[name] = list(dict.fromkeys([_json(v) for v in previous.get(name) or []]
                                                   + [_json(v) for v in value]))
                previous[name] = [json.loads(v) for v in previous[name]]
            elif isinstance(value, dict) and isinstance(previous.get(name), dict):
                # Nested values remain authoritative where already present.
                previous[name] = {**value, **previous[name]}
            elif name not in previous or previous[name] in (None, '', []):
                previous[name] = value
    return list(result.values())


def _acquired_graph(graph: dict, retrieval: dict | None, access_scope: Any,
                    database: str | None, conflicts: list[dict]) -> dict:
    source = str(database or graph.get('database') or '')
    scope = (access_scope if isinstance(access_scope, AccessScope)
             else AccessScope.from_dict(access_scope) if access_scope is not None else None)
    tables = []
    for item in graph.get('tables') or []:
        db = str(item.get('database') or graph.get('database') or source)
        if source and db != source:
            continue
        if scope and not scope.allows_table(db, str(item.get('id') or '')):
            continue
        tables.append(item)
    tables = _merge(tables, 'tables', conflicts)
    table_map = {t['id']: t for t in tables}
    fields = [item for item in graph.get('fields') or [] if item.get('table_id') in table_map]
    for hit in (retrieval or {}).get('hits') or []:
        table = table_map.get(hit.get('table_id'))
        hit_database = str(table.get('database') or source) if table else source
        if not table or (hit.get('database_id') and hit['database_id'] != hit_database):
            continue
        name = hit.get('field_name')
        if not name:
            continue
        fields.append({
            'id': hit.get('doc_id') or f"{table['id']}.{name}",
            'table_id': table['id'], 'table_name': table['name'],
            'name': name, 'sql_name': f"{table['name']}.{name}",
            'label': hit.get('field_label') or name,
            'type': hit.get('field_type') or '',
            'description': hit.get('field_description') or '',
            'role': hit.get('field_role') or '',
            **{key: hit[key] for key in VALUE_METADATA if key in hit},
        })
    fields = _merge(fields, 'fields', conflicts)
    joins, seen = [], set()
    for item in graph.get('joins') or []:
        if item.get('left_table') not in table_map or item.get('right_table') not in table_map:
            continue
        key = tuple(item.get(k) for k in ('left_table', 'left_field', 'right_table', 'right_field'))
        if key not in seen:
            seen.add(key)
            joins.append(deepcopy(item))
    return {**{k: deepcopy(v) for k, v in graph.items() if k not in {'tables', 'fields', 'joins'}},
            'database': source or graph.get('database'), 'tables': tables, 'fields': fields, 'joins': joins}


def _path(joins: list[dict], start: str, target: str) -> list[dict]:
    queue = deque([(start, [])])
    visited = {start}
    while queue:
        current, path = queue.popleft()
        if current == target:
            return path
        for join in joins:
            if join['left_table'] == current:
                other = join['right_table']
            elif join['right_table'] == current:
                other = join['left_table']
            else:
                continue
            if other not in visited:
                visited.add(other)
                queue.append((other, [*path, join]))
    return []


def _select(graph: dict, requested_fields: list[str] | None, metrics: list[dict],
            metric_ids: list[str] | None, missing: list[dict]) -> tuple[dict, list[dict]]:
    by_metric = {_key(m): m for m in metrics}
    selected_metric_ids = set(by_metric) if metric_ids is None else set(metric_ids)
    pending = list(selected_metric_ids)
    while pending:
        identity = pending.pop()
        metric = by_metric.get(identity)
        if metric is None:
            missing.append({'kind': 'metric', 'id': identity, 'reason': '当前已取得口径中未找到，需补查'})
            continue
        for dependency in metric.get('depends_on') or []:
            if dependency not in selected_metric_ids:
                selected_metric_ids.add(dependency)
                pending.append(dependency)
    selected_metrics = [m for m in metrics if _key(m) in selected_metric_ids]
    wanted = set(requested_fields or [])
    for metric in selected_metrics:
        wanted.update(_metric_refs(metric))
    fields = graph.get('fields') or []
    table_names = {t['id']: t['name'] for t in graph.get('tables') or []}

    def names(field):
        return {str(v) for v in (field.get('id'), field.get('sql_name'),
                f"{field.get('table_id')}.{field.get('name')}",
                f"{table_names.get(field.get('table_id'))}.{field.get('name')}") if v}

    known = set().union(*(names(f) for f in fields)) if fields else set()
    for ref in sorted(wanted - known):
        missing.append({'kind': 'field', 'id': ref, 'reason': '当前已取得字段中未找到，需补查'})
    selected = list(fields) if requested_fields is None else [f for f in fields if wanted & names(f)]
    table_ids = {f['table_id'] for f in selected}
    if requested_fields is None:
        table_ids.update(table_names)
    joins, join_keys = [], set()
    terminals = sorted(table_ids)
    # Select paths before dropping bridge tables. Filtering both endpoints
    # first silently removed valid A -> bridge -> C relations.
    if terminals:
        connected = {terminals[0]}
        for target in terminals[1:]:
            paths = [_path(graph.get('joins') or [], start, target) for start in sorted(connected)]
            paths = [path for path in paths if path]
            if not paths:
                connected.add(target)
                continue  # Independent batch queries need not share one join.
            for join in min(paths, key=len):
                key = tuple(join.get(k) for k in ('left_table', 'left_field', 'right_table', 'right_field'))
                if key not in join_keys:
                    join_keys.add(key)
                    joins.append(join)
                table_ids.update((join['left_table'], join['right_table']))
                connected.update((join['left_table'], join['right_table']))
    key_refs = {(j[f'{side}_table'], j[f'{side}_field']) for j in joins for side in ('left', 'right')}
    existing = {_key(f) for f in selected}
    selected.extend(f for f in fields if (f.get('table_id'), f.get('name')) in key_refs and _key(f) not in existing)
    actual_keys = {(f.get('table_id'), f.get('name')) for f in selected}
    for table, name in sorted(key_refs - actual_keys):
        missing.append({'kind': 'join_key', 'id': f'{table}.{name}', 'reason': '关联路径缺少已取得连接键，需补查'})
    return {**graph, 'tables': [t for t in graph.get('tables') or [] if t['id'] in table_ids],
            'fields': selected, 'joins': joins}, selected_metrics


def assemble_query_context(graph: dict, *, metrics: list[dict] | None = None,
                           retrieval: dict | None = None, requested_fields: list[str] | None = None,
                           metric_ids: list[str] | None = None, access_scope: Any = None,
                           database: str | None = None) -> dict:
    """Build a fresh, deduplicated request; None fields preserves acquired scope.

    An explicit [] requests only metric dependency and join closure. Missing
    metadata is returned rather than invented or declared unsupported.
    """
    conflicts: list[dict] = []
    missing: list[dict] = []
    acquired = _acquired_graph(graph, retrieval, access_scope, database, conflicts)
    source = acquired.get('database')
    metric_candidates = metrics if metrics is not None else graph.get('registered_metrics') or []
    known_metrics = _merge([m for m in metric_candidates
                           if not source or not (m.get('database_id') or m.get('datasource_id'))
                           or (m.get('database_id') or m.get('datasource_id')) == source],
                          'metrics', conflicts)
    selected, selected_metrics = _select(acquired, requested_fields, known_metrics, metric_ids, missing)
    physical = sql_facing_graph(selected)
    blocks = {'schema_graph': physical, 'registered_metrics': selected_metrics}
    manifest = {
        'database': selected.get('database'),
        'snapshot_id': graph.get('snapshot_id'), 'graph_version': graph.get('graph_version'),
        'table_ids': [t['id'] for t in selected['tables']],
        'field_ids': [_key(f) for f in selected['fields']],
        'metric_ids': [_key(m) for m in selected_metrics],
        'join_count': len(selected['joins']),
        'sources': [{'id': _key(f), **{k: f[k] for k in ('source', 'snapshot_id', 'version', 'metadata_version',
                                                     'schema_version', 'content_hash') if k in f}}
                    for f in selected['fields']],
        'blocks': {name: {'characters': len(_json(value)),
                          'estimated_tokens': (len(_json(value)) + 3) // 4,
                          'sha256': hashlib.sha256(_json(value).encode()).hexdigest()}
                   for name, value in blocks.items()},
    }
    return {**blocks, 'internal_schema_graph': selected, 'manifest': manifest,
            'missing_requests': missing, 'conflicts': conflicts}


def select_query_graph(graph: dict, *, metrics: list[dict] | None = None,
                       requested_fields: list[str] | None = None, metric_ids: list[str] | None = None,
                       access_scope: Any = None, database: str | None = None) -> dict:
    return assemble_query_context(graph, metrics=metrics, requested_fields=requested_fields,
                                  metric_ids=metric_ids, access_scope=access_scope,
                                  database=database)['internal_schema_graph']


def select_evidence_ids(evidence: list[dict], *, progress: Any = None, requests: Any = None,
                        observation: Any = None, recent: int = 2) -> list[str]:
    """Select real records referenced by active progress or upcoming dependencies.

    This selects IDs only. Callers must still use their scoped result reader to
    load actual numbers; a directory or a model-authored fact is not that data.
    """
    valid = [e for e in evidence if e.get('check_status') not in {'superseded', 'invalid', 'failed'}
             and e.get('id')]
    by_id = {str(e['id']): e for e in valid}
    selected = {str(e['id']) for e in valid[-max(0, recent):]} if recent > 0 else set()

    def walk(value: Any):
        if isinstance(value, dict):
            if value.get('status') in {'invalid', 'superseded', 'removed'}:
                return
            for key, item in value.items():
                if key in {'evidence_ids', 'input_result_ids', 'result_ids'} and isinstance(item, list):
                    selected.update(str(r) for r in item if str(r) in by_id)
                elif key in {'evidence_id', 'result_id', 'source_result_id'} and str(item) in by_id:
                    selected.add(str(item))
                elif key == 'result' and isinstance(item, dict):
                    if str(item.get('id')) in by_id:
                        selected.add(str(item['id']))
                    walk(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for value in (progress, requests, observation):
        walk(value)
    pending = list(selected)
    while pending:
        for ref in by_id[pending.pop()].get('input_result_ids') or []:
            if ref in by_id and ref not in selected:
                selected.add(ref)
                pending.append(ref)
    return [str(e['id']) for e in valid if str(e['id']) in selected]
