"""Business-facing projections of immutable evidence; no model calls or inferred facts."""
from copy import deepcopy

INACTIVE = {'invalid', 'invalidated', 'superseded', 'removed', 'retracted'}


def current_progress(progress):
    result = deepcopy(progress)
    for category in ('facts', 'hypotheses', 'explanations'):
        result[category] = [x for x in result.get(category, []) if x.get('status') not in INACTIVE]
    return result


def evidence_brief(entry, *, rows=False):
    """Keep scope and uncertainty; a preview is never promoted to a summary."""
    keys = ('id', 'goal', 'version', 'columns', 'complete', 'total_rows', 'stored_rows',
            'check_status', 'question_ids', 'result_kind', 'calculation_complete',
            'interpretation', 'interpretation_limits', 'pending_calculation', 'input_result_ids')
    brief = {k: deepcopy(entry[k]) for k in keys if k in entry}
    contract = entry.get('query_contract') or {}
    brief['scope'] = {k: deepcopy(contract[k]) for k in
        ('grain', 'dimensions', 'filters', 'time_range', 'time_ranges', 'assumptions', 'open_questions') if contract.get(k)}
    validation = contract.get('validation') or {}
    brief['pending_checks'] = deepcopy(validation.get('pending_checks') or [])
    brief['column_meanings'] = {column: {k: deepcopy(v) for k, v in meaning.items() if k in
        {'kind', 'metric_id', 'measurement_id', 'measurement_version', 'definition', 'unit',
         'source_unit', 'statistical_entity', 'requires_review', 'display_unit_check', 'semantic_status'}}
        for column, meaning in (entry.get('column_metadata') or {}).items() if isinstance(meaning, dict)}
    if rows and entry.get('result_kind') != 'calculation_source':
        brief.update(rows=deepcopy(entry.get('rows') or []),
                     provided_rows=len(entry.get('rows') or []), display_truncated=entry.get('display_truncated', False))
    else:
        brief['data_location'] = 'read_result'
    brief['verification_boundary'] = '执行及基础校验不认证业务口径或因果；缺口和正常结果均须保留。'
    return brief


def investigation_checks(task):
    """A complete check directory, including normal/failed/unknown outcomes."""
    active = {e['id'] for e in task.evidence if e.get('check_status') not in INACTIVE}
    return [{k: deepcopy(v) for k, v in {
        'id': a['id'], 'goal': a.get('goal'), 'status': a.get('status'),
        'result_ids': [rid for rid in a.get('result_ids', []) if rid in active],
        'error': a.get('error'), 'question_ids': a.get('question_ids'),
    }.items() if v is not None} for a in task.actions if a.get('action_type') in {'query', 'calculate'}]
