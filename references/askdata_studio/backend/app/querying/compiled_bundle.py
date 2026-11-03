"""One bounded monthly display of two existing, independently defined recipes."""
from copy import deepcopy
import hashlib
import json

from .compiled_metrics import METRICS, RecipeNotCovered, compile_paid_goods
from .compiled_sessions import check_session_population, compile_sessions
from .query_scope import normalize_conditions

RECIPE_ID = 'homenova.paid_and_sessions.monthly.v1'
SESSION_METRICS = {'sessions_count', 'checkout_session_cvr'}


def compile_bundle(request, context):
    from ..homenova import registered_metric
    database = context.get('database') or context.get('datasource_id')
    if database != 'homenova_20260831' or request.get('execution_mode') != 'registered_metrics':
        raise RecipeNotCovered('No approved registered monthly bundle for this request')
    names = request.get('metric_requirements') or []
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise RecipeNotCovered('The bundle requires explicit registered metric names')
    ids = set()
    for name in names:
        metric = registered_metric(database, name)
        ids.add(metric['id'] if metric else name)
    try:
        conditions = normalize_conditions(request.get('conditions') or {})
    except ValueError as exc:
        raise RecipeNotCovered(str(exc)) from exc
    dimensions = request.get('dimensions') or []
    # Preserve the dedicated method decision even when other families occur
    # before session metrics, rather than falling through to model SQL.
    check_session_population(ids, dimensions, conditions)
    paid_ids, session_ids = ids & METRICS.keys(), ids & SESSION_METRICS
    if not paid_ids or not session_ids or ids - (METRICS.keys() | SESSION_METRICS):
        raise RecipeNotCovered('Only paid-goods and store-session metric families can be combined')
    if dimensions != ['month'] or set(conditions) != {'months'}:
        raise RecipeNotCovered('The bundle covers only explicit months at whole-store monthly grain')
    paid_columns = {'month', *METRICS}
    session_columns = {'month', 'sessions_count'}
    if 'checkout_session_cvr' in session_ids:
        session_columns.update({'checkout_paid_sessions', 'checkout_session_cvr', 'unmatured_sessions'})
    outputs = request.get('required_output_fields') or []
    if (not isinstance(outputs, list) or not all(isinstance(n, str) for n in outputs)
            or set(outputs) - (paid_columns | session_columns)):
        raise RecipeNotCovered('Requested outputs are not all covered by the two recipes')
    paid = compile_paid_goods({**request, 'conditions': deepcopy(conditions),
        'metric_requirements': sorted(paid_ids),
        'required_output_fields': [n for n in outputs if n in paid_columns]}, context)
    sessions = compile_sessions({**request, 'conditions': deepcopy(conditions),
        'metric_requirements': sorted(session_ids),
        'required_output_fields': [n for n in outputs if n in session_columns]}, context)
    if any(paid['recipe'][key] != sessions['recipe'][key] for key in
           ('datasource_id', 'snapshot_cutoff', 'months', 'dimensions', 'timezone')):
        raise RecipeNotCovered('Component snapshots, periods or grain do not align')
    # Each component is already unique by month. No facts or statistical
    # populations are joined; missing aggregate rows remain NULL, not zero.
    projections = ['COALESCE(p.month, s.month) AS month']
    projections += [f'p.{name} AS {name}' for name in paid['output_metadata']]
    projections += [f's.{name} AS {name}' for name in sessions['output_metadata']]
    sql = ('WITH paid_monthly AS (' + paid['sql'] + '), session_monthly AS (' + sessions['sql']
           + ') SELECT ' + ', '.join(projections)
           + ' FROM paid_monthly p FULL OUTER JOIN session_monthly s ON p.month = s.month ORDER BY month')
    components = [paid, sessions]
    declarations = [deepcopy(metric) for component in components for metric in component['query_contract']['metrics']]
    definition = {'recipe_id': RECIPE_ID, 'datasource_id': database,
        'snapshot_cutoff': paid['recipe']['snapshot_cutoff'], 'months': paid['recipe']['months'],
        'dimensions': ('month',), 'timezone': paid['recipe']['timezone'], 'requested_metrics': tuple(sorted(ids)),
        'alignment': 'local_calendar_month_label_only',
        'components': [{'recipe': deepcopy(c['recipe']), 'spec_fingerprint': c['spec_fingerprint']} for c in components]}
    return {'sql': sql, 'query_contract': {'metrics': declarations,
                'time_ranges': [deepcopy(c['query_contract']['time_range']) for c in components]},
        'recipe': definition, 'registered_metric_ids': [m['name'] for m in declarations],
        'local_measurements': [deepcopy(m) for c in components for m in c.get('local_measurements', [])],
        'output_metadata': {k: deepcopy(v) for c in components for k, v in c['output_metadata'].items()},
        'spec_fingerprint': hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        'execution_source': 'registered_recipe',
        'interpretation_limits': list(dict.fromkeys([limit for c in components for limit in c.get('interpretation_limits', [])]))
            + ['两组仅按经营月标签并列；支付月金额与会话开始月转化仍属不同统计集合。']}
