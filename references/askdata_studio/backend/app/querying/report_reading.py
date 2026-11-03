"""Readable numeric inputs for report Agents; no narrative or database access.

Keep the source immutable. Reuse the existing calculator for within-result
period comparisons; never sum cohorts or infer units from field names.
"""
from decimal import Decimal, InvalidOperation
import re

from .calculations import execute_calculation, CalculationError


def display_number(value, unit=''):
    if value is None:
        return '未提供'
    if isinstance(value, bool):
        return str(value)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if not number.is_finite():
        return '未提供'
    if unit.startswith('EUR cents'):
        number /= 100
        unit = unit.replace('EUR cents', 'EUR', 1)
    elif unit == 'ratio':
        number *= 100
        unit = '%'
    text = format(number, ',.2f') if number != number.to_integral() or unit else format(number, ',f')
    return text + (' ' + unit if unit else '（原始值，单位未登记）')


def reading_view(sources):
    views = []
    for source in sources:
        numeric = [c for c in source.columns if c in source.units and any(
            isinstance(row.get(c), (int, float)) and not isinstance(row.get(c), bool) for row in source.rows)]
        view = {'result_id': source.result_id, 'source_version': source.version,
                'complete': source.complete, 'rows': [
                    {c: display_number(row.get(c), source.units[c]) if c in numeric else row.get(c)
                     for c in source.columns} for row in source.rows],
                'period_comparisons': [],
                'interpretation': '同一结果内逐组算术对比，不代表因果；不把分组比例平均为总体比例。'}
        periods = [c for c in source.columns if c in {'month', 'period'} and source.rows and all(
            re.fullmatch(r'\d{4}-\d{2}(?:-\d{2}(?:[ T].*)?)?', str(row.get(c, ''))) for row in source.rows)]
        if source.complete and len(periods) == 1 and numeric:
            period = periods[0]
            values = sorted({row[period] for row in source.rows})
            if len(values) == 2:
                groups = [c for c in source.columns if c != period and c not in numeric]
                try:
                    result = execute_calculation({'operation': 'period_change', 'result_id': source.result_id, 'period_field': period,
                        'base_period': values[0], 'current_period': values[1], 'group_by': groups,
                        'value_fields': numeric}, {'columns': source.columns, 'rows': source.rows,
                            'complete': True, 'total_rows': source.total_rows,
                            'column_metadata': {c: {'unit': u} for c, u in source.units.items()}})
                    for row in result['rows']:
                        unit = source.units.get(row['metric'], '')
                        view['period_comparisons'].append({**row,
                            **{key: display_number(row[key], unit) for key in ('base', 'current')},
                            'change': (display_number(row['change'], 'ratio').removesuffix(' %') + ' 个百分点'
                                       if unit == 'ratio' and row['change'] is not None else display_number(row['change'], unit)),
                            'change_rate': display_number(row['change_rate'], 'ratio')})
                except (CalculationError, ValueError, TypeError) as exc:
                    view['comparison_unavailable'] = str(exc)
        views.append(view)
    return views


def compact_reading_view(views):
    """Lossless table transport: column names once; every cell keeps its unit/value."""
    from copy import deepcopy
    compact = deepcopy(views)
    for view in compact:
        for key in ('rows', 'period_comparisons'):
            records = view.get(key) or []
            if not records:
                continue
            columns = list(records[0])
            # Only rectangular records can be encoded without changing absent keys.
            if all(set(row) == set(columns) for row in records):
                view[key] = {'columns': columns, 'rows': [[row[c] for c in columns] for row in records]}
    return compact
