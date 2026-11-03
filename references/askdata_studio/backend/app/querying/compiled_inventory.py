"""A bounded input binding for the existing inventory state-duration operator.

The recipe reads complete authorized event history; it never implements time
windows in SQL or turns exposure duration into demand loss. Public definitions
are pinned independently of saved reports and evaluation material.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json

from .compiled_metrics import RecipeNotCovered, SEMANTIC_KEYS, literal, month_window

RECIPE_ID = 'homenova.inventory.duration.monthly.v1'
APPROVED_SEMANTIC_VERSIONS = {
    'stockout_duration': '941dec87457c8a1d3f4990b01e79bddfba19184d8eb5f4fc0dea3b6ec20b93c6',
    'available_inventory': '235d1e5dfe6c6bce396ac6174bceb8c381ff7f68c47b040cfdbd403c380bef73',
}
FIELDS = ('sku_id', 'warehouse_id', 'occurred_at', 'event_seq',
          'movement_type', 'on_hand_delta', 'reserved_delta')
SKU_DIMENSIONS = {'sku_catalog.sku_id', 'inventory_movements.sku_id', 'order_items.sku_id'}
CATEGORY = 'sku_catalog.category_code'
DIMENSIONS = {'month', CATEGORY, 'inventory_movements.warehouse_id', *SKU_DIMENSIONS}
DIMENSION_ALIASES = {'sku_id': 'inventory_movements.sku_id', 'sku': 'inventory_movements.sku_id',
                     'category_code': CATEGORY, 'category': CATEGORY,
                     'warehouse_id': 'inventory_movements.warehouse_id'}
FILTERS = {'sku_catalog.category_code': 's.category_code',
           'sku_catalog.sku_id': 'im.sku_id', 'inventory_movements.sku_id': 'im.sku_id',
           'order_items.sku_id': 'im.sku_id', 'inventory_movements.warehouse_id': 'im.warehouse_id'}
CALCULATED_COLUMNS = {'period', 'sku_id', 'warehouse_id', 'opening_state', 'ending_state',
                      'period_start', 'period_end', 'duration_hours', 'duration_seconds', 'period_hours',
                      'time_fraction', 'episode_count', 'threshold'}


@dataclass(frozen=True)
class InventoryDurationSpec:
    datasource_id: str
    snapshot_cutoff: str
    months: tuple[str, ...]
    dimensions: tuple[str, ...]
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    requested_metrics: tuple[str, ...]
    semantic_versions: tuple[tuple[str, str], ...]
    recipe_id: str = RECIPE_ID
    timezone: str = 'Europe/Berlin'
    result_grain: str = 'sku_id × warehouse_id × period'
    partition_by: tuple[str, ...] = ('sku_id', 'warehouse_id')
    attribute_bindings: tuple[tuple[str, str], ...] = ()


def inventory_duration_requested(request, database):
    """Select the capability from metric identity, never from prose keywords."""
    from ..homenova import registered_metric
    for name in request.get('metric_requirements') or []:
        if not isinstance(name, str):
            continue
        metric = registered_metric(database, name)
        if (metric['id'] if metric else name) == 'stockout_duration':
            return True
    return False


def compile_inventory_duration(request, context):
    """Bind a pure stockout request to raw history plus typed postprocessing.

    Unsupported mixtures remain explicit coverage failures. In particular,
    conversion and inventory cannot be broadcast into the same result grain.
    """
    from ..homenova import registered_metric
    database = context.get('database') or context.get('datasource_id')
    if database != 'homenova_20260831':
        raise RecipeNotCovered('No approved inventory recipe for this datasource')
    for identity, approved in APPROVED_SEMANTIC_VERSIONS.items():
        metric = registered_metric(database, identity)
        actual = (hashlib.sha256(json.dumps({k: metric[k] for k in SEMANTIC_KEYS if k in metric},
                  ensure_ascii=False, sort_keys=True).encode()).hexdigest() if metric else None)
        if actual != approved:
            raise RecipeNotCovered('Registered inventory definition changed; recipe requires revalidation: ' + identity)
    names = request.get('metric_requirements') or []
    if not names or not all(isinstance(n, str) for n in names):
        raise RecipeNotCovered('An explicit inventory metric set is required')
    ids = []
    for name in names:
        metric = registered_metric(database, name)
        identity = metric['id'] if metric else name
        if identity not in APPROVED_SEMANTIC_VERSIONS:
            raise RecipeNotCovered('Split inventory state duration from other evidence populations: ' + name)
        ids.append(identity)
    if 'stockout_duration' not in ids:
        raise RecipeNotCovered('Inventory balance alone requires an explicit observation instant')
    dimensions = request.get('dimensions') or []
    if not isinstance(dimensions, list) or not all(isinstance(d, str) for d in dimensions):
        raise RecipeNotCovered('Inventory dimensions must be an explicit string list')
    dimensions = [DIMENSION_ALIASES.get(d, d) for d in dimensions]
    unknown = set(dimensions) - DIMENSIONS
    if unknown:
        raise RecipeNotCovered('Unsupported inventory dimensions: ' + ', '.join(sorted(unknown))
            + '; allowed: ' + ', '.join(sorted(DIMENSIONS))
            + '; aliases: ' + ', '.join(sorted(DIMENSION_ALIASES)))
    if len(dimensions) != len(set(dimensions)):
        raise RecipeNotCovered('Inventory dimensions contain duplicate fields or aliases')
    if 'month' not in dimensions or len(set(dimensions) & SKU_DIMENSIONS) != 1:
        raise RecipeNotCovered('Inventory duration requires month and exactly one SKU dimension; '
            'category is only an optional SKU label, not aggregated category duration; warehouse partitions remain separate')
    with_category = CATEGORY in dimensions
    outputs = request.get('required_output_fields') or []
    allowed_outputs = CALCULATED_COLUMNS | (DIMENSIONS - {CATEGORY}) | set(APPROVED_SEMANTIC_VERSIONS)
    if with_category:
        allowed_outputs |= {CATEGORY, 'category_code'}
    if any(DIMENSION_ALIASES.get(name, name) not in allowed_outputs for name in outputs):
        raise RecipeNotCovered('Extra requested output is outside the inventory recipe')
    from .query_scope import normalize_conditions
    try:
        conditions = normalize_conditions(request.get('conditions') or {})
    except ValueError as exc:
        raise RecipeNotCovered(str(exc)) from exc
    if not isinstance(conditions, dict) or set(conditions) - {'months', *FILTERS}:
        raise RecipeNotCovered('Unsupported inventory condition; preserve it for investigation')
    months = conditions.get('months', context.get('requested_time_expressions'))
    if (not isinstance(months, list) or not months or len(months) > 24
            or not all(isinstance(m, str) for m in months)):
        raise RecipeNotCovered('Inventory duration requires at most 24 explicit calendar months')
    months = tuple(sorted(set(months)))
    cutoff = (context.get('scenario') or {}).get('observation_cutoff_at')
    try:
        cutoff_dt = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        raise RecipeNotCovered('Missing trusted snapshot cutoff') from None
    periods = []
    for month in months:
        start, end = month_window(month)
        if cutoff_dt.tzinfo is None or end > cutoff_dt:
            raise RecipeNotCovered('Requested inventory period exceeds the authorized snapshot')
        periods.append({'label': month, 'start': start.isoformat(), 'end': end.isoformat()})
    filters = []
    for field, values in conditions.items():
        if field == 'months':
            continue
        values = values if isinstance(values, list) else [values]
        if not values or len(values) > 100 or not all(isinstance(v, str) and v for v in values):
            raise RecipeNotCovered('Invalid inventory partition selection')
        filters.append((field, tuple(sorted(set(values)))))
    scope = ((context.get('task') or {}).get('task_understanding') or {}).get('scope') or {}
    if scope.get('kind') not in (None, '', 'all') and not filters:
        raise RecipeNotCovered('A scoped task requires explicit normalized inventory conditions')
    partitions = (['category_code'] if with_category else []) + ['sku_id', 'warehouse_id']
    spec = InventoryDurationSpec(database, cutoff, months, tuple(dimensions), tuple(sorted(filters)),
        tuple(sorted(set(ids))), tuple(sorted(APPROVED_SEMANTIC_VERSIONS.items())),
        partition_by=tuple(partitions), attribute_bindings=(('category_code', CATEGORY),) if with_category else ())
    needs_catalog = with_category or any(k == CATEGORY for k, _ in filters)
    projections = ['im.' + field for field in FIELDS] + (['s.category_code AS category_code'] if with_category else [])
    sql = ('SELECT ' + ', '.join(projections) + ' FROM inventory_movements im'
           + (' JOIN sku_catalog s ON im.sku_id = s.sku_id' if needs_catalog else ''))
    if filters:
        sql += ' WHERE ' + ' AND '.join(FILTERS[field] + ' IN (' + ', '.join(literal(v) for v in values) + ')'
                                        for field, values in filters)
    # Both signs and opening rows remain in the source. The existing calculator
    # owns stable ordering and interval intersections for each requested month.
    plan = {'operation': 'state_duration', 'result_id': 'query_result',
            'partition_by': partitions, 'time_field': 'occurred_at',
            'order_field': 'event_seq', 'opening_field': 'movement_type', 'opening_value': 'opening',
            'delta_terms': [{'column': 'on_hand_delta', 'coefficient': 1},
                            {'column': 'reserved_delta', 'coefficient': -1}],
            'periods': periods, 'timezone_name': 'Europe/Berlin', 'threshold': 0.0}
    definition = asdict(spec)
    return {'sql': sql, 'postprocess': plan,
            'query_contract': {'metrics': [
                {'name': 'inventory_movements.' + name, 'output_columns': [name],
                 'source_fields': ['inventory_movements.' + name], 'aggregation': 'none'}
                for name in ('on_hand_delta', 'reserved_delta')]},
            'recipe': definition, 'registered_metric_ids': list(APPROVED_SEMANTIC_VERSIONS),
            'required_fields': ['inventory_movements.' + field for field in FIELDS]
                + (['sku_catalog.sku_id', CATEGORY] if needs_catalog else []),
            'spec_fingerprint': hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest(),
            'execution_source': 'registered_recipe',
            'interpretation_limits': [
                '月度缺货时长描述SKU×仓库的供给状态，不证明缺货时段与实际购买阻断重合。',
                '时长不是损失订单或金额；跨SKU或仓库的对象小时不能当日历时长。'],
            'output_metadata': {
                **({'category_code': {'kind': 'dimension', 'source_fields': [CATEGORY],
                    'constant_within': ['sku_id', 'warehouse_id'],
                    'definition': '当前授权快照中SKU的固定品类标签；时长仍逐SKU、仓库计算，不是品类合计。'}}
                   if with_category else {}),
                'duration_hours': {'metric_id': 'stockout_duration', 'unit': 'hours', 'recipe_id': RECIPE_ID,
                                   'additive_across_entities': False},
                'opening_state': {'metric_id': 'available_inventory', 'unit': 'items',
                                  'instant': 'period_start', 'additive_across_periods': False},
                'ending_state': {'metric_id': 'available_inventory', 'unit': 'items',
                                 'instant': 'immediately_before_period_end', 'additive_across_periods': False}}}
