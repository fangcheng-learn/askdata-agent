"""Small, typed calculations over complete, already authorized results.

The caller owns result lookup, permissions, budgets and persistence. This module
does not query a database, call a model, evaluate expressions or infer business
definitions from column names.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator


class CalculationError(ValueError):
    def __init__(self, message: str, *, code: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _column_names(values: list[str], *, nonempty: bool = True) -> list[str]:
    if nonempty and not values:
        raise ValueError('必须至少选择一列')
    if any(not value.strip() for value in values) or len(values) != len(set(values)):
        raise ValueError('列名必须非空且不能重复')
    return values


class _Request(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    result_id: str = Field(min_length=1)


class SumRequest(_Request):
    operation: Literal['sum']
    column: str = Field(min_length=1)


class ColumnStatsRequest(_Request):
    operation: Literal['column_stats']
    column: str = Field(min_length=1)


class ChangeRequest(_Request):
    operation: Literal['change']
    base_field: str = Field(min_length=1)
    current_field: str = Field(min_length=1)

    @model_validator(mode='after')
    def distinct_columns(self):
        if self.base_field == self.current_field:
            raise ValueError('change是同一行两列比较，不能用同一列自减代替跨期比较；长表请使用period_change')
        return self


class RatioRequest(_Request):
    operation: Literal['ratio']
    numerator: str = Field(min_length=1)
    denominator: str = Field(min_length=1)


class _PeriodRequest(_Request):
    period_field: str = Field(min_length=1)
    base_period: str | int | float
    current_period: str | int | float
    group_by: list[str] = Field(default_factory=list)

    @field_validator('base_period', 'current_period')
    @classmethod
    def valid_period(cls, value):
        if isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
            raise ValueError('期间必须为非空文字或有限数值，不能是布尔值')
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('期间数值必须有限')
        return value

    @field_validator('group_by')
    @classmethod
    def valid_groups(cls, values):
        return _column_names(values, nonempty=False)

    @model_validator(mode='after')
    def distinct_periods(self):
        if _period_key(self.base_period) == _period_key(self.current_period):
            raise ValueError('基期与本期不能相同')
        if self.period_field in self.group_by:
            raise ValueError('期间列不能同时作为分组键')
        return self


class PeriodChangeRequest(_PeriodRequest):
    operation: Literal['period_change']
    value_fields: list[str] = Field(min_length=1)

    @field_validator('value_fields')
    @classmethod
    def valid_values(cls, values):
        return _column_names(values)

    @model_validator(mode='after')
    def separate_dimensions(self):
        if set(self.value_fields) & {self.period_field, *self.group_by}:
            raise ValueError('数值列不能同时作为期间或分组键')
        return self


class ProductDecompositionRequest(_PeriodRequest):
    operation: Literal['product_decomposition']
    factor_fields: list[str] = Field(min_length=2, max_length=2)
    product_field: str | None = None

    @field_validator('factor_fields')
    @classmethod
    def valid_factors(cls, values):
        return _column_names(values)

    @model_validator(mode='after')
    def separate_dimensions(self):
        if set(self.factor_fields) & {self.period_field, *self.group_by}:
            raise ValueError('因素列不能同时作为期间或分组键')
        if self.product_field and self.product_field in {self.period_field, *self.group_by}:
            raise ValueError('乘积列不能同时作为期间或分组键')
        return self


class StateDelta(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    column: str = Field(min_length=1)
    coefficient: Literal[-1, 1]


class StatePeriod(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    label: str = Field(min_length=1)
    start: str = Field(min_length=1)
    end: str = Field(min_length=1)


class StateDurationRequest(_Request):
    operation: Literal['state_duration']
    partition_by: list[str] = Field(min_length=1)
    time_field: str = Field(min_length=1)
    order_field: str = Field(min_length=1, description='同一时刻内确定先后的稳定数字序号')
    delta_terms: list[StateDelta] = Field(min_length=1)
    opening_field: str = Field(min_length=1)
    opening_value: str = Field(min_length=1, description='已登记的期初记录标记；期初行包含初始余额')
    periods: list[StatePeriod] = Field(min_length=1, max_length=24)
    timezone_name: str = Field(min_length=1, description='无偏移量的期间/时间戳所使用的明确时区')
    threshold: float = Field(default=0, allow_inf_nan=False, description='累计状态小于等于此值的持续时间')

    @model_validator(mode='after')
    def distinct_fields(self):
        _column_names(self.partition_by)
        _column_names([term.column for term in self.delta_terms])
        _column_names([period.label for period in self.periods])
        _column_names([*self.partition_by, self.time_field, self.order_field, self.opening_field,
                       *(term.column for term in self.delta_terms)])
        return self


CalculatorRequest = Annotated[
    SumRequest | ColumnStatsRequest | ChangeRequest | RatioRequest | PeriodChangeRequest | ProductDecompositionRequest | StateDurationRequest,
    Field(discriminator='operation'),
]
_ADAPTER = TypeAdapter(CalculatorRequest)
_ENVELOPE_FIELDS = {'type', 'goal', 'repair_action_id', 'action_reason'}


def parse_calculation_request(request: dict | _Request) -> _Request:
    """Strip only known Controller metadata, then validate every operation key."""
    if isinstance(request, _Request):
        return request
    if not isinstance(request, dict):
        raise CalculationError('计算请求必须为结构化对象', code='invalid_parameters')
    if 'type' in request and request['type'] != 'calculate':
        raise CalculationError('计算请求type必须为calculate', code='invalid_parameters', details={'path': 'type'})
    values = {key: value for key, value in request.items() if key not in _ENVELOPE_FIELDS}
    try:
        return _ADAPTER.validate_python(values)
    except ValidationError as exc:
        errors = [{key: item[key] for key in ('loc', 'type', 'msg') if key in item}
                  for item in exc.errors(include_url=False, include_context=False, include_input=False)]
        raise CalculationError('计算参数不符合操作协议：' + '; '.join(item['msg'] for item in errors),
                               code='invalid_parameters', details={'errors': errors}) from exc


def model_tool_schema() -> dict:
    """The same operation schema used by execution, not a handwritten example."""
    return _ADAPTER.json_schema()


def _period_key(value: Any) -> tuple:
    if isinstance(value, bool) or value is None:
        return ('other', type(value).__name__, str(value))
    if isinstance(value, (int, float, Decimal)):
        return ('number', Decimal(str(value)))
    return ('text', str(value))


def _number(row: dict, column: str, index: int) -> Decimal | None:
    if column not in row:
        raise CalculationError('真实结果行缺少声明的列：' + column, code='missing_row_field',
                               details={'column': column, 'row': index})
    value = row[column]
    if value is None:
        return None
    if isinstance(value, bool):
        raise CalculationError('布尔字段不能当作数值：' + column, code='non_numeric',
                               details={'column': column, 'row': index})
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CalculationError('计算字段不是数值：' + column, code='non_numeric',
                               details={'column': column, 'row': index}) from None
    if not result.is_finite():
        raise CalculationError('计算字段必须是有限数值：' + column, code='non_finite',
                               details={'column': column, 'row': index})
    return result


def _float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise CalculationError('计算产物超出有限数值范围', code='non_finite_output')
    return result


def _period_rows(request: _PeriodRequest, rows: list[dict]) -> list[tuple[dict, dict | None, dict | None]]:
    groups: dict[tuple, tuple[dict, dict]] = {}
    wanted = {_period_key(request.base_period), _period_key(request.current_period)}
    for index, row in enumerate(rows):
        for column in [request.period_field, *request.group_by]:
            if column not in row:
                raise CalculationError('真实结果行缺少期间／分组列：' + column, code='missing_row_field',
                                       details={'column': column, 'row': index})
        period_value = row[request.period_field]
        if isinstance(period_value, (dict, list, set, tuple)) or (isinstance(period_value, float) and not math.isfinite(period_value)):
            raise CalculationError('期间列必须为有限标量', code='invalid_period_value',
                                   details={'column': request.period_field, 'row': index})
        period = _period_key(period_value)
        if period not in wanted:
            continue
        values = {column: row[column] for column in request.group_by}
        for column, value in values.items():
            if isinstance(value, (dict, list, set, tuple)) or (isinstance(value, float) and not math.isfinite(value)):
                raise CalculationError('分组键必须为有限标量：' + column, code='invalid_group_value')
        key = tuple((column, type(value).__name__, str(value)) for column, value in values.items())
        if key not in groups:
            groups[key] = (values, {})
        periods = groups[key][1]
        if period in periods:
            raise CalculationError('同一分组同一期间存在多行，不能默认相加或取第一行', code='duplicate_period',
                                   details={'group': values, 'period': row[request.period_field], 'row': index})
        periods[period] = row
    if not groups and not request.group_by:
        groups[()] = ({}, {})
    return [(values, periods.get(_period_key(request.base_period)), periods.get(_period_key(request.current_period)))
            for values, periods in groups.values()]


def _source_metadata(data: dict, column: str) -> dict:
    metadata = data.get('column_metadata') or {}
    output = dict(metadata.get(column) or {}) if isinstance(metadata, dict) else {}
    for key, mapping_name in (('unit', 'units'), ('currency', 'currencies'), ('precision', 'precisions')):
        mapping = data.get(mapping_name) or {}
        if key not in output and isinstance(mapping, dict) and column in mapping:
            output[key] = mapping[column]
    return output


def _compatible_values(data: dict, left: str, right: str) -> None:
    first, second = _source_metadata(data, left), _source_metadata(data, right)
    for name in ('unit', 'currency'):
        if first.get(name) and second.get(name) and first[name] != second[name]:
            raise CalculationError('比较列的已知单位或币种不一致，不能直接相减', code='incompatible_units',
                                   details={'attribute': name, 'base': first[name], 'current': second[name]})


def _group_output_fields(request: _PeriodRequest) -> dict[str, str]:
    reserved = {'base_period', 'current_period', 'status'}
    reserved.update({'metric', 'base', 'current', 'change', 'change_rate'} if isinstance(request, PeriodChangeRequest) else
                    {'factor_a', 'factor_b', 'base_product', 'current_product', 'change', 'factor_a_contribution',
                     'factor_b_contribution', 'reconciliation_error', 'method'})
    used = set(reserved) | set(request.group_by)
    mapping = {}
    for column in request.group_by:
        if column not in reserved:
            mapping[column] = column
            continue
        target = 'group_' + column
        while target in used:
            target = 'group_' + target
        mapping[column] = target
        used.add(target)
    return mapping


def execute_calculation(request: dict | _Request, data: dict) -> dict:
    request = parse_calculation_request(request)
    if not isinstance(data, dict) or data.get('complete') is not True:
        raise CalculationError('完整计算需要完整结果；请先取得完整聚合或受控结果引用', code='incomplete_result')
    columns, rows = data.get('columns'), data.get('rows')
    if (not isinstance(columns, list) or not all(isinstance(v, str) for v in columns)
            or len(columns) != len(set(columns)) or not isinstance(rows, list)
            or not all(isinstance(row, dict) for row in rows)):
        raise CalculationError('结果必须包含唯一列名和结构化行数据', code='invalid_result')
    total = data.get('total_rows')
    if isinstance(total, int) and total > len(rows):
        raise CalculationError('保存行数少于总行数，不能把片段作为完整结果计算', code='incomplete_result')
    if isinstance(request, StateDurationRequest):
        from .state_duration import calculate_state_duration
        return calculate_state_duration(request, data)
    needed: list[str]
    if isinstance(request, (SumRequest, ColumnStatsRequest)):
        needed = [request.column]
    elif isinstance(request, ChangeRequest):
        needed = [request.base_field, request.current_field]
    elif isinstance(request, RatioRequest):
        needed = [request.numerator, request.denominator]
    else:
        needed = [request.period_field, *request.group_by]
        needed += request.value_fields if isinstance(request, PeriodChangeRequest) else request.factor_fields
        if isinstance(request, ProductDecompositionRequest) and request.product_field:
            needed.append(request.product_field)
    missing = [column for column in needed if column not in columns]
    if missing:
        raise CalculationError('计算参数必须引用真实结果列：' + ', '.join(missing), code='unknown_columns',
                               details={'missing_columns': missing, 'available_columns': columns})
    diagnostics: list[dict] = []
    metadata: dict = {}
    metric_metadata: dict = {}
    group_field_map: dict = {}
    out: list[dict] = []
    if isinstance(request, (SumRequest, ColumnStatsRequest)):
        if _source_metadata(data, request.column).get('additive') is False:
            raise CalculationError('该指标已明确标为不可加，不能直接求和', code='non_additive')
        raw = [_number(row, request.column, index) for index, row in enumerate(rows)]
        values = [value for value in raw if value is not None]
        out = [{'field': request.column, 'sum': _float(sum(values, Decimal(0))) if values else None,
                'non_null_count': len(values), 'row_count': len(rows)}]
        if len(raw) != len(values):
            diagnostics.append({'code': 'null_values', 'count': len(raw) - len(values), 'column': request.column,
                                'message': '空值未当作0，合计只使用非空值'})
        if isinstance(request, ColumnStatsRequest):
            out[0].update(positive_sum=_float(sum((v for v in values if v > 0), Decimal(0))),
                          negative_sum=_float(sum((v for v in values if v < 0), Decimal(0))),
                          positive_count=sum(v > 0 for v in values), negative_count=sum(v < 0 for v in values))
        metadata['sum'] = _source_metadata(data, request.column)
    elif isinstance(request, (ChangeRequest, RatioRequest)):
        if isinstance(request, ChangeRequest):
            _compatible_values(data, request.base_field, request.current_field)
        for index, row in enumerate(rows):
            result = dict(row)
            left_column = request.base_field if isinstance(request, ChangeRequest) else request.numerator
            right_column = request.current_field if isinstance(request, ChangeRequest) else request.denominator
            left, right = _number(row, left_column, index), _number(row, right_column, index)
            if left is None or right is None:
                diagnostics.append({'code': 'null_value', 'row': index, 'columns': [left_column, right_column]})
            if isinstance(request, ChangeRequest):
                change = right - left if left is not None and right is not None else None
                result.update(change=_float(change), change_rate=_float(change / left) if change is not None and left else None)
                if left == 0:
                    diagnostics.append({'code': 'zero_base', 'row': index, 'column': left_column,
                                        'message': '基期为0，相对变化未定义'})
                metadata['change'] = _source_metadata(data, request.base_field)
                metadata['change_rate'] = {'unit': 'ratio'}
            else:
                result['ratio'] = _float(left / right) if left is not None and right else None
                if right == 0:
                    diagnostics.append({'code': 'zero_denominator', 'row': index, 'column': right_column})
                first, second = _source_metadata(data, left_column), _source_metadata(data, right_column)
                metadata['ratio'] = {'numerator_field': left_column, 'denominator_field': right_column,
                                     'numerator_unit': first.get('unit'), 'denominator_unit': second.get('unit')}
                if first.get('unit') and first.get('unit') == second.get('unit') and first.get('currency') == second.get('currency'):
                    metadata['ratio']['unit'] = 'ratio'
            out.append(result)
    else:
        group_field_map = _group_output_fields(request)
        selected_groups = _period_rows(request, rows)
        if not selected_groups:
            diagnostics.append({'code': 'missing_both_periods', 'message': '没有请求期间的分组数据，未创建或猜测分组'})
        for group, base, current in selected_groups:
            status = ('missing_both_periods' if base is None and current is None else
                      'missing_base_period' if base is None else 'missing_current_period' if current is None else 'ok')
            if status != 'ok':
                diagnostics.append({'code': status, 'group': group})
            shared = {**{group_field_map[key]: value for key, value in group.items()},
                      'base_period': request.base_period, 'current_period': request.current_period}
            if isinstance(request, PeriodChangeRequest):
                for column in request.value_fields:
                    metric_metadata[column] = _source_metadata(data, column)
                    b = _number(base, column, 0) if base is not None else None
                    c = _number(current, column, 1) if current is not None else None
                    item_status = status
                    if status == 'ok' and (b is None or c is None):
                        item_status = 'null_value'
                    elif status == 'ok' and b == 0:
                        item_status = 'zero_base'
                    change = c - b if c is not None and b is not None else None
                    out.append({**shared, 'metric': column, 'base': _float(b), 'current': _float(c),
                                'change': _float(change), 'change_rate': _float(change / b) if change is not None and b else None,
                                'status': item_status})
                    if item_status != status:
                        diagnostics.append({'code': item_status, 'group': group, 'column': column})
                metadata['change_rate'] = {'unit': 'ratio'}
            else:
                factor_a, factor_b = request.factor_fields
                numbers = ([_number(base, factor_a, 0), _number(base, factor_b, 0),
                            _number(current, factor_a, 1), _number(current, factor_b, 1)]
                           if base is not None and current is not None else [None] * 4)
                a0, b0, a1, b1 = numbers
                if status == 'ok' and any(value is None for value in numbers):
                    status = 'null_value'
                    diagnostics.append({'code': status, 'group': group})
                if status == 'ok':
                    product0, product1 = a0 * b0, a1 * b1
                    if request.product_field:
                        for row, product in ((base, product0), (current, product1)):
                            actual = _number(row, request.product_field, 0)
                            tolerance = max(Decimal('0.000001'), abs(product) * Decimal('1e-10'))
                            if actual is None or abs(actual - product) > tolerance:
                                raise CalculationError('两因素乘积与实际总量不一致，不能据此分解', code='product_identity_mismatch',
                                                       details={'group': group, 'product_field': request.product_field})
                    effect_a = (a1 - a0) * (b0 + b1) / 2
                    effect_b = (b1 - b0) * (a0 + a1) / 2
                    change = product1 - product0
                else:
                    product0 = product1 = effect_a = effect_b = change = None
                out.append({**shared, 'factor_a': factor_a, 'factor_b': factor_b,
                            'base_product': _float(product0), 'current_product': _float(product1), 'change': _float(change),
                            'factor_a_contribution': _float(effect_a), 'factor_b_contribution': _float(effect_b),
                            'reconciliation_error': _float(effect_a + effect_b - change) if change is not None else None,
                            'method': 'symmetric_midpoint', 'status': status})
                if request.product_field:
                    for column in ('base_product', 'current_product', 'change', 'factor_a_contribution', 'factor_b_contribution'):
                        metadata[column] = _source_metadata(data, request.product_field)
    output_columns = list(out[0]) if out else list(columns)
    if not out and isinstance(request, _PeriodRequest):
        output_columns = [*group_field_map.values(), 'base_period', 'current_period']
        output_columns += (['metric', 'base', 'current', 'change', 'change_rate', 'status'] if isinstance(request, PeriodChangeRequest) else
                           ['factor_a', 'factor_b', 'base_product', 'current_product', 'change', 'factor_a_contribution',
                            'factor_b_contribution', 'reconciliation_error', 'method', 'status'])
    if not out and isinstance(request, ChangeRequest):
        output_columns += [column for column in ['change', 'change_rate'] if column not in output_columns]
    if not out and isinstance(request, RatioRequest) and 'ratio' not in output_columns:
        output_columns.append('ratio')
    return {'operation': request.operation, 'parameters': request.model_dump(mode='json'),
            'columns': output_columns, 'rows': out, 'column_metadata': metadata,
            'metric_metadata': metric_metadata, 'group_field_map': group_field_map,
            'diagnostics': diagnostics, 'calculation_complete': not diagnostics,
            'complete': True, 'total_rows': len(out),
            'interpretation': 'arithmetic_decomposition_not_causal_effect' if isinstance(request, ProductDecompositionRequest) else 'deterministic_calculation'}
