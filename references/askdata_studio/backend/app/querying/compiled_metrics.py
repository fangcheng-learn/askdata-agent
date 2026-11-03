"""Versioned executable metric recipe with a deliberately bounded capability set.

No natural-language date guessing, generated SQL fragments, or example answers.
A refusal preserves the original request for the ordinary SQL module.
"""
from dataclasses import dataclass, asdict
from datetime import datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo
from sqlglot import exp

RECIPE_ID = 'homenova.paid_goods.monthly.v1'
METRICS = {
    'paid_goods_gmv_gross': ('SUM(i.line_paid_gross_cents)', 'sum', ['order_items.line_paid_gross_cents'], 'EUR cents'),
    'paid_order_count': ('COUNT(DISTINCT p.order_id)', 'count_distinct', ['payments.order_id'], 'orders'),
    'paid_goods_quantity': ('SUM(i.quantity)', 'sum', ['order_items.quantity'], 'items'),
    'goods_aov_gross': ('SUM(i.line_paid_gross_cents) / NULLIF(COUNT(DISTINCT p.order_id), 0)', 'derived', ['order_items.line_paid_gross_cents', 'payments.order_id'], 'EUR cents/order'),
    'units_per_paid_order': ('SUM(i.quantity) / NULLIF(COUNT(DISTINCT p.order_id), 0)', 'derived', ['order_items.quantity', 'payments.order_id'], 'items/order'),
    'paid_unit_price_gross': ('SUM(i.line_paid_gross_cents) / NULLIF(SUM(i.quantity), 0)', 'derived', ['order_items.line_paid_gross_cents', 'order_items.quantity'], 'EUR cents/item'),
}
SEMANTIC_KEYS = ('id', 'definition', 'formula', 'time_fields', 'required_predicates', 'grain', 'unit', 'tax_policy', 'shipping_policy', 'refund_policy', 'maturity', 'duplicate_risks', 'component_policies')
# Pinned during development, not learned or refreshed automatically at runtime.
APPROVED_SEMANTIC_VERSIONS = {'paid_goods_gmv_gross': 'f02a8044e48108248a0ec224dba1954ea2c5321a348302e641a86321d4a2fa90', 'paid_order_count': 'e5f5d9fdd7d46904f6d4e8493ce2166306f75d985d925a516e8e05df3e92811c', 'goods_aov_gross': '49a4c39bc2f9c7b251716e9be0bca53fbe35ab4842ba3c50512a7745be4d1550', 'paid_goods_quantity': 'b5e0312c0cd9cee637c5ef330950f6c5bdd28d61cbfebef6dffe2fee4ca7ae8f', 'units_per_paid_order': '16565d3756c28b07e0dbf72fab7a2030fb23dab818e018fc44b7019be7578cf9', 'paid_unit_price_gross': 'eb6ad6d1e236837df13af73360d86a1485e02ecc918a02663c1fe209498f6790'}

DIMENSIONS = {'month': "strftime(timezone('Europe/Berlin', p.captured_at), '%Y-%m')",
              'sku_catalog.category_code': 's.category_code', 'order_items.sku_id': 'i.sku_id',
              'sku_catalog.sku_id': 'i.sku_id'}
FILTERS = {'sku_catalog.category_code': 's.category_code', 'order_items.sku_id': 'i.sku_id',
           'sku_catalog.sku_id': 'i.sku_id'}


class RecipeNotCovered(ValueError):
    pass


def literal(value):
    # SQLGlot constructs a literal node; values never become SQL expressions.
    return exp.Literal.string(value).sql(dialect='duckdb')


def month_window(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}', value):
        raise RecipeNotCovered('The recipe requires explicit ISO calendar months')
    try:
        year, month = map(int, value.split('-'))
        start = datetime(year, month, 1, tzinfo=ZoneInfo('Europe/Berlin'))
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=start.tzinfo)
    except ValueError as exc:
        raise RecipeNotCovered('Invalid calendar month') from exc
    return start, end


