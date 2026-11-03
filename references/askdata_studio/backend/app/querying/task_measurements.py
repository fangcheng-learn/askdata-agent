"""Task-scoped semantic memory derived from immutable evidence, not chat summaries.

No data rows or invented definitions. Same output names are not semantic identity;
conflicting versions stay visible. Every actual result read remains scoped by the
existing result reader.
"""
from copy import deepcopy
import json


def norm(value):
    return str(value or '').strip().casefold()


def measurement_field_refs(definition):
    """Keep scalar field names intact; schema assembly still checks existence."""
    refs = []
    for key in ('source_fields', 'event_time'):
        value = definition.get(key)
        if value is None or value == '':
            continue
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError(f'{key}必须是字段名或字段名数组，不能推测时间字段')
        refs.extend(values)
    return refs


def canonical_name(task, value):
    value = norm(value)
    for pool, field in [(task.metric_pool, 'metric'), (task.field_pool, 'field')]:
        matches = {str(key if field == 'metric' else item.get('sql_name') or key)
                   for key, item in pool.items() if value in {norm(v) for v in
                   [key, item.get('sql_name'), item.get('label'), item.get('name'), *(item.get('aliases') or [])] if v}}
        if matches:
            if len(matches) == 1:
                return next(iter(matches))
            if field == 'metric':
                return value
            # Reuse and query preparation must use the same authoritative
            # entity resolution. Multiple FK copies alone are not ambiguity
            # when the acquired schema identifies one canonical entity key.
            break
    from .entity_resolution import resolve_dimension
    dimension = resolve_dimension(value,task.state.get('schema_graph') or
        {'tables':list(task.table_pool.values()),'fields':list(task.field_pool.values())},
        access_scope=task.state.get('access_scope'),datasource_id=task.state.get('datasource_id'))
    return dimension['field'] if dimension.get('status')=='resolved' else value


def canonical_conditions(value, canonical):
    if isinstance(value, dict):
        # Identical aliases collapse, conflicting values remain separate.
        pairs = [(canonical(k),canonical_conditions(v,canonical)) for k,v in value.items()]
        unique = {json.dumps(p,ensure_ascii=False,sort_keys=True,default=str):p for p in pairs}
        return sorted(unique.values(),key=lambda x:(x[0],json.dumps(x[1],sort_keys=True,default=str)))
    if isinstance(value, list):
        return [canonical_conditions(v, canonical) for v in value]
    return value


def inherit_definitions(resolution, memory):
    by_name = {}
    for definition in memory['definitions']:
        if definition.get('kind') == 'task_local':
            by_name.setdefault(norm(definition.get('measurement_id')), []).append(definition)
    for query in resolution.get('queries') or []:
        remaining = []
        for gap in query.get('unresolved_requirements') or []:
            matching = by_name.get(norm(gap.get('request')), [])
            if gap['kind'] == 'unresolved_measure' and len(matching) == 1:
                definition = deepcopy(matching[0])
                query['measurements'].append({**definition, 'id': definition['measurement_id'],
                    'inheritance': 'prior_task_evidence_definition_not_registered'})
                resolution['unresolved_requirements'].remove(gap)
            else:
                remaining.append(gap)
        query['unresolved_requirements'] = remaining


def valid_evidence(task):
    state = task.state
    identity = {'task_id': state['task_id'], 'user_id': state['access_scope']['user_id'],
                'datasource_id': state['datasource_id']}
    return [e for e in task.evidence if e.get('check_status') not in
            {'superseded', 'invalid', 'failed'} and
            all(e.get(k, v) == v for k, v in identity.items()) and
            (not e.get('snapshot') or e['snapshot'] == (state.get('scenario') or {}))]


