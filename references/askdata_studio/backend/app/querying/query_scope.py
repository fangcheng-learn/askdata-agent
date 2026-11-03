"""Conservative normalization of inherited periods, without date guessing."""
from copy import deepcopy
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TIME_CONDITIONS = frozenset({'months', 'time_range', 'time_ranges', 'date_range',
    'period', 'periods', 'baseline', 'comparison_period', 'start', 'end', 'start_date', 'end_date'})


def normalize_conditions(raw, *, strict=False):
    """Adapt the closed model wire shape without changing legacy semantics.

    Typed filters become the existing field/value map. Unknown legacy keys
    remain visible to coverage checks; they are never discarded to make a
    recipe match. Strict mode is for new actions, not historical evidence.
    """
    if not isinstance(raw, dict):
        raise ValueError('conditions必须为对象；字段筛选使用filters数组')
    conditions = deepcopy(raw)
    if 'filters' in conditions:
        filters = conditions.pop('filters')
        if not isinstance(filters, list):
            raise ValueError('conditions.filters必须为数组，每项包含field、operator、values')
        for item in filters:
            if not isinstance(item, dict) or set(item) - {'field', 'operator', 'values'}:
                raise ValueError('条件只接受field、operator、values，不能包含SQL片段')
            field, operator, values = item.get('field'), item.get('operator', 'in'), item.get('values')
            if not isinstance(field, str) or not field.strip() or field in TIME_CONDITIONS or field == 'filters':
                raise ValueError('filters.field需为明确业务维度或字段；期间使用months或time_range')
            if not isinstance(values, list) or not values or any(type(v) not in (str, int, float, bool) for v in values):
                raise ValueError('filters.values需为非空标量数组；不能用null猜测筛选含义')
            if operator not in {'=', 'in', '!=', 'not in', '>', '>=', '<', '<='}:
                raise ValueError('筛选operator不受支持，不能改变筛选含义')
            if operator not in {'in', 'not in'} and len(values) != 1:
                raise ValueError('单值比较需恰好一个筛选值')
            if operator in {'=', 'in'}:
                value = values[0] if len(values) == 1 else values
            else:
                value = {'operator': operator, 'values': values} if operator == 'not in' else {
                    'operator': operator, 'value': values[0]}
            if field in conditions and conditions[field] != value:
                raise ValueError('同一字段存在冲突条件，不能静默覆盖：' + field)
            conditions[field] = value
    if strict:
        for field, value in conditions.items():
            if (not isinstance(field, str) or not field.strip()
                    or any(c in field for c in "{}[]:'\"\n\r")):
                raise ValueError('conditions含畸形字段键；期间使用months/time_range，字段与值分放filters.field/values')
            if value is None:
                raise ValueError('conditions含未定义的null条件；不能忽略筛选，请明确条件或省略未设置项')
    return conditions


def complete_registered_request(query, database):
    """Fill execution details from registered meaning, without changing populations.

    Business comparison and inventory partitioning are explicit here; a list of
    months alone is NOT permission to turn an overall aggregate into a trend.
    The original request remains in the controller log for audit/recovery.
    """
    from ..homenova import registered_metric
    from .compiled_metrics import METRICS
    from .compiled_inventory import SKU_DIMENSIONS, DIMENSION_ALIASES
    from .compiled_sessions import APPROVED_SEMANTIC_VERSIONS as SESSION_METRICS
    result = deepcopy(query)
    names = result.get('metric_requirements') or []
    ids = [(registered_metric(database, name) or {}).get('id', name) for name in names]
    if database != 'homenova_20260831' or not ids:
        return result
    supported = set(METRICS) | set(SESSION_METRICS) | {'stockout_duration', 'available_inventory'}
    if query.get('execution_mode') == 'exploratory' and (
            result.get('required_output_fields') or result.get('input_result_ids')):
        return result
    if set(ids) <= supported:
        result['metric_requirements'] = ids
        result['execution_mode'] = 'registered_metrics'
        dimensions = list(result.get('dimensions') or [])
        months = (result.get('conditions') or {}).get('months')
        inventory = 'stockout_duration' in ids and set(ids) <= {'stockout_duration', 'available_inventory'}
        if inventory:
            if not {DIMENSION_ALIASES.get(d, d) for d in dimensions} & SKU_DIMENSIONS:
                dimensions.append('inventory_movements.sku_id')
        if months and (inventory or
                       'month' in (result.get('required_output_fields') or [])) and 'month' not in dimensions:
            dimensions.insert(0, 'month')
        result['dimensions'] = dimensions
    return result