@dataclass(frozen=True)
class PaidGoodsSpec:
    datasource_id: str
    snapshot_cutoff: str
    months: tuple[str, ...]
    dimensions: tuple[str, ...]
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    requested_metrics: tuple[str, ...]
    recipe_id: str = RECIPE_ID
    timezone: str = 'Europe/Berlin'

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def resolve_spec(request, context):
    from ..homenova import is_homenova, registered_metric
    database = context.get('database') or context.get('datasource_id')
    if database != 'homenova_20260831':
        raise RecipeNotCovered('No approved recipe for this datasource')
    for identity, approved in APPROVED_SEMANTIC_VERSIONS.items():
        metric = registered_metric(database, identity)
        actual = (hashlib.sha256(json.dumps({k: metric[k] for k in SEMANTIC_KEYS if k in metric},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest() if metric else None)
        if actual != approved:
            raise RecipeNotCovered('Registered definition changed; recipe requires revalidation: ' + identity)
    names = request.get('metric_requirements') or []
    if not names or not all(isinstance(n, str) for n in names):
        raise RecipeNotCovered('An explicit metric set is required')
    ids = []
    for name in names:
        metric = registered_metric(database, name)
        identity = metric['id'] if metric else name
        if identity not in METRICS:
            raise RecipeNotCovered('Unsupported metric: ' + name)
        ids.append(identity)
    dimensions = request.get('dimensions') or []
    if not isinstance(dimensions, list) or not all(isinstance(d, str) for d in dimensions):
        raise RecipeNotCovered('Invalid grouping')
    if len(dimensions) != len(set(dimensions)) or any(d not in DIMENSIONS for d in dimensions):
        raise RecipeNotCovered('Unsupported grouping')
    # First release covers explicit monthly outputs, optionally by one entity.
    if 'month' not in dimensions or len(dimensions) > 2:
        raise RecipeNotCovered('Only monthly and monthly/entity grouping is verified')
    # Output aliases are exactly those emitted for the selected dimensions.
    # A filter or another supported physical field does not add a grouping.
    allowed_outputs = set(METRICS) | set(dimensions) | {d.rsplit('.', 1)[-1] for d in dimensions}
    if any(name not in allowed_outputs for name in request.get('required_output_fields') or []):
        raise RecipeNotCovered('Extra requested output is outside this recipe')
    from .query_scope import normalize_conditions
    try:
        conditions = normalize_conditions(request.get('conditions') or {})
    except ValueError as exc:
        raise RecipeNotCovered(str(exc)) from exc
    if not isinstance(conditions, dict) or set(conditions) - {'months', *FILTERS}:
        raise RecipeNotCovered('Unsupported condition; preserve it for exploration')
    months = conditions.get('months', context.get('requested_time_expressions'))
    if not isinstance(months, list) or not months:
        raise RecipeNotCovered('Missing explicit structured periods')
    if len(months) > 120:
        raise RecipeNotCovered('Too many requested periods')
    months = tuple(sorted(set(months))) if all(isinstance(m, str) for m in months) else ()
    if not months:
        raise RecipeNotCovered('Invalid periods')
    cutoff = (context.get('scenario') or {}).get('observation_cutoff_at')
    if not cutoff:
        raise RecipeNotCovered('Missing trusted snapshot cutoff')
    cutoff_dt = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
    for month in months:
        _, end = month_window(month)
        if cutoff_dt.tzinfo is None or end > cutoff_dt:
            raise RecipeNotCovered('Requested period exceeds the authorized snapshot')
    filters = []
    for field, values in conditions.items():
        if field == 'months':
            continue
        if isinstance(values, dict):
            # Historical entity requests may use the explicit equality/IN
            # shape retained by entity resolution. Only these exact meanings
            # are equivalent to the recipe's membership predicate.
            operator = str(values.get('operator', '=')).strip().lower()
            value_keys = set(values) & {'value', 'values'}
            if (set(values) - {'operator', 'value', 'values'} or len(value_keys) != 1
                    or operator not in {'=', 'eq', 'in'}):
                raise RecipeNotCovered('Unsupported entity predicate; preserve it for exploration')
            values = values[next(iter(value_keys))]
            if operator != 'in' and isinstance(values, list) and len(values) != 1:
                raise RecipeNotCovered('Equality requires one entity value')
        values = values if isinstance(values, list) else [values]
        if not values or len(values) > 100 or not all(isinstance(v, str) and v for v in values):
            raise RecipeNotCovered('Invalid entity selection')
        filters.append((field, tuple(sorted(set(values)))))
    scope = ((context.get('task') or {}).get('task_understanding') or {}).get('scope') or {}
    if scope.get('kind') not in (None, '', 'all') and not filters:
        raise RecipeNotCovered('A scoped task requires explicit normalized entity conditions')
    return PaidGoodsSpec(database, cutoff, months, tuple(dimensions), tuple(sorted(filters)), tuple(sorted(set(ids))))


def compile_paid_goods(request, context):
    spec = resolve_spec(request, context)
    projections = [f'{DIMENSIONS[d]} AS {"month" if d == "month" else d.rsplit(".", 1)[-1]}' for d in spec.dimensions]
    # A closed, declared metric pack includes the ingredients of its ratios.
    # This lets later steps compute/reuse them without another database query.
    projections += [f'{parts[0]} AS {name}' for name, parts in METRICS.items()]
    windows = []
    for month in spec.months:
        start, end = month_window(month)
        if windows and windows[-1][1] == start:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    def interval(start, end):
        return f"(p.captured_at >= CAST({literal(start.isoformat())} AS TIMESTAMPTZ) AND p.captured_at < CAST({literal(end.isoformat())} AS TIMESTAMPTZ))"
    # A time_range describes the overall envelope. Disjoint requested months
    # remain explicit predicates and are retained in the immutable recipe spec.
    # Adjacent periods merge; they are not falsely declared simultaneous ranges.
    predicates = [interval(windows[0][0], windows[-1][1])]
    if len(windows) > 1:
        predicates.append('(' + ' OR '.join(interval(*w) for w in windows) + ')')
    for field, values in spec.filters:
        predicates.append(FILTERS[field] + ' IN (' + ', '.join(literal(v) for v in values) + ')')
    needs_catalog = any(d == 'sku_catalog.category_code' for d in spec.dimensions) or any(k == 'sku_catalog.category_code' for k, _ in spec.filters)
    sql = ('SELECT ' + ', '.join(projections) +
        " FROM (SELECT DISTINCT order_id, captured_at FROM payments WHERE status = 'captured') p" +
        ' JOIN order_items i ON p.order_id = i.order_id' +
        (' JOIN sku_catalog s ON i.sku_id = s.sku_id' if needs_catalog else '') +
        ' WHERE ' + ' AND '.join(predicates) +
        ' GROUP BY ' + ', '.join(str(i+1) for i in range(len(spec.dimensions))) +
        ' ORDER BY ' + ', '.join(str(i+1) for i in range(len(spec.dimensions))))
    declarations = [{'name': name, 'output_columns': [name], 'source_fields': values[2], 'aggregation': values[1]}
                    for name, values in METRICS.items()]
    # The compiler already owns exact event-time boundaries. Preserve them in
    # the contract instead of relying on heuristic extraction through OR/CAST.
    time_range = {'field': 'payments.captured_at', 'start': windows[0][0].isoformat(),
                  'end': windows[-1][1].isoformat(), 'end_inclusive': False}
    return {'sql': sql, 'query_contract': {'metrics': declarations, 'time_range': time_range}, 'recipe': asdict(spec),
            'spec_fingerprint': spec.fingerprint(), 'execution_source': 'registered_recipe',
            'registered_metric_ids': list(METRICS),
            'interpretation_limits': [
                '金额、客单价和件均价输出以欧分计；显示欧元须除以100。',
                '成交均价不是标价或折扣分布；金额、件数变化及分组贡献不能单独证明业务成因。'],
            'output_metadata': {name: {'unit': values[3], 'event_time': 'payments.captured_at',
                'recipe_id': RECIPE_ID, 'additive_across_entities': name in {'paid_goods_gmv_gross', 'paid_goods_quantity'}}
                for name, values in METRICS.items()}}
