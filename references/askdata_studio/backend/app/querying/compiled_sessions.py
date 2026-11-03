"""Three explicit session populations, with independently selected denominators.

Only registered metric requests enter this bounded recipe. Browsing, landing
and checkout populations are not interchangeable, even when their rows happen
to match in one snapshot.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json

from .compiled_metrics import RecipeNotCovered, SEMANTIC_KEYS, literal, month_window
from .query_scope import normalize_conditions

RECIPE_ID = 'homenova.sessions.monthly.v1'
APPROVED_SEMANTIC_VERSIONS = {
    'sessions_count': '02fe4d82c59e9da3ab229e26a731628df7aafe36b185861f2fe49a3694a81d21',
    'checkout_session_cvr': '2ac54ac3b8acb6b347c67b364cc499f87f7e1da0f02324cf359dc01cf0f80505',
    'sku_view_session_cvr': 'e71b45abd3c3ca38c367a0ee58c6b7198e33f74d2d4303886372175c266aa763',
    'category_view_session_cvr': 'ba49f872a78e1362c4a5c5188afd4f37a857fd617f51d5cea26bb27c3f0396ab',
}
SKU_DIMENSIONS = {'sku_catalog.sku_id', 'visit_events.sku_id', 'order_items.sku_id'}
CATEGORY = 'sku_catalog.category_code'
CHANNEL = 'sessions.source_channel'


class SessionPopulationMismatch(RecipeNotCovered):
    """A known metric's population conflicts with the requested entity scope."""

    def __init__(self, message, alternative_metric_ids=()):
        super().__init__(message)
        # Options for an explicit method decision, never replacement metrics.
        self.alternative_metric_ids = list(alternative_metric_ids)


def check_session_population(metric_ids, dimensions, conditions):
    """Reject explicit scope conflicts even inside a mixed metric request."""
    fields = set(conditions)
    if isinstance(dimensions, list) and all(isinstance(d, str) for d in dimensions):
        fields.update(dimensions)
    category_fields = {CATEGORY, 'category_code'}
    sku_fields = {*SKU_DIMENSIONS, 'sessions.landing_sku_id', 'sku_id', 'landing_sku_id'}
    if 'category_view_session_cvr' in metric_ids and fields & sku_fields:
        raise SessionPopulationMismatch(
            'category_view_session_cvr按品类浏览及同品类购买定义，不能按SKU改写成另一统计集合。'
            '若要比较各SKU，请显式选择sku_view_session_cvr；若仍分析品类，请保留品类粒度。'
            '浏览SKU后购买同品类任意商品属于另一自定义口径，不能沿用登记指标名。',
            ['sku_view_session_cvr'])
    if not set(metric_ids) & {'sessions_count', 'checkout_session_cvr'}:
        return
    conflicts = fields & (category_fields | sku_fields)
    if conflicts:
        alternatives = ['category_view_session_cvr'] if conflicts & category_fields else []
        if conflicts & sku_fields:
            alternatives.append('sku_view_session_cvr')
        raise SessionPopulationMismatch(
            'sessions_count/checkout_session_cvr的全店或渠道会话集合不能按商品/品类字段直接改义：'
            + ', '.join(sorted(conflicts))
            + '。请明确入口或浏览分母及购买对象；浏览集合可显式选择备选登记指标，'
              '自定义入口集合需exploratory及本地定义，不能沿用上述指标名。', alternatives)


@dataclass(frozen=True)
class SessionSpec:
    datasource_id: str
    snapshot_cutoff: str
    cohort: str
    months: tuple[str, ...]
    dimensions: tuple[str, ...]
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    requested_metrics: tuple[str, ...]
    semantic_versions: tuple[tuple[str, str], ...]
    recipe_id: str = RECIPE_ID
    timezone: str = 'Europe/Berlin'