def partition_registered_request(query, database):
    """Split incompatible known populations, retaining every metric and filter.

    A session scope conflict stays a conflict in its child query; it no longer
    blocks the independent payment metrics. This does not choose a denominator.
    """
    from ..homenova import registered_metric
    from .compiled_metrics import METRICS
    from .compiled_sessions import APPROVED_SEMANTIC_VERSIONS, check_session_population, SessionPopulationMismatch
    query = complete_registered_request(query, database)
    if database != 'homenova_20260831' or query.get('execution_mode') == 'exploratory':
        return [query]
    groups = [set(METRICS), set(APPROVED_SEMANTIC_VERSIONS), {'stockout_duration', 'available_inventory'}]
    names = query.get('metric_requirements') or []
    ids = [(registered_metric(database, name) or {}).get('id', name) for name in names]
    selected = [[name for name in ids if name in group] for group in groups]
    if not ids or not set(ids) <= set.union(*groups) or sum(bool(g) for g in selected) < 2:
        return [query]
    conflict = bool(selected[2])
    try:
        check_session_population(ids, query.get('dimensions') or [], normalize_conditions(query.get('conditions') or {}))
    except SessionPopulationMismatch:
        conflict = True
    if not conflict:
        return [query]  # Existing compatible bundle remains the fast path.
    outputs = query.get('required_output_fields') or []
    # Unresolved output aliases belong to the original request, not a guessed child.
    dimension_outputs = set(query.get('dimensions') or []) | {d.rsplit('.', 1)[-1] for d in query.get('dimensions') or []}
    if set(outputs) - set(ids) - dimension_outputs:
        return [query]
    result = []
    for group in selected:
        if group:
            result.append({**deepcopy(query), 'metric_requirements': group,
                           'required_output_fields': [o for o in outputs if o in group or o in dimension_outputs]})
    return result


def calendar_months(period, timezone='Europe/Berlin'):
    """Only explicit, complete calendar-month intervals; never infer inclusion."""
    if not isinstance(period, dict) or set(period) - {'start', 'end', 'end_inclusive'} or not isinstance(period.get('end_inclusive'), bool):
        return None
    try:
        zone = ZoneInfo(timezone)
        start, end = [datetime.fromisoformat(period[k].replace('Z', '+00:00')) for k in ('start','end')]
        start = start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
        end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
        if period['end_inclusive']:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', period['end']):
                return None
            end += timedelta(days=1)
        if start >= end or any((v.day != 1 or any((v.hour,v.minute,v.second,v.microsecond))) for v in (start,end)):
            return None
        values=[]
        while start < end and len(values) < 120:
            values.append(start.strftime('%Y-%m'))
            start = start.replace(year=start.year+(start.month==12),month=start.month%12+1)
        return values if start == end else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def effective_conditions(query, state):
    conditions = normalize_conditions(query.get('conditions') or {})
    if 'time_range' in conditions and not (set(conditions) & (TIME_CONDITIONS - {'time_range'})):
        periods = calendar_months(conditions['time_range'], (state.get('scenario') or {}).get('timezone') or 'Europe/Berlin')
        if periods:
            conditions.pop('time_range')
            conditions['months'] = periods
    months = conditions.get('months')
    if isinstance(months, list) and all(isinstance(v, str) for v in months):
        conditions['months'] = sorted(set(months))
    if set(conditions) & TIME_CONDITIONS or query.get('comparison_purpose'):
        return conditions
    # Explicit physical event-time constraints outrank task defaults.
    for field in (state.get('schema_graph') or {}).get('fields') or []:
        names = {field.get('name'), field.get('sql_name'), field.get('id')}
        if names & conditions.keys() and any(t in str(field.get('data_type') or field.get('type')).upper()
                                             for t in ('DATE', 'TIME')):
            return conditions
    # A corrected period must first be resolved by preprocessing/resume; do
    # not substitute an obsolete initial extraction for a correction.
    confirmed = state.get('confirmed_parameters') or {}
    if set(confirmed) & TIME_CONDITIONS:
        return conditions
    values = (state.get('extraction') or {}).get('time_expressions') or []
    if not values:
        # The main investigation may have resolved an explicit year/month list
        # even when the lightweight entry extractor returned no fields. Reuse
        # that task context; never guess a year or expand a partial/date range.
        period = (state.get('task_understanding') or {}).get('period') or ''
        match = re.fullmatch(r'\s*(\d{4})年\s*((?:0?[1-9]|1[0-2])月(?:\s*[和与及、,，]\s*(?:0?[1-9]|1[0-2])月)*)\s*', period)
        if match and 1 <= int(match[1]) <= 9999:
            values = [f'{match[1]}-{int(m):02d}' for m in re.findall(r'(\d+)月', match[2])]
    if values and all(isinstance(v, str) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', v) for v in values):
        conditions['months'] = sorted(set(values))
    return conditions
