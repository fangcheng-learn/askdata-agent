"""Small measurement requests assembled from already acquired public metadata.

This is neither another metric registry nor a SQL semantic proof engine. Unknown
business terms remain explicit local-definition requests; they are not silently
promoted to registered metrics or mistaken for unavailable data.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

import sqlglot
from sqlglot import exp

from .entity_resolution import TIME_GRAINS, is_time_grain_term, resolve_dimension

from .query_scope import TIME_CONDITIONS as _CONDITION_ENVELOPES


def _text(value: Any) -> str:
    return str(value or '').strip().casefold()


def _physical_refs(values: Any, graph: dict) -> list[str]:
    tables = {str(t.get('id')): str(t.get('name')) for t in graph.get('tables') or []
              if t.get('id') and t.get('name')}
    result = []
    for value in values or []:
        if isinstance(value, dict):
            table = value.get('table') or value.get('table_name') or value.get('table_id')
            field = value.get('field') or value.get('field_name')
            reference = f'{tables.get(str(table), str(table))}.{field}' if table and field else value.get('sql_name')
        else:
            reference = str(value)
            if '.' in reference:
                table, field = reference.rsplit('.', 1)
                reference = f'{tables.get(table, table)}.{field}'
        if reference and reference not in result:
            result.append(reference)
    return result


def _entity(metric: dict) -> tuple[str | None, str | None]:
    explicit = metric.get('statistical_entity') or metric.get('entity')
    if explicit:
        return str(explicit), 'registered_metadata'
    # Unit mappings are exact public metadata values, never a guess from the
    # user's wording or SQL alias. They are descriptive, not an admission rule.
    unit = _text(metric.get('unit'))
    entity = {'orders': 'orders', 'sessions': 'sessions', 'users': 'users',
              'visitors': 'visitors', 'units': 'goods_units', 'pieces': 'goods_units',
              'events': 'events', 'ratio': 'ratio'}.get(unit)
    if entity is None and unit.startswith('eur_cents'):
        entity = ('monetary_amount_per_order' if '_per_order' in unit else 'monetary_amount')
    return (entity, 'registered_unit' if entity else None)


def compact_metric(metric: dict, graph: dict | None = None) -> dict:
    """One physical reference per dependency and no repeated self-definition."""
    graph = graph or {}
    keep = ('id', 'label', 'aliases', 'definition', 'formula', 'unit', 'grain', 'depends_on',
            'required_predicates', 'maturity', 'tax_policy', 'shipping_policy', 'refund_policy',
            'duplicate_risks', 'as_of_policy', 'statistical_entity', 'entity', 'counting_key',
            'aggregation', 'population', 'cohort', 'version', 'semantic_version', 'source_version')
    result = {key: deepcopy(metric[key]) for key in keep if key in metric}
    if result.get('formula') == result.get('definition'):
        result.pop('formula', None)
    for name in ('required_fields', 'source_fields', 'time_fields'):
        if metric.get(name):
            result[name] = _physical_refs(metric[name], graph)
    policies = []
    for policy in metric.get('component_policies') or []:
        item = {key: deepcopy(policy[key]) for key in (
            'metric_id', 'definition', 'required_predicates', 'maturity', 'unit', 'grain', 'cohort',
            'population', 'counting_key', 'duplicate_risks', 'as_of_policy') if key in policy}
        if policy.get('time_fields'):
            item['time_fields'] = _physical_refs(policy['time_fields'], graph)
        if item.get('metric_id') == metric.get('id'):
            for key in ('definition', 'required_predicates', 'maturity', 'unit', 'grain'):
                if item.get(key) == result.get(key):
                    item.pop(key, None)
            if item.get('time_fields') == result.get('time_fields'):
                item.pop('time_fields', None)
        if set(item) != {'metric_id'} and item not in policies:
            policies.append(item)
    if policies:
        result['component_policies'] = policies
    return result


def measurement_card(metric: dict, graph: dict) -> dict:
    compact = compact_metric(metric, graph)
    entity, entity_basis = _entity(metric)
    fields = _physical_refs(metric.get('required_fields') or metric.get('source_fields'), graph)
    card = {'id': metric.get('id') or metric.get('name'), 'kind': 'registered',
            'definition': compact.get('definition') or compact.get('formula') or '',
            'unit': metric.get('unit'), 'statistical_entity': entity,
            'entity_basis': entity_basis, 'source_fields': fields,
            'event_time': _physical_refs(metric.get('time_fields'), graph),
            'required_predicates': deepcopy(metric.get('required_predicates') or []),
            'grain': metric.get('grain'), 'maturity': metric.get('maturity')}
    for key in ('counting_key', 'aggregation', 'population', 'cohort', 'component_policies',
                'tax_policy', 'shipping_policy', 'refund_policy', 'duplicate_risks', 'as_of_policy'):
        if compact.get(key) is not None:
            card[key] = compact[key]
    if compact.get('formula'):
        card['formula'] = compact['formula']
    version = metric.get('semantic_version') or metric.get('source_version') or metric.get('version')
    card['version'] = str(version or hashlib.sha256(json.dumps(compact, ensure_ascii=False, sort_keys=True,
                                                            default=str).encode()).hexdigest()[:16])
    return {key: value for key, value in card.items() if value not in (None, '', [])}


def resolve_query_requirements(queries: list[dict], graph: dict, metrics: list[dict]) -> dict:
    """Resolve declared business names without a model or an extra lookup.

    ``metric_ids``/``requested_fields`` are selection inputs for the existing
    context assembler. Unknown terms still require targeted candidate metadata
    and a clear task-local definition in the SQL response, or an explicit gap.
    """
    selected_ids: list[str] = []
    wanted_fields: list[str] = []
    unresolved: list[dict] = []
    resolved = []
    field_specs = graph.get('fields') or []
    by_metric = {str(m.get('id') or m.get('name')): m for m in metrics if m.get('id') or m.get('name')}
    for index, query in enumerate(queries):
        qid = str(query.get('query_id') or query.get('id') or f'query-{index+1}')
        cards, query_gaps = [], []
        names = query.get('metric_requirements') or query.get('metric_ids') or []
        for name in names:
            matching = [m for m in metrics if _text(name) in {_text(v) for v in
                        [m.get('id'), m.get('name'), m.get('label'), *(m.get('aliases') or [])] if v}]
            if len(matching) == 1:
                card = measurement_card(matching[0], graph)
                if card['id'] not in selected_ids:
                    selected_ids.append(card['id'])
                if not any(c['id'] == card['id'] for c in cards):
                    cards.append(card)
                wanted_fields.extend(card.get('source_fields') or [])
                wanted_fields.extend(card.get('event_time') or [])
                continue
            if len(matching) > 1:
                gap = {'query_id': qid, 'request': name, 'kind': 'ambiguous_measure',
                       'candidates': [m.get('id') for m in matching],
                       'instruction': '多个公开指标名称匹配；核对目标，实质歧义不能猜来源。'}
            else:
                field_matches = [f for f in field_specs if _text(name) in {_text(v) for v in
                                 [f.get('id'), f.get('sql_name'), str(f.get('sql_name') or '').rsplit('.', 1)[-1], f.get('name'), f.get('label'),
                                  *(f.get('aliases') or [])] if v}]
                if len(field_matches) == 1:
                    field = field_matches[0]
                    reference = field.get('sql_name') or field.get('id')
                    wanted_fields.append(reference)
                    time_candidates = [candidate for candidate in field_specs
                        if candidate.get('table_id') == field.get('table_id') and
                        (candidate.get('role') == 'time' or any(kind in str(candidate.get('sql_type') or
                         candidate.get('type') or '').upper() for kind in ('DATE', 'TIME')) or
                         candidate.get('type') == '日期')]
                    candidate_refs = [candidate.get('sql_name') or candidate.get('id') for candidate in time_candidates]
                    wanted_fields.extend(ref for ref in candidate_refs if ref)
                    cards.append({'id': str(name), 'kind': 'field_measure', 'source_fields': [reference],
                                  'unit': field.get('unit'), 'aggregation': field.get('aggregation'),
                                  'event_time_candidates': candidate_refs,
                                  'definition': field.get('description') or field.get('label') or '',
                                  'instruction': '真实字段不是已登记业务指标；按目标和公开说明明确聚合、集合及事件时间，候选时间不等于已确认口径。'})
                    continue
                gap = {'query_id': qid, 'request': name,
                       'kind': 'ambiguous_measure' if field_matches else 'unresolved_measure',
                       'instruction': '未解析为已登记指标；可据公开字段给出明确任务内定义，或返回资料缺口。不得视为数据不支持或冒充登记指标。'}
            query_gaps.append(gap)
            unresolved.append(gap)
        dimension_terms = [(value, False) for value in query.get('dimensions') or []]
        dimension_terms += [(value, True) for value in (query.get('conditions') or {}).keys()]
        dimension_bindings = []
        for dimension, condition_key in dimension_terms:
            if condition_key and _text(dimension) in _CONDITION_ENVELOPES:
                continue
            resolution = resolve_dimension(dimension, graph)
            dimension_bindings.append(resolution)
            if resolution['status'] == 'resolved':
                wanted_fields.append(resolution['field'])
            elif resolution['status'] != 'time_grain':
                candidates = resolution.get('candidates') or []
                # Keep acquired candidates in the SQL view; an unresolved
                # business spelling is not evidence that a field is absent.
                wanted_fields.extend(item['sql_name'] for item in candidates)
                gap = {'query_id': qid, 'request': dimension, 'kind': 'unresolved_dimension',
                       'resolution': resolution['status'], 'technical_candidates': candidates,
                       'instruction': '由SQL模块结合本轮真实字段候选完成技术映射；不是数据不支持，不应让用户选择物理字段。实质统计集合歧义仍须说明或作业务澄清。'}
                query_gaps.append(gap)
                unresolved.append(gap)
        resolved.append({'query_id': qid, 'goal': query.get('goal') or '', 'measurements': cards,
                         'unresolved_requirements': query_gaps,
                         'conditions': deepcopy(query.get('conditions') or {}),
                         'dimensions': deepcopy(query.get('dimensions') or []),
                         'dimension_bindings': dimension_bindings,
                         'input_result_ids': deepcopy(query.get('input_result_ids') or [])})
    pending = list(selected_ids)
    while pending:
        for dependency in by_metric.get(pending.pop(), {}).get('depends_on') or []:
            if dependency in by_metric and dependency not in selected_ids:
                selected_ids.append(dependency)
                pending.append(dependency)
                card = measurement_card(by_metric[dependency], graph)
                wanted_fields.extend(card.get('source_fields') or [])
                wanted_fields.extend(card.get('event_time') or [])
    grouping = []
    for request in resolved:
        groups: dict[str, dict] = {}
        for card in request['measurements']:
            basis = {'event_time': card.get('event_time') or [], 'cohort': card.get('cohort'),
                     'population': card.get('population')}
            key = json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str)
            groups.setdefault(key, {**basis, 'metric_ids': []})['metric_ids'].append(card['id'])
        grouping.append({'query_id': request['query_id'], 'time_basis_groups': list(groups.values()),
                         'instruction': '不同统计集合不能强套同一时间筛选；分别聚合或由调用方先绑定独立执行项。'})
    return {'queries': resolved, 'by_query': {q['query_id']: q for q in resolved},
            'metric_ids': selected_ids, 'requested_fields': list(dict.fromkeys(v for v in wanted_fields if v)),
            'unresolved_requirements': unresolved, 'grouping': grouping}


def repair_diagnostics(error: Any, *, sql: str = '', query_contract: dict | None = None,
                       measurements: Any = None, schema_graph: dict | None = None) -> dict:
    """Actionable diagnostic using only known error facts and public cards.

    Descriptive hints are not another validator: unproven expressions remain
    unproven, and no suggested full SQL or private answer is manufactured.
    """
    raw = str(error.get('message') or error.get('error') or error) if isinstance(error, dict) else str(error)
    graph = schema_graph or {}
    known = {f.get('sql_name'): f for f in graph.get('fields') or [] if f.get('sql_name')}
    cards = []
    if isinstance(measurements, dict):
        for request in measurements.get('queries') or []:
            cards.extend(request.get('measurements') or [])
        if 'measurements' in measurements:
            cards.extend(measurements['measurements'])
    elif isinstance(measurements, list):
        cards = measurements
    matches = [c for c in cards if str(c.get('id') or '') in raw]
    declared = (query_contract or {}).get('metrics') or []
    if not matches:
        matches = [c for c in cards if any(m.get('name') == c.get('id') for m in declared)]
    details = []
    for card in matches:
        metric = next((m for m in declared if m.get('name') == card.get('id')), {})
        actual = _physical_refs(metric.get('source_fields') or [], graph)
        details.append({'metric_id': card.get('id'), 'actual_source_fields': actual,
                        'source_fields_basis': 'query_contract_declaration_not_full_sql_lineage',
                        'declared_source_fields': actual,
                        'actual_field_units': {ref: known[ref].get('unit') for ref in actual if ref in known},
                        'expected_statistical_entity': card.get('statistical_entity'),
                        'expected_unit': card.get('unit'), 'expected_event_time': card.get('event_time') or [],
                        'expected_source_fields': card.get('source_fields') or [],
                        'expected_grain': card.get('grain'), 'expected_cohort': card.get('cohort'),
                        'declared_source_roles': {ref: known[ref].get('role') for ref in actual if ref in known},
                        'registered_definition': card.get('definition') or '',
                        'instruction': '修正真正的统计对象、集合或时间；不能只改指标名或把会话改名成订单。'
                            '区分度量取值/计算来源与分组、筛选、关联来源；不要把所有使用过的字段都抄进指标source_fields。'
                            '若当前业务目标的粒度或cohort不同于登记指标，应保留业务目标，明确任务内定义、scope、来源及事件时间，'
                            '用对应实际SQL别名绑定local_measurements；不能只删报错字段却继续冒用登记ID。'})
    expressions = []
    try:
        tree = sqlglot.parse_one(sql, read='duckdb')
        expressions = [node.sql(dialect='duckdb') for node in tree.find_all(exp.AggFunc)]
    except (sqlglot.errors.ParseError, TypeError):
        pass
    category = ('permission' if isinstance(error, PermissionError) else
                'measurement_conflict' if any(v in raw for v in ('指标来源', '计量单位', '声明', '口径', '聚合')) else
                'sql_compile' if any(v in raw for v in ('编译', 'Binder Error', 'Parser Error', 'Catalog Error')) else
                'input_or_output_contract')
    return {'stage': 'query_preparation_or_execution', 'kind': category, 'raw_error': raw,
            'measurement_conflicts': details, 'actual_aggregate_expressions': expressions,
            'instruction': '保持用户目标与已确定范围，依据本次具体诊断实质修正失败项；未证明的业务等价不要冒充已验证。'}


def _semantic_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def measurement_lineage(sql: str, contract: dict, metrics: list[dict], graph: dict, *,
                        local_measurements: list[dict] | None = None,
                        declared_metrics: list[dict] | None = None) -> dict:
    """Describe output meaning without certifying the business implementation.

    A binding requires a real output alias or an existing validator binding.
    Shared physical sources alone never identify an arbitrary derived output.
    Missing, conflicting and unbound definitions remain reviewable observations,
    not another SQL execution gate.
    """
    output: dict[str, Any] = {'column_metadata': {}, 'task_local_measurements': [],
                              'unbound_measurements': [], 'pending_checks': []}
    try:
        statement = sqlglot.parse_one(sql, read='duckdb')
        names = [node.alias_or_name for node in statement.selects if node.alias_or_name]
        projections = {node.alias_or_name: node for node in statement.selects
                       if node.alias_or_name and names.count(node.alias_or_name) == 1}
    except (sqlglot.errors.ParseError, TypeError):
        projections = {}
    bindings = (contract.get('validation') or {}).get('metric_output_bindings') or []
    validated = contract.get('metrics') or []
    declarations = declared_metrics if declared_metrics is not None else validated
    claims: dict[str, list[dict]] = {}

    def pending(code: str, message: str, **detail: Any) -> None:
        item = {'code': code, 'message': message, **detail}
        if item not in output['pending_checks']:
            output['pending_checks'].append(item)

    def aliases_for(identities: set[str], binding_identities: set[str] | None = None) -> set[str]:
        aliases = set(identities)
        aliases.update(alias for binding in bindings if binding.get('declared_name') in (binding_identities or identities)
                       for alias in binding.get('output_aliases') or [])
        return aliases & projections.keys()

    def claim(identities: set[str], metadata: dict, *, snapshot: dict | None = None,
              binding_identities: set[str] | None = None) -> None:
        aliases = aliases_for(identities, binding_identities)
        if snapshot is not None:
            snapshot['bound_output_columns'] = sorted(aliases)
        if not aliases:
            missing = {'measurement_id': metadata['measurement_id'], 'kind': metadata['kind'],
                       'definition': metadata.get('definition') or '',
                       'measurement_version': metadata.get('measurement_version'),
                       'reason': '实际输出别名及已有验证绑定未唯一指向该定义'}
            if missing not in output['unbound_measurements']:
                output['unbound_measurements'].append(missing)
            pending('measurement_output_unbound', missing['reason'],
                    measurement_id=metadata['measurement_id'], kind=metadata['kind'])
        for alias in aliases:
            claims.setdefault(alias, []).append(deepcopy(metadata))

    def registered_candidates(name: Any) -> list[dict]:
        # Resolve identity, never infer equivalence from shared source fields.
        by_id = {str(m.get('id') or m.get('name')): m for m in metrics if _text(name) in
                 {_text(v) for v in [m.get('id'), m.get('name'), m.get('label'), *(m.get('aliases') or [])] if v}}
        return list(by_id.values())

    registered_names: set[str] = set()
    formal: dict[str, tuple[dict, set[str], set[str]]] = {}
    for index, declared in enumerate(declarations):
        name = str(declared.get('name') or '')
        candidates = registered_candidates(name)
        if len(candidates) != 1:
            continue
        metric = candidates[0]
        registered_names.add(name)
        card = measurement_card(metric, graph)
        _, identities, binding_identities = formal.setdefault(str(card['id']), (card, set(), set()))
        identities.update(str(v) for v in [card['id'], metric.get('name'), metric.get('label'),
                                         name, *(metric.get('aliases') or [])] if v)
        binding_identities.update(identities)
        if index < len(validated):
            binding_identities.add(str(validated[index].get('name') or ''))
    for card, identities, binding_identities in formal.values():
        aliases = aliases_for(identities, binding_identities)
        base = {'kind': 'registered', 'measurement_id': card['id'], 'metric_id': card['id'],
                'definition': card.get('definition'), 'source_fields': card.get('source_fields') or [],
                'source_unit': card.get('unit'), 'statistical_entity': card.get('statistical_entity'),
                'event_time': card.get('event_time') or [], 'measurement_version': card.get('version'),
                'binding_basis': 'exact_alias_or_validator_binding', 'requires_review': True}
        for key in ('counting_key', 'population', 'cohort', 'required_predicates', 'duplicate_risks', 'as_of_policy'):
            if key in card:
                base[key] = deepcopy(card[key])
        if not aliases:
            claim(identities, base, binding_identities=binding_identities)
        for alias in aliases:
            metadata = deepcopy(base)
            unit = str(card.get('unit') or '')
            projection = projections[alias]
            divisions = [d for d in projection.find_all(exp.Div)
                         if isinstance(d.expression, exp.Literal) and d.expression.this in {'100', '100.0'}]
            physical_tables = {table.alias_or_name: table.name for table in statement.find_all(exp.Table)}
            direct_money_fields = {f"{physical_tables.get(column.table, column.table)}.{column.name}"
                                   for column in projection.find_all(exp.Column) if column.table}
            # An arbitrary CTE alias can have an earlier conversion. Only a
            # direct registered physical money source proves this local scale.
            known_money_fields = {field.get('sql_name') for field in graph.get('fields') or []
                                  if str(field.get('unit') or '').startswith('EUR_cents')}
            direct_money = bool(direct_money_fields & set(card.get('source_fields') or []) & known_money_fields)
            if unit.startswith('EUR_cents') and divisions and direct_money:
                metadata.update(unit=unit.replace('EUR_cents', 'EUR', 1), currency='EUR', precision=2,
                                display_conversion='division_by_100_in_output_expression')
            elif unit in {'orders', 'sessions', 'units', 'pieces', 'users', 'visitors', 'events'}:
                metadata.update(unit=unit)
            elif unit == 'ratio' and not projection.find(exp.Mul, exp.Div):
                metadata.update(unit='ratio')
            else:
                metadata['display_unit_check'] = '输出换算尚未机械确认；source_unit不是展示单位'
            claims.setdefault(alias, []).append({k: v for k, v in metadata.items() if v is not None})

    local_ids: set[str] = set()
    for item in local_measurements or []:
        if not isinstance(item, dict) or not str(item.get('id') or '').strip():
            pending('task_local_definition_incomplete', '任务内定义缺少稳定ID，已保留真实查询结果供核对')
            continue
        identity = str(item['id'])
        local_ids.add(identity)
        semantics = {key: deepcopy(item[key]) for key in (
            'id', 'definition', 'scope', 'statistical_entity', 'unit', 'formula', 'population', 'cohort')
            if key in item and item[key] is not None}
        semantics['source_fields'] = _physical_refs(item.get('source_fields'), graph)
        semantics['event_time'] = _physical_refs(item.get('event_time'), graph)
        semantic_hash = _semantic_hash(semantics)
        snapshot = {**semantics, 'kind': 'task_local', 'semantic_hash': semantic_hash,
                    'measurement_version': semantic_hash, 'requires_review': True,
                    'validation_status': 'requires_evidence_and_review'}
        output['task_local_measurements'].append(snapshot)
        metadata = {**snapshot, 'measurement_id': identity, 'metric_id': identity,
                    'binding_basis': 'exact_alias_or_validator_binding'}
        identities = {identity, *(str(v) for v in item.get('output_columns') or [])}
        binding_identities = set(identities)
        # A validator may normalize this declaration's name to a field label.
        # Preserve its business identity, but use only the recorded alias link.
        for index, declared in enumerate(declarations):
            if declared.get('name') == identity and index < len(validated):
                binding_identities.add(str(validated[index].get('name') or ''))
        missing = [key for key in ('definition', 'scope') if not str(item.get(key) or '').strip()]
        if missing:
            pending('task_local_definition_incomplete', '任务内定义缺少影响解释的必要说明',
                    measurement_id=identity, missing_fields=missing)
        claim(identities, metadata, snapshot=snapshot, binding_identities=binding_identities)

    for index, declared in enumerate(declarations):
        identity = str(declared.get('name') or '')
        if not identity or identity in registered_names or identity in local_ids:
            continue
        identities = {identity}
        binding_identities = set(identities)
        if index < len(validated):
            binding_identities.add(str(validated[index].get('name') or ''))
        metadata = {'kind': 'unresolved', 'measurement_id': identity, 'metric_id': identity,
                    'definition': declared.get('definition') or '',
                    'source_fields': _physical_refs(declared.get('source_fields'), graph),
                    'aggregation': declared.get('aggregation'), 'requires_review': True,
                    'binding_basis': 'exact_alias_or_validator_binding',
                    'semantic_status': 'task_local_definition_needed'}
        claim(identities, metadata, binding_identities=binding_identities)
        pending('task_local_definition_missing', '该业务量未绑定登记定义或本候选任务内定义；可查询，但用于结论前需核对含义及适用集合',
                measurement_id=identity, output_columns=sorted(aliases_for(identities, binding_identities)))

    for alias, candidates in claims.items():
        unique = {_semantic_hash(candidate): candidate for candidate in candidates}
        if len(unique) == 1:
            output['column_metadata'][alias] = next(iter(unique.values()))
        else:
            pending('measurement_output_ambiguous', '同一实际输出列存在不同指标定义，未猜测采用哪一份',
                    output_column=alias, measurement_ids=sorted({c['measurement_id'] for c in candidates}))
    return output


def output_measurements(sql: str, contract: dict, metrics: list[dict], graph: dict) -> dict[str, dict]:
    """Compatibility view over the same column binding used by execution."""
    return measurement_lineage(sql, contract, metrics, graph)['column_metadata']