def compile_sessions(request, context):
    from ..homenova import registered_metric
    database = context.get('database') or context.get('datasource_id')
    if database != 'homenova_20260831':
        raise RecipeNotCovered('No approved session recipe for this datasource')
    if request.get('execution_mode') != 'registered_metrics':
        raise RecipeNotCovered('Exploratory evidence goals are not replaced by session aggregates')
    names = request.get('metric_requirements') or []
    if not names or not all(isinstance(n, str) for n in names):
        raise RecipeNotCovered('An explicit session metric set is required')
    ids = set()
    for name in names:
        metric = registered_metric(database, name)
        identity = metric['id'] if metric else name
        ids.add(identity)
    dimensions = request.get('dimensions') or []
    try:
        conditions = normalize_conditions(request.get('conditions') or {})
    except ValueError as exc:
        raise RecipeNotCovered(str(exc)) from exc
    check_session_population(ids, dimensions, conditions)
    unsupported = ids - APPROVED_SEMANTIC_VERSIONS.keys()
    if unsupported:
        raise RecipeNotCovered('Unsupported session metric: ' + ', '.join(sorted(unsupported)))
    if ids <= {'sessions_count', 'checkout_session_cvr'}:
        cohort, ratio, denominator, numerator = 'valid_sessions', 'checkout_session_cvr', 'sessions_count', 'checkout_paid_sessions'
        supported_dims, supported_filters = {'month', CHANNEL}, {CHANNEL}
        metric_ids = ['sessions_count'] if ids == {'sessions_count'} else ['sessions_count', 'checkout_session_cvr']
    elif ids == {'sku_view_session_cvr'}:
        cohort, ratio, denominator, numerator = 'sku_view_sessions', 'sku_view_session_cvr', 'sku_view_sessions', 'sku_view_paid_sessions'
        supported_dims, supported_filters = {'month', *SKU_DIMENSIONS}, {*SKU_DIMENSIONS, CATEGORY}
        metric_ids = ['sku_view_session_cvr']
    elif ids == {'category_view_session_cvr'}:
        cohort, ratio, denominator, numerator = 'category_view_sessions', 'category_view_session_cvr', 'category_view_sessions', 'category_view_paid_sessions'
        supported_dims, supported_filters = {'month', CATEGORY}, {CATEGORY}
        metric_ids = ['category_view_session_cvr']
    else:
        raise RecipeNotCovered('Distinct session populations require separate query items')
    for identity in metric_ids:
        metric = registered_metric(database, identity)
        actual = (hashlib.sha256(json.dumps({k: metric[k] for k in SEMANTIC_KEYS if k in metric},
                  ensure_ascii=False, sort_keys=True).encode()).hexdigest() if metric else None)
        if actual != APPROVED_SEMANTIC_VERSIONS[identity]:
            raise RecipeNotCovered('Registered session definition changed; recipe requires revalidation: ' + identity)
    if (not isinstance(dimensions, list) or not all(isinstance(d, str) for d in dimensions)
            or len(dimensions) != len(set(dimensions)) or set(dimensions) - supported_dims
            or 'month' not in dimensions or len(dimensions) > 2):
        raise RecipeNotCovered('Unsupported session grouping')
    columns = {'month', denominator, numerator, ratio, 'unmatured_sessions'}
    if ids == {'sessions_count'}:
        columns = {'month', 'sessions_count'}
    if len(dimensions) == 2:
        columns.add('source_channel' if cohort == 'valid_sessions' else 'sku_id' if cohort == 'sku_view_sessions' else 'category_code')
    if any(name not in columns | supported_dims for name in request.get('required_output_fields') or []):
        raise RecipeNotCovered('Extra requested output is outside the session recipe')
    if set(conditions) - {'months', *supported_filters}:
        raise RecipeNotCovered('Unsupported filter or incompatible session population')
    months = conditions.get('months', context.get('requested_time_expressions'))
    if (not isinstance(months, list) or not months or len(months) > 120
            or not all(isinstance(m, str) for m in months)):
        raise RecipeNotCovered('Session cohorts require explicit calendar months')
    months = tuple(sorted(set(months)))
    cutoff = (context.get('scenario') or {}).get('observation_cutoff_at')
    try:
        cutoff_dt = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        raise RecipeNotCovered('Missing trusted snapshot cutoff') from None
    windows = []
    for month in months:
        start, end = month_window(month)
        if cutoff_dt.tzinfo is None or end > cutoff_dt:
            raise RecipeNotCovered('Requested session period exceeds the authorized snapshot')
        if windows and windows[-1][1] == start:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    filters = []
    for field, values in conditions.items():
        if field == 'months':
            continue
        values = values if isinstance(values, list) else [values]
        if not values or len(values) > 100 or not all(isinstance(v, str) and v for v in values):
            raise RecipeNotCovered('Invalid session entity selection')
        filters.append((field, tuple(sorted(set(values)))))
    if cohort != 'valid_sessions' and len(dimensions) != 2:
        entity_filters = [(field, values) for field, values in filters
                          if field in (SKU_DIMENSIONS if cohort == 'sku_view_sessions' else {CATEGORY})]
        if not entity_filters or any(len(values) != 1 for _, values in entity_filters):
            raise RecipeNotCovered('Browsing conversion requires its entity dimension or an explicit single-entity filter')
    scope = ((context.get('task') or {}).get('task_understanding') or {}).get('scope') or {}
    if scope.get('kind') not in (None, '', 'all') and not filters:
        raise RecipeNotCovered('A scoped task requires explicit normalized session conditions')
    spec = SessionSpec(database, cutoff, cohort, months, tuple(dimensions), tuple(sorted(filters)), tuple(sorted(ids)),
                       tuple((identity, APPROVED_SEMANTIC_VERSIONS[identity]) for identity in metric_ids))
    def boundary(value):
        return 'CAST(' + literal(value) + ' AS TIMESTAMPTZ)'
    def interval(start, end):
        return '(s.started_at >= ' + boundary(start.isoformat()) + ' AND s.started_at < ' + boundary(end.isoformat()) + ')'
    predicates = ['s.is_internal_traffic = false', interval(windows[0][0], windows[-1][1])]
    if len(windows) > 1:
        predicates.append('(' + ' OR '.join(interval(*w) for w in windows) + ')')
    source = 'sessions s'
    entity = ('source_channel' if cohort == 'valid_sessions' else 'sku_id' if cohort == 'sku_view_sessions' else 'category_code')
    entity_sql = ('s.source_channel' if cohort == 'valid_sessions' else 've.sku_id' if cohort == 'sku_view_sessions' else 'sc.category_code')
    if cohort != 'valid_sessions':
        source += ' JOIN visit_events ve ON s.session_id = ve.session_id'
        predicates.append("ve.event_type = 'product_view'")
        if cohort == 'category_view_sessions' or any(k == CATEGORY for k, _ in filters):
            source += ' JOIN sku_catalog sc ON ve.sku_id = sc.sku_id'
    filter_columns = {CHANNEL: 's.source_channel', CATEGORY: 'sc.category_code',
                      **{name: 've.sku_id' for name in SKU_DIMENSIONS}}
    for field, values in filters:
        predicates.append(filter_columns[field] + ' IN (' + ', '.join(literal(v) for v in values) + ')')
    # The denominator has no order/payment join. DISTINCT removes repeated
    # browsing events before the numerator is attached to the same population.
    base = ('SELECT DISTINCT s.session_id, s.started_at, s.ended_at, ' + entity_sql + ' AS ' + entity
            + ' FROM ' + source + ' WHERE ' + ' AND '.join(predicates))
    paid = ('SELECT DISTINCT o.checkout_session_id'
            + (', oi.sku_id' if cohort == 'sku_view_sessions' else ', pc.category_code' if cohort == 'category_view_sessions' else '')
            + " FROM orders o JOIN (SELECT DISTINCT order_id FROM payments WHERE status = 'captured') cap ON o.order_id = cap.order_id")
    if cohort != 'valid_sessions':
        paid += ' JOIN order_items oi ON o.order_id = oi.order_id'
    if cohort == 'category_view_sessions':
        paid += ' JOIN sku_catalog pc ON oi.sku_id = pc.sku_id'
    cutoff_sql = boundary(cutoff)
    # Keep the denominator intact. If a cohort has unfinished observation, the
    # conversion is unknown; expose that count instead of silently dropping it.
    immature = ('COUNT(DISTINCT CASE WHEN c.ended_at IS NULL OR c.ended_at >= ' + cutoff_sql
                + ' OR u.checkout_session_id IS NOT NULL THEN c.session_id END)')
    den, num = 'COUNT(DISTINCT c.session_id)', 'COUNT(DISTINCT p.checkout_session_id)'
    projections = ["strftime(timezone('Europe/Berlin', c.started_at), '%Y-%m') AS month"]
    if len(dimensions) == 2:
        projections.append('c.' + entity + ' AS ' + entity)
    group_projections = list(projections)
    projections += [den + ' AS ' + denominator, num + ' AS ' + numerator,
                    'CASE WHEN ' + immature + ' > 0 THEN NULL ELSE CAST(' + num + ' AS DOUBLE) / NULLIF(' + den + ', 0) END AS ' + ratio,
                    immature + ' AS unmatured_sessions']
    sql = ('WITH cohort AS (' + base + '), paid AS (' + paid + '), unobserved_orders AS ('
           + 'SELECT DISTINCT checkout_session_id FROM orders WHERE expires_at IS NULL OR expires_at >= ' + cutoff_sql
           + ') SELECT ' + ', '.join(projections) + ' FROM cohort c LEFT JOIN paid p ON c.session_id = p.checkout_session_id'
           + (' AND c.' + entity + ' = p.' + entity if cohort != 'valid_sessions' else '')
           + ' LEFT JOIN unobserved_orders u ON c.session_id = u.checkout_session_id'
           + ' GROUP BY ' + ', '.join(str(i + 1) for i in range(len(dimensions)))
           + ' ORDER BY ' + ', '.join(str(i + 1) for i in range(len(dimensions))))
    sources = ['sessions.session_id', 'sessions.started_at', 'orders.checkout_session_id', 'payments.status']
    if cohort != 'valid_sessions':
        sources += ['visit_events.session_id', 'visit_events.sku_id', 'visit_events.event_type', 'order_items.sku_id']
    if cohort == 'category_view_sessions':
        sources += ['sku_catalog.category_code']
    declarations = [
        {'name': denominator, 'output_columns': [denominator], 'source_fields': ['sessions.session_id'], 'aggregation': 'count_distinct'},
        {'name': numerator, 'output_columns': [numerator], 'source_fields': ['orders.checkout_session_id'], 'aggregation': 'count_distinct'},
        {'name': ratio, 'output_columns': [ratio], 'source_fields': sources, 'aggregation': 'derived'},
        {'name': 'unmatured_sessions', 'output_columns': ['unmatured_sessions'],
         'source_fields': ['sessions.session_id'], 'aggregation': 'count_distinct'}]
    definition = asdict(spec)
    cohort_description = {'valid_sessions': '所有有效会话', 'sku_view_sessions': '浏览同一SKU的有效会话',
                          'category_view_sessions': '浏览同一品类任一SKU的有效会话；同品类跨SKU去重'}[cohort]
    metadata = {name: {'unit': 'ratio' if name == ratio else 'sessions', 'cohort': cohort,
                       'event_time': 'sessions.started_at', 'recipe_id': RECIPE_ID,
                       'additive_across_entities': cohort == 'valid_sessions' and name != ratio}
                for name in (denominator, numerator, ratio, 'unmatured_sessions')}
    metadata[denominator]['meaning'] = cohort_description + '，不以是否下单筛分母'
    metadata[numerator]['meaning'] = '分母集合内同checkout_session成功capture的会话；商品范围与分母相同'
    metadata[ratio].update(numerator_column=numerator, denominator_column=denominator,
                           maturity_column='unmatured_sessions', meaning='unmatured_sessions>0时为NULL，不能当零转化')
    # Auxiliary counts are explicit local measurements. Declaring several
    # unknown metrics over session_id invites ambiguous physical-name fallback
    # and can erase a valid registered output binding during result reuse.
    local_measurements = []
    local_definitions = {denominator: metadata[denominator]['meaning'], numerator: metadata[numerator]['meaning'],
                         'unmatured_sessions': '同一分母会话集合中会话未结束、结束未在截点前或订单支付有效期尚未观察完成的不同会话数'}
    for name in (denominator, numerator, 'unmatured_sessions'):
        if name in metric_ids:
            continue
        local_measurements.append({'id': name, 'output_columns': [name], 'definition': local_definitions[name],
            'scope': '本次指定范围及sessions.started_at月份的' + cohort_description,
            'source_fields': ['orders.checkout_session_id'] if name == numerator else ['sessions.session_id'],
            'statistical_entity': 'sessions', 'event_time': ['sessions.started_at'], 'unit': 'sessions',
            'population': cohort, 'cohort': cohort, 'formula': 'COUNT(DISTINCT matched session_id)'})
    declarations = [entry for entry in declarations if entry['name'] in metric_ids]
    if ids == {'sessions_count'}:
        # Counting visits alone must not introduce order/payment dependencies
        # that its registered definition and retrieval did not request.
        sql = ('WITH cohort AS (' + base + ') SELECT ' + ', '.join(group_projections + [den + ' AS sessions_count'])
               + ' FROM cohort c GROUP BY ' + ', '.join(str(i + 1) for i in range(len(dimensions)))
               + ' ORDER BY ' + ', '.join(str(i + 1) for i in range(len(dimensions))))
        declarations = declarations[:1]
        metadata = {'sessions_count': metadata['sessions_count']}
        local_measurements = []
    return {'sql': sql, 'query_contract': {'metrics': declarations, 'time_range': {
                'field': 'sessions.started_at', 'start': windows[0][0].isoformat(),
                'end': windows[-1][1].isoformat(), 'end_inclusive': False}},
            'recipe': definition, 'registered_metric_ids': metric_ids,
            'local_measurements': local_measurements,
            'spec_fingerprint': hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest(),
            'interpretation_limits': [
                '统计同一sessions.started_at会话集合，不定位加购、结账或支付失败环节。',
                '购买会话不同于支付月订单；不得与按captured_at归期的GMV直接组成乘法恒等式。'],
            'execution_source': 'registered_recipe', 'output_metadata': metadata}