def measurement_memory(task, names=None):
    wanted = {norm(canonical_name(task, n)) for n in names or []}
    actions = {a['id']: a for a in task.actions}
    definitions, results, conflicts = {}, [], []
    versions = {}
    for evidence in valid_evidence(task):
        validation = (evidence.get('query_contract') or {}).get('validation') or {}
        metadata = evidence.get('column_metadata') or validation.get('column_metadata') or {}
        request = (actions.get(evidence.get('step_id'), {}).get('business_request') or {})
        requested_match = bool(wanted & {norm(canonical_name(task, n)) for n in request.get('metric_requirements') or []})
        outputs = []
        for column in evidence.get('columns') or []:
            meaning = deepcopy(metadata.get(column) or {})
            identity = meaning.get('measurement_id') or meaning.get('metric_id')
            if wanted and not requested_match and not {norm(canonical_name(task,column)), norm(canonical_name(task,identity))} & wanted:
                continue
            item = {'column': column, 'measurement_id': identity,
                    'kind': meaning.get('kind', 'observed_output'),
                    'semantic_status': meaning.get('semantic_status') or
                        ('requires_evidence_and_review' if meaning else 'definition_not_declared')}
            if meaning:
                key = json.dumps(meaning, sort_keys=True, ensure_ascii=False, default=str)
                record = definitions.setdefault(key, {**meaning, 'evidence_ids': []})
                if evidence['id'] not in record['evidence_ids']:
                    record['evidence_ids'].append(evidence['id'])
                item['measurement_version'] = meaning.get('measurement_version')
                if identity and meaning.get('measurement_version'):
                    versions.setdefault(identity, {}).setdefault(meaning['measurement_version'], []).append(evidence['id'])
            outputs.append(item)
        if outputs:
            action = actions.get(evidence.get('step_id'), {})
            results.append({'result_id': evidence['id'], 'outputs': outputs,
                            'complete': bool(evidence.get('complete')),
                            'conditions': (action.get('business_request') or {}).get('conditions') or {},
                            'dimensions': (action.get('business_request') or {}).get('dimensions') or [],
                            'question_ids': evidence.get('question_ids') or [],
                            'result_kind': evidence.get('result_kind','query_result'),
                            'pending_calculation': evidence.get('pending_calculation')})
    for identity, variants in versions.items():
        if len(variants) > 1:
            conflicts.append({'measurement_id': identity, 'versions': variants,
                'instruction': '同名度量有不同版本或适用范围；按证据范围选择，不覆盖、不直接合并对比。'})
    return {'definitions': list(definitions.values()), 'results': results, 'conflicts': conflicts,
            'policy': '来自本任务真实结果，非永久登记。observed_output仅说明已有该列，不证明指标口径正确；先读取原结果及SQL核对，不因漏声明就重新查询。同名不同版本不能静默替换。'}


def covered_result_ids(task, previous, query, canonical):
    """Reuse a known projection only at the identical requested grain/scope.

    Definitions must actually bind an output. Extra undeclared columns stay in
    the directory for inspection, but never establish automatic equivalence.
    """
    if query.get('execution_mode') == 'exploratory':
        return []  # Matching a metric name cannot satisfy a new evidence goal.
    old = previous.get('normalized_business_request') or previous.get('business_request') or {}
    if previous.get('status') != 'completed':
        return []
    result_ids = previous.get('result_ids') or []
    if previous.get('completion_role') == 'calculation_source':
        sources = [e for e in valid_evidence(task) if e['id'] in result_ids]
        if not sources or any((e.get('pending_calculation') or {}).get('status') != 'completed' for e in sources):
            return []
        result_ids = [e['pending_calculation']['result_id'] for e in sources]
    from .query_scope import effective_conditions
    if canonical_conditions(effective_conditions(old, task.state), canonical) != canonical_conditions(effective_conditions(query, task.state), canonical) or old.get('comparison_purpose', '') != query.get('comparison_purpose', ''):
        return []
    terms = lambda q, k: {canonical(x) for x in q.get(k) or []}
    if terms(old, 'dimensions') != terms(query, 'dimensions'):
        return []
    wanted = terms(query, 'metric_requirements')
    if not wanted:
        return []
    old_inputs = set(old.get('input_result_ids') or [])
    new_inputs = set(query.get('input_result_ids') or [])
    if not old_inputs <= new_inputs or not new_inputs <= old_inputs | set(previous.get('result_ids') or []) | set(result_ids):
        return []
    evidence = [e for e in valid_evidence(task) if e['id'] in result_ids]
    if not evidence or not all(e.get('complete') for e in evidence):
        return []
    if any(e.get('result_kind') == 'calculated_result' and not e.get('calculation_complete') for e in evidence):
        return []
    bound, columns = set(), set()
    for e in evidence:
        columns.update(canonical(c) for c in e.get('columns') or [])
        metadata = e.get('column_metadata') or {}
        for column, card in metadata.items():
            if column in (e.get('columns') or []) and card.get('kind') in {'registered', 'task_local'}:
                if card.get('kind') == 'registered':
                    from .measurement_context import measurement_card
                    identity = card.get('measurement_id') or card.get('metric_id')
                    current = task.metric_pool.get(identity)
                    if current and card.get('measurement_version') and card['measurement_version'] != measurement_card(current, task.state.get('schema_graph') or {}).get('version'):
                        continue
                bound.add(canonical(card.get('measurement_id') or card.get('metric_id')))
    if not wanted <= bound:
        return []
    if not terms(query, 'required_output_fields') <= (columns | bound):
        return []
    return [e['id'] for e in evidence]


def compact_measurement_memory(memory, expanded_registered_ids):
    """Keep one authoritative definition, retain per-result binding/version cautions."""
    result=deepcopy(memory)
    keep={'kind','metric_id','measurement_id','measurement_version','evidence_ids','source_unit','unit',
          'requires_review','display_unit_check','semantic_status','binding_basis'}
    for index,card in enumerate(result['definitions']):
        if card.get('kind')=='registered' and (card.get('metric_id') or card.get('measurement_id')) in expanded_registered_ids:
            result['definitions'][index]={k:v for k,v in card.items() if k in keep}
            result['definitions'][index]['definition_location']='registered_metrics'
    return result
