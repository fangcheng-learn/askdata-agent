"""One-to-one full alignment of two authorized result snapshots, without SQL."""
from decimal import Decimal
import math

from .calculations import CalculationError, _period_key, _source_metadata


def _index(data, keys, selected):
    if not isinstance(data, dict) or data.get('complete') is not True:
        raise CalculationError('整合需要两份完整结果', code='incomplete_result')
    columns, rows = data.get('columns'), data.get('rows')
    if (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
            or len(set(columns)) != len(columns) or not isinstance(rows, list)
            or not all(isinstance(row, dict) for row in rows)):
        raise CalculationError('整合输入必须包含唯一列名和结构化行', code='invalid_result')
    if isinstance(data.get('total_rows'), int) and data['total_rows'] > len(rows):
        raise CalculationError('不能用截断结果进行整合', code='incomplete_result')
    needed = list(dict.fromkeys([*keys, *selected]))
    missing = [c for c in needed if c not in columns]
    if missing:
        raise CalculationError('整合引用了不存在的列', code='unknown_columns', details={'missing_columns': missing})
    indexed = {}
    for row in rows:
        if any(c not in row for c in needed):
            raise CalculationError('结果行缺少声明的列', code='missing_row_field')
        values = [row[c] for c in keys]
        if any(v is None or isinstance(v, bool) or not isinstance(v, (str, int, float, Decimal))
               or (isinstance(v, (float, Decimal)) and not math.isfinite(v)) for v in values):
            raise CalculationError('对齐键必须为非空有限标量，不能猜测空键的对应关系', code='invalid_alignment_key')
        key = tuple(_period_key(v) for v in values)
        if key in indexed:
            raise CalculationError('对齐键不唯一；请补齐粒度键或先取得同粒度聚合，避免重复计数',
                                   code='duplicate_alignment_key', details={'key_fields': keys})
        indexed[key] = row
    return indexed


def align_results(request, left, right):
    left_keys, right_keys = list(request.join_keys), list(request.join_keys.values())
    first = _index(left, left_keys, request.left_fields.values())
    second = _index(right, right_keys, request.right_fields.values())
    for a, b in request.join_keys.items():
        for attribute in ('unit', 'currency'):
            x, y = _source_metadata(left, a).get(attribute), _source_metadata(right, b).get(attribute)
            if x and y and x != y:
                raise CalculationError('对齐键的单位或币种不一致', code='incompatible_units')
    rows, unmatched = [], 0
    # Full alignment retains observations unique to either side; no silent inner join.
    for key in dict.fromkeys([*first, *second]):
        a, b = first.get(key), second.get(key)
        row = {x: a[x] if a is not None else b[y] for x, y in request.join_keys.items()}
        row.update({out: a[source] if a is not None else None for out, source in request.left_fields.items()})
        row.update({out: b[source] if b is not None else None for out, source in request.right_fields.items()})
        row['alignment_status'] = 'matched' if a is not None and b is not None else 'left_only' if a is not None else 'right_only'
        unmatched += row['alignment_status'] != 'matched'
        rows.append(row)
    metadata = {key: {**_source_metadata(right, request.join_keys[key]), **_source_metadata(left, key)} for key in left_keys}
    for data, result_id, mapping in ((left, request.result_id, request.left_fields),
                                     (right, request.right_result_id, request.right_fields)):
        for out, source in mapping.items():
            metadata[out] = {**_source_metadata(data, source), 'source_result_id': result_id, 'source_field': source}
    diagnostics = ([{'code': 'unmatched_rows', 'count': unmatched,
                     'message': '未匹配行保留，缺失指标为null而非0；不能视为同范围已全部核对'}] if unmatched else [])
    return {'operation': request.operation, 'parameters': request.model_dump(mode='json'),
            'columns': [*left_keys, *request.left_fields, *request.right_fields, 'alignment_status'],
            'rows': rows, 'column_metadata': metadata, 'diagnostics': diagnostics,
            'calculation_complete': not diagnostics, 'complete': True, 'total_rows': len(rows),
            'interpretation': 'explicit_key_alignment_not_causal_evidence'}
