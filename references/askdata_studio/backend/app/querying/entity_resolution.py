"""Resolve business dimensions and entity values inside acquired, authorized metadata.

This module performs no database/model calls.  An optional caller-owned lookup
returns values for a field selected here; it never receives SQL from a model.
Unrecognised names are not converted to the only available entity by default.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from typing import Any, Callable

from ..security import AccessScope


TIME_GRAINS = frozenset({'day', 'week', 'month', 'quarter', 'year', 'date', 'period',
    '比较期间', '月份', '月', '日期', '时间', '期间', '季度', '年', '年月'})
_TIME_SUFFIXES = frozenset({'str', 'string', 'label', 'key', 'name', 'text', 'formatted',
                           'display', 'bucket', 'value', 'code'})
_CONCEPTS = {
    'channel': {'channel', 'source_channel', 'session_source_channel', 'traffic_channel',
                '渠道', '来源渠道', '会话来源渠道', '流量渠道'},
    'category': {'category', 'category_code', 'product_category', '商品品类', '品类',
                 '类目', '类别', '商品类目', '商品类目代码'},
    'store': {'store', 'shop', 'store_id', 'shop_id', 'store_name', 'shop_name',
              '店铺', '商店', '店铺名称', '经营店铺标识'},
    'sku': {'sku', 'sku_id', 'sku_name', 'sku标识', 'sku 标识', '商品名称'},
    'product': {'product', 'product_id', 'product_name', '商品', '商品编码'},
}
_ENTITY_CONCEPTS = frozenset({'store', 'category', 'sku', 'product', 'channel'})


def _text(value: Any) -> str:
    return unicodedata.normalize('NFKC', '' if value is None else str(value)).strip().casefold()


def _token(value: Any) -> str:
    return re.sub(r'[\s\-]+', '_', _text(value))


def is_time_grain_term(value: Any) -> bool:
    """Recognise time grouping/output labels without guessing a source field."""
    text = _token(value)
    if text in TIME_GRAINS:
        return True
    # month_str, order_month_label, display_quarter_key, etc. are grouping
    # hints. They do not select captured_at versus started_at or prove a cohort.
    parts = text.rsplit('.', 1)[-1].split('_')
    grains = {'day', 'week', 'month', 'quarter', 'year', 'date', 'period'}
    return (any(part in grains for part in parts)
            and parts[-1] in grains | _TIME_SUFFIXES
            and not any(char in text for char in "'();"))


def _concept(value: Any) -> str | None:
    text = _token(value).rsplit('.', 1)[-1]
    for concept, names in _CONCEPTS.items():
        if text in {_token(name) for name in names}:
            return concept
    return None


def _field_concept(field: dict) -> str | None:
    for value in (field.get('name'), field.get('label'), *(field.get('aliases') or [])):
        concept = _concept(value)
        if concept:
            return concept
    return None


def _fields(graph: dict, access_scope: Any = None, datasource_id: str | None = None) -> list[dict]:
    source = str(datasource_id or graph.get('database') or '')
    scope = (access_scope if isinstance(access_scope, AccessScope) else
             AccessScope.from_dict(access_scope) if access_scope is not None else None)
    tables = {}
    for table in graph.get('tables') or []:
        if not isinstance(table, dict) or not table.get('id') or not table.get('name'):
            continue
        database = str(table.get('database') or graph.get('database') or source)
        if database != source or (scope and not scope.allows_table(database, str(table['id']))):
            continue
        tables[str(table['id'])] = table
    result = []
    for original in graph.get('fields') or []:
        if not isinstance(original, dict) or not isinstance(original.get('name'), str):
            continue
        table = tables.get(str(original.get('table_id')))
        if not table:
            continue
        if original.get('database') and str(original['database']) != source:
            continue
        if original.get('datasource_id') and str(original['datasource_id']) != source:
            continue
        # Derive physical identities from trusted table membership, not a
        # model-provided sql_name that could point outside the selected scope.
        field = deepcopy(original)
        field.update(table_id=table['id'], table_name=table['name'],
                     sql_name=f"{table['name']}.{original['name']}",
                     datasource_id=source)
        field['_primary_key'] = original['name'] in (table.get('primary_key') or [])
        result.append(field)
    return result


def _public_field(field: dict) -> dict:
    result = {key: deepcopy(field[key]) for key in
            ('id', 'sql_name', 'name', 'label', 'table_id', 'table_name', 'datasource_id',
             'role', 'description', 'aliases', 'unit', 'sql_type') if key in field}
    result['field_name'] = field['name']
    return result


def resolve_dimension(term: Any, graph: dict, *, access_scope: Any = None,
                      datasource_id: str | None = None) -> dict:
    """Return one authorised field or technical candidates; never ask for SQL names."""
    result = {'request': term, 'status': 'unresolved', 'candidates': []}
    if not isinstance(term, str) or not term.strip():
        return result
    available = _fields(graph, access_scope, datasource_id)
    exact = [field for field in available if _text(term) in {_text(value) for value in
             (field.get('id'), field['sql_name'], field.get('name'), field.get('label'),
              *(field.get('aliases') or [])) if value}]
    explicit = [field for field in exact if _text(term) in {_text(field.get('id')), _text(field['sql_name'])}]
    matching = explicit or exact
    basis = 'exact_metadata'
    if not matching and is_time_grain_term(term):
        return {**result, 'status': 'time_grain', 'basis': 'time_grouping_hint'}
    concept = _concept(term)
    if not matching and concept:
        matching = [field for field in available if _field_concept(field) == concept]
        basis = 'business_dimension_concept'
    if not matching:
        return result
    # A present qualifier disambiguates only a real candidate in that table.
    # A hallucinated table.field spelling does not erase valid candidates in
    # the acquired schema (e.g. order_items.category -> sku_catalog.category_code).
    qualifier = _text(term.rsplit('.', 1)[0]) if '.' in term else ''
    qualified = [field for field in matching if qualifier in
                 {_text(field['table_name']), _text(field['table_id'])}] if qualifier else []
    if qualified:
        matching = qualified
    tail = _token(term).rsplit('.', 1)[-1]
    desired = 'name' if tail.endswith('_name') else 'id' if tail.endswith('_id') else None
    specialised = [field for field in matching if _token(field['name']).endswith('_' + desired)] if desired else []
    if specialised:
        matching = specialised
    # Foreign-key copies identify the same entity but may change the counting
    # population. Prefer a unique authoritative identifier for an unqualified
    # entity request; otherwise retain candidates for the SQL role to bind.
    authoritative = [field for field in matching if field.get('_primary_key') or field.get('role') == 'identifier']
    if not qualifier and len(authoritative) == 1:
        matching = authoritative
    candidates = [_public_field(field) for field in matching]
    if len(candidates) == 1:
        return {**result, 'status': 'resolved', 'field': candidates[0]['sql_name'],
                'field_info': candidates[0], 'candidates': candidates, 'basis': basis,
                'concept': concept or _field_concept(matching[0])}
    return {**result, 'status': 'technical_candidates', 'candidates': candidates,
            'basis': basis, 'concept': concept}


def _values(field: dict) -> tuple[list[Any], bool]:
    values = []
    complete = isinstance(field.get('enum_values'), list)
    sources = [field.get('enum_values'), field.get('samples')]
    for name in ('profile', 'data_profile'):
        profile = field.get(name)
        if isinstance(profile, dict):
            sources.extend(profile.get(key) for key in ('top_values', 'values', 'samples'))
    for source in sources:
        if not isinstance(source, list):
            continue
        for raw in source:
            value = raw.get('value') if isinstance(raw, dict) else raw
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value not in values:
                values.append(value)
    return values, complete


def _requested_values(raw: Any) -> tuple[list[Any] | None, Callable[[list[Any]], Any]]:
    if isinstance(raw, dict):
        operator = _text(raw.get('operator') or '=')
        if operator not in {'=', 'eq', 'in'}:
            return None, lambda values: raw
        key = 'values' if 'values' in raw else 'value'
        values, _ = _requested_values(raw.get(key))
        return values, lambda canonical: {**deepcopy(raw), key: canonical if isinstance(raw.get(key), list) else canonical[0]}
    if isinstance(raw, list):
        valid = all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in raw)
        return (raw if valid and raw else None), lambda canonical: canonical
    if isinstance(raw, (str, int, float)) and not isinstance(raw, bool) and str(raw).strip():
        return [raw], lambda canonical: canonical[0]
    return None, lambda values: raw


def prepare_entity_context(requests: list[dict], graph: dict, *, access_scope: Any,
                           datasource_id: str, lookup_values: Callable | None = None) -> dict:
    """Prepare mappings and human entity clarification, without changing task intent.

    ``lookup_values(field_info, limit=100)`` is caller-owned and must enforce
    task/user/snapshot permissions.  ``field_info`` contains only schema-selected
    identities plus requested_values as *data*, never a SQL expression. Return
    ``{'values': [...], 'complete': bool}``; a bare list is accepted but does not
    certify catalog completeness.  Results are used only in this function call.
    """
    normalised = deepcopy(requests)
    output = {'normalized_requests': normalised, 'resolutions': [], 'clarification': None,
              'missing_requests': [], 'requested_fields': []}
    fields = {field['sql_name']: field for field in _fields(graph, access_scope, datasource_id)}
    cache = {}
    for index, query in enumerate(normalised):
        qid = str(query.get('query_id') or query.get('id') or f'query-{index + 1}')
        dimensions = []
        for dimension in query.get('dimensions') or []:
            resolution = resolve_dimension(dimension, graph, access_scope=access_scope, datasource_id=datasource_id)
            output['resolutions'].append({'query_id': qid, 'kind': 'dimension', **resolution})
            if resolution['status'] == 'resolved':
                dimensions.append(resolution['field'])
                output['requested_fields'].append(resolution['field'])
            else:
                dimensions.append(dimension)
                if resolution['status'] != 'time_grain':
                    output['missing_requests'].append({'query_id': qid, 'kind': 'technical_dimension_mapping', **resolution,
                        'instruction': '由SQL模块结合真实候选完成技术绑定；不要要求用户选择物理字段，也不等于数据不支持。'})
        if 'dimensions' in query:
            query['dimensions'] = list(dict.fromkeys(dimensions))
        conditions = query.get('conditions')
        if not isinstance(conditions, dict):
            continue
        updated = deepcopy(conditions)
        for key, raw_value in conditions.items():
            resolved = resolve_dimension(key, graph, access_scope=access_scope, datasource_id=datasource_id)
            concept = resolved.get('concept') or _concept(key)
            if concept not in _ENTITY_CONCEPTS:
                continue
            requested, rebuild = _requested_values(raw_value)
            if requested is None:
                continue
            record = {'query_id': qid, 'kind': 'entity', 'original_condition': key,
                      'original_value': deepcopy(raw_value), 'requested_values': deepcopy(requested),
                      'status': 'unresolved', 'candidates': resolved.get('candidates') or []}
            if resolved['status'] != 'resolved':
                output['resolutions'].append(record)
                output['missing_requests'].append({**record, 'kind': 'technical_entity_mapping',
                    'instruction': '先补齐实体字段候选，不把名称直接当作标识值，不让用户选择物理字段。'})
                continue
            reference = resolved['field']
            field = fields[reference]
            values, complete = _values(field)
            value_source = 'metadata_enum' if complete else 'metadata_samples'
            cache_key = (reference, tuple(_text(value) for value in requested))
            if lookup_values and (not values or any(not any(_text(value) == _text(candidate) for candidate in values) for value in requested)):
                if cache_key not in cache:
                    info = {**resolved['field_info'], 'requested_values': deepcopy(requested)}
                    cache[cache_key] = lookup_values(info, limit=100)
                found = cache[cache_key]
                found_values = found.get('values') if isinstance(found, dict) else found
                if not isinstance(found_values, list):
                    raise ValueError('实体值检索必须返回真实values数组')
                values = []
                for value in found_values:
                    if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value not in values:
                        values.append(value)
                complete = found.get('complete') is True if isinstance(found, dict) else False
                value_source = 'authorized_value_lookup'
            record.update(value_source=value_source, values_complete=complete)
            matched = [[candidate for candidate in values if _text(candidate) == _text(value)] for value in requested]
            if all(len(matches) == 1 for matches in matched):
                canonical = [matches[0] for matches in matched]
                if reference in updated and reference != key and updated[reference] != rebuild(canonical):
                    record['status'] = 'conflicting_conditions'
                    output['missing_requests'].append({**record, 'kind': 'conflicting_entity_conditions',
                        'instruction': '同一实体字段有互相冲突的条件，不能静默覆盖。'})
                    if output['clarification'] is None:
                        output['clarification'] = {'parameter': 'scope', 'category': 'entity_identity',
                            'question': '同一分析对象有两项不同的筛选条件，应以哪项为准？',
                            'reason': '已确认的实体条件互相冲突，不能静默覆盖或猜测用户范围。',
                            'options': [], 'query_id': qid}
                else:
                    updated.pop(key, None)
                    updated[reference] = rebuild(canonical)
                    record.update(status='resolved', canonical_field=reference,
                                  canonical_value=rebuild(canonical), basis='exact_available_entity_value')
                    output['requested_fields'].append(reference)
            else:
                # A truncated DISTINCT preview cannot establish that an entity
                # does not exist. Ask for an exact lookup, not a different name.
                ambiguous_value = any(len(matches) > 1 for matches in matched)
                record.update(status='needs_clarification' if complete or ambiguous_value else 'lookup_required',
                              canonical_field=reference, available_values=values[:100], values_complete=complete)
                if record['status'] == 'lookup_required':
                    output['missing_requests'].append({**record, 'kind': 'entity_values_required',
                        'instruction': '样例或截断目录不代表全集，须在当前授权快照对 requested_values 做精确参数化检索；不能据预览宣称对象不存在或猜名称对应的标识。'})
                elif output['clarification'] is None:
                    label = {'store': '店铺', 'category': '品类', 'sku': 'SKU', 'product': '商品', 'channel': '渠道'}[concept]
                    quoted = '、'.join(str(value) for value in requested)
                    output['clarification'] = {'parameter': 'scope', 'category': 'entity_identity',
                        'question': f'你说的{label}“{quoted}”具体对应哪个对象？请提供真实名称或标识。',
                        'reason': '当前授权数据中的实体值未能唯一对应用户称呼，不能自动替换为唯一候选或将无匹配当作零。',
                        'options': [{'id': str(value), 'label': str(value), 'description': f'当前数据中可识别的{label}，尚需你确认对应关系',
                                     'recommended': False, 'metric_sources': {}} for value in values[:8]],
                        'query_id': qid}
            output['resolutions'].append(record)
        query['conditions'] = updated
    output['requested_fields'] = list(dict.fromkeys(output['requested_fields']))
    return output
