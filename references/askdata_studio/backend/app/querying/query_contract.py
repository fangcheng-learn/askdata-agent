from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from ..database import SCHEMA, physical_table_name
from ..security import AccessScope
from .duckdb_engine import DuckDbEngine
from ..homenova import is_homenova, registered_metric
from .metric_sources import normalize_source_clarification


class MetricContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    definition: str = ''
    output_columns: list[str] = Field(default_factory=list, description='该业务度量在最终SELECT中的实际输出别名；不是新增SQL字段')
    source_fields: list[str] = Field(min_length=1)
    aggregation: Literal["sum", "avg", "count", "count_distinct", "min", "max", "derived", "none"]


class FilterContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "in", "between", "like"]
    value: Any

    @field_validator('operator', mode='before')
    @classmethod
    def normalize_operator(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().lower()
            return '!=' if value == '<>' else value
        return value


class TimeRangeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    start: str | None = Field(description='包含起点；通常使用 field >= start')
    end: str | None = Field(description='结束边界，必须用end_inclusive明确是否包含；推荐排他终点')
    end_inclusive: bool | None = Field(default=None, description='false为排他终点；true为包含终点日期全天；省略仅用于旧格式兼容，不能认证业务时间完整性')


class DerivedDimensionContract(BaseModel):
    """Server-observed SQL lineage, never a model's assertion of correctness."""

    model_config = ConfigDict(extra="forbid")

    output_name: str = Field(min_length=1)
    expression: str = Field(min_length=1)
    source_fields: list[str]
    source_expressions: list[str] = Field(default_factory=list)
    kind: Literal['expression', 'literal', 'union']
    role: Literal['time', 'dimension']


class QueryContract(BaseModel):
    """模型声明的单次查询意图；通过校验后仍只代表 Schema 层可信。"""

    model_config = ConfigDict(extra="forbid")

    original_question: str = Field(min_length=1)
    data_source: str = Field(min_length=1)
    tables: list[str] = Field(min_length=1)
    metrics: list[MetricContract]
    dimensions: list[str]
    derived_dimensions: list[DerivedDimensionContract] = Field(default_factory=list)
    filters: list[FilterContract]
    time_range: TimeRangeContract | None
    time_ranges: list[TimeRangeContract] = Field(default_factory=list)
    grain: Literal["overall", "dimension", "day", "week", "month", "detail"]
    assumptions: list[str]
    open_questions: list[str]


class QueryContractValidator:
    def __init__(self, engine: DuckDbEngine) -> None:
        self.engine = engine

    def validate(
        self,
        raw: Any,
        sql: str,
        original_question: str,
        database: str,
        schema_graph: dict[str, Any],
        access_scope: dict[str, Any],
        validation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .query_protocol import _SqlLineage, complete_contract
        raw, protocol_normalizations = complete_contract(raw, sql, original_question, database, schema_graph)
        try:
            contract = QueryContract.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"query_contract结构不完整或无效：{exc.errors(include_url=False)}") from exc
        normalizations: list[dict[str, Any]] = list(protocol_normalizations)
        pending_checks: list[dict[str, Any]] = []

        def normalize(path: str, before: Any, after: Any, reason: str) -> None:
            if before != after:
                normalizations.append({'path': path, 'before': before, 'after': after, 'reason': reason})

        def pending(code: str, message: str, **details: Any) -> None:
            check = {'code': code, 'message': message, **details}
            if check not in pending_checks:
                pending_checks.append(check)

        normalize('original_question', contract.original_question, original_question, '服务端绑定任务原文')
        contract.original_question = original_question
        provided = raw if isinstance(raw, dict) else contract.model_dump()
        for index, item in enumerate(contract.filters):
            normalize(f'filters.{index}.operator', provided['filters'][index]['operator'], item.operator, '等价运算符规范化')
        if contract.data_source != database:
            raise ValueError("query_contract.data_source与目标数据库不一致")
        if contract.open_questions:
            raise ValueError("查询仍有待澄清事项：" + "；".join(contract.open_questions))
        if not contract.metrics and not contract.dimensions:
            raise ValueError("query_contract缺少指标和分析维度")
        if contract.grain == "overall" and contract.dimensions:
            raise ValueError("总体汇总粒度不能同时声明分组维度")
        if contract.grain in {"dimension", "day", "week", "month"} and not contract.dimensions:
            raise ValueError("分组或时间粒度缺少维度字段")
        reporting_periods = None
        if (validation_context or {}).get('postprocess') is not None:
            from .sql_output_protocol import normalize_postprocess
            # A typed, actually executed state calculation applies reporting
            # periods after reconstruction. Raw history must retain its opening
            # balance; no ordinary SQL or permission checks are relaxed.
            reporting_periods = normalize_postprocess(validation_context['postprocess'])['periods']
        if (re.search(r"20\d{2}[-/年]\d{1,2}", original_question)
                and not contract.time_range and not contract.time_ranges and not reporting_periods):
            raise ValueError("用户指定了日期，但query_contract缺少时间范围")
        if reporting_periods:
            pending('postprocessed_reporting_periods', '报告期间在受控状态计算中应用；原始台账不按月份裁剪',
                    periods=reporting_periods)


        scope = AccessScope.from_dict(access_scope)
        graph_tables = {
            str(table["name"]): str(table["id"])
            for table in schema_graph.get("tables", [])
            if table.get("database") == database
        }
        table_names = {name.casefold(): name for name in graph_tables}
        for index, name in enumerate(contract.tables):
            canonical = table_names.get(name.casefold(), name)
            normalize(f'tables.{index}', name, canonical, '按Schema规范化表名大小写')
            contract.tables[index] = canonical
        declared = set(contract.tables)
        if len(declared) != len(contract.tables) or not declared <= set(graph_tables):
            raise ValueError(f'query_contract引用了Schema图之外的表或重复表：{sorted(declared-set(graph_tables))}；'
                             f'当前授权物理表名：{sorted(graph_tables)}，内部ID不能直接作为SQL表名')
        if any(not scope.allows_table(database, graph_tables[name]) for name in declared):
            raise PermissionError("query_contract引用了无权访问的表")

        available_definitions = {
            physical_table_name(table): table
            for table in SCHEMA
            if table.get("database", "askdata_mock") == database
            and physical_table_name(table) in graph_tables
            and scope.allows_table(database, table["id"])
        }
        definitions = {name: table for name, table in available_definitions.items() if name in declared}
        known_fields = {
            f"{table_name}.{field['name']}": field
            for table_name, table in definitions.items()
            for field in table.get("fields", [])
        }
        field_names = {name.casefold(): name for name in known_fields}

        def canonical_field(path: str, name: str) -> str:
            canonical = field_names.get(name.casefold(), name)
            normalize(path, name, canonical, '按Schema规范化字段大小写')
            return canonical

        for index, metric in enumerate(contract.metrics):
            metric.source_fields = [canonical_field(f'metrics.{index}.source_fields.{position}', name)
                                    for position, name in enumerate(metric.source_fields)]
        contract.dimensions = [canonical_field(f'dimensions.{index}', name) for index, name in enumerate(contract.dimensions)]
        for index, item in enumerate(contract.derived_dimensions):
            item.source_fields = [canonical_field(f'derived_dimensions.{index}.source_fields.{position}', name)
                                  for position, name in enumerate(item.source_fields)]
        for index, item in enumerate(contract.filters):
            item.field = canonical_field(f'filters.{index}.field', item.field)
        for index, item in enumerate(([contract.time_range] if contract.time_range else []) + contract.time_ranges):
            item.field = canonical_field(f'time_ranges.{index}.field', item.field)

        def field_definition(reference: str) -> dict[str, Any]:
            definition = known_fields.get(reference)
            if definition is None:
                raise ValueError(f"query_contract引用了不存在的字段：{reference}；引用必须为table.physical_field，不能使用SQL别名或CTE派生列；当前来源可用字段：{sorted(known_fields)}")
            return definition

        def normalized_literal(reference: str, value: Any) -> tuple[str, Any]:
            field = known_fields.get(reference, {})
            sql_type = str(field.get('sql_type') or field.get('source_type') or '').upper()
            if (any(token in sql_type for token in ('INT', 'DOUBLE', 'FLOAT', 'DECIMAL', 'NUMERIC', 'REAL'))
                    or field.get('type') in {'整数', '数值'}):
                try:
                    number = Decimal(str(value))
                    if number.is_finite():
                        return ('number', number)
                except (InvalidOperation, ValueError, TypeError):
                    pass
            if is_homenova(database) and str(field.get('sql_type', '')).upper() == 'TIMESTAMPTZ':
                try:
                    text = re.sub(r'[zZ]$', '+00:00', str(value).strip())
                    instant = datetime.fromisoformat(text)
                    if instant.tzinfo is None:
                        instant = instant.replace(tzinfo=ZoneInfo('Europe/Berlin'))
                    return ('instant', instant.astimezone(timezone.utc))
                except (ValueError, TypeError):
                    pass
            # SQL identifiers are case-insensitive; stored string values are not.
            return ('literal', str(value).lower() if isinstance(value, bool) else str(value))

        literal_dimensions = {item.output_name for item in contract.derived_dimensions if not item.source_fields}
        derived_sources = {source for item in contract.derived_dimensions for source in item.source_fields}
        for item in contract.derived_dimensions:
            for reference in item.source_fields:
                field_definition(reference)  # Every origin still needs permission/schema validation.
        for reference in contract.dimensions:
            if reference in literal_dimensions:
                pending('literal_dimension_not_proven', 'SQL输出的常量分组标签没有物理来源，不证明业务期间或分类正确', output=reference)
                continue
            field = field_definition(reference)
            if field.get("role") not in {"dimension", "time", "filter", "identifier", "foreign_key"}:
                if reference in derived_sources:
                    pending('derived_dimension_semantics_not_proven', '分组表达式使用了真实数值来源，需核对分组规则的业务含义', field=reference)
                else:
                    raise ValueError(f"字段不能作为分析维度：{reference}")
        if contract.grain in {"day", "week", "month"} and not any(
            field_definition(reference).get("role") == "time"
            for reference in contract.dimensions if reference not in literal_dimensions
        ):
            raise ValueError("时间聚合粒度缺少Schema时间维度")
        for item in contract.filters:
            field_definition(item.field)
            if item.value is None or item.value == "" or item.value == []:
                raise ValueError(f"筛选条件缺少取值：{item.field}")
        time_ranges = ([contract.time_range] if contract.time_range else []) + contract.time_ranges
        for time_range in time_ranges:
            field = field_definition(time_range.field)
            if field.get("role") != "time":
                raise ValueError(f"时间范围未使用Schema时间字段：{time_range.field}")
            if not time_range.start and not time_range.end:
                raise ValueError("时间范围缺少起止边界")
            if (time_range.start and time_range.end
                    and (normalized_literal(time_range.field, time_range.start) > normalized_literal(time_range.field, time_range.end)
                         or (time_range.end_inclusive is False and normalized_literal(time_range.field, time_range.start)
                             == normalized_literal(time_range.field, time_range.end)))):
                raise ValueError("时间范围起点不得晚于终点")
        registered_source_checks = []
        monetary_sources: dict[str, set[str]] = {}
        alias_metric_checks = []
        context = validation_context or {}
        confirmation = context.get('confirmed_metric_sources') or {}
        confirmed_sources: dict[str, str] = {}
        if confirmation:
            if (confirmation.get('database') != database or not context.get('task_id')
                    or confirmation.get('task_id') != context.get('task_id')):
                pending('metric_confirmation_scope_mismatch', '指标来源确认不属于当前任务或数据源，未用作已确认依据')
            else:
                choice = normalize_source_clarification(
                    {'options': [{'metric_sources': confirmation.get('sources') or {}}]},
                    database, schema_graph, access_scope)
                confirmed_sources = choice['options'][0]['metric_sources']
        for index, metric in enumerate(contract.metrics):
            fields = [field_definition(reference) for reference in metric.source_fields]
            metric_names = {metric.name.casefold(), *(
                str(name).casefold() for field in fields for name in
                (field.get('name', ''), field.get('label', ''), *(field.get('aliases') or [])))}
            selected_sources = {source for name, source in confirmed_sources.items() if name in metric_names}
            if selected_sources and not selected_sources <= set(metric.source_fields):
                raise ValueError(f'指标来源与用户已确认来源冲突：{metric.name}；应使用{sorted(selected_sources)}')
            registered = registered_metric(database, metric.name)
            if is_homenova(database) and registered:
                normalize(f'metrics.{index}.name', metric.name, registered['id'], '按注册指标ID绑定名称')
                normalize(f'metrics.{index}.definition', metric.definition, registered['definition'], '从公开注册信息读取定义，不认证模型复述')
                metric.name = registered['id']
                metric.definition = registered['definition']
                unit = str(registered.get('unit') or '')
                if unit.startswith('EUR_cents'):
                    monetary = [str(field.get('unit') or '') for field in fields
                                if str(field.get('unit') or '').startswith('EUR_cents')]
                    expected = 'EUR_cents_ex_vat' if 'ex_vat' in unit else 'EUR_cents_gross'
                    if not monetary or any(not value.startswith(expected) for value in monetary):
                        raise ValueError(f'指标计量单位与来源冲突：{metric.name}需要{expected}金额来源，不能用件数或其他税口径替代')
                    monetary_sources[metric.name] = {reference for reference, field in zip(metric.source_fields, fields)
                                                     if str(field.get('unit') or '').startswith(expected)}
                allowed_sources = {f"{item['table']}.{item['field']}" for item in registered.get("required_fields", [])}
                registered_source_checks.append((metric, allowed_sources))
                # A registered business metric is not a physical column label.
                # Its declared SQL aggregation is still checked below.
                continue
            if metric.aggregation == "derived":
                pending('unregistered_derived_metric', '派生计算没有已登记定义，需结合SQL和结果核对，不能认证模型自述', metric=metric.name)
            elif len(fields) != 1:
                pending('metric_composition_not_proven', '指标涉及多个真实字段，需结合SQL核对组合含义', metric=metric.name)
            else:
                field = fields[0]
                names = [field.get("name", ""), field.get("label", ""), *(field.get("aliases") or [])]
                candidates = [
                    (table, candidate)
                    for table in available_definitions.values()
                    for candidate in table.get("fields", [])
                    if metric.name.casefold() in {
                        str(name).casefold() for name in
                        (candidate.get("name", ""), candidate.get("label", ""), *(candidate.get("aliases") or []))
                    }
                ]
                candidate_sources = sorted({physical_table_name(table) + '.' + candidate['name']
                                            for table, candidate in candidates})
                if len(candidate_sources) > 1 and not selected_sources:
                    pending('metric_source_not_proven', '同名指标有多个候选来源，文字描述差异不证明定义冲突；结合用户确认、SQL及数据核对',
                            metric=metric.name, selected_sources=metric.source_fields, candidates=candidate_sources,
                            clarification_history=context.get('clarification_history') or [])
                if metric.name.casefold() not in {str(name).casefold() for name in names}:
                    normalize(f'metrics.{index}.name', metric.name, field['name'], '未知显示名称只绑定真实物理计量，不注册新业务指标')
                    metric.name = field['name']
                normalize(f'metrics.{index}.definition', metric.definition, str(field.get('description') or ''), '从真实字段读取描述')
                metric.definition = str(field.get('description') or '')
                alias_metric_checks.append(metric)

        try:
            statements = [item for item in sqlglot.parse(sql, read="duckdb") if item]
        except sqlglot.errors.ParseError as exc:
            raise ValueError(f"SQL解析失败：{exc}") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("SQL必须是单条只读查询")
        statement = statements[0]
        if (contract.grain in {"dimension", "day", "week", "month"}
                and any(metric.aggregation in {"sum", "avg", "count", "count_distinct", "min", "max"}
                        for metric in contract.metrics)
                and not any(statement.find_all(exp.Group))):
            pending('grouping_expression_not_proven', '未使用GROUP BY；可能为窗口或分期聚合，需结合返回粒度核对')
        # Use exactly the same authorized/normalized SQL as execution. This also
        # resolves explicit CTE stars before doing physical column lineage.
        sql = self.engine._validate_sql(database, sql, scope)
        statement = sqlglot.parse_one(sql, read='duckdb')
        scopes = traverse_scope(statement)
        sql_lineage = _SqlLineage(scopes, known_fields)
        sql_tables = {table_names.get(source.name.casefold(), source.name)
                      for query_scope in scopes for source in query_scope.sources.values()
                      if isinstance(source, exp.Table)}
        if sql_tables != declared:
            raise ValueError(f"query_contract来源表与SQL不一致：声明{sorted(declared)}，SQL使用{sorted(sql_tables)}")

        # Resolve physical lineage through table aliases and CTE outputs. Comparing
        # bare names both falsely rejected valid CTE aggregates and confused two
        # tables that happen to share a column name.
        scope_by_select = {id(scope.expression): scope for scope in scopes}
        data_scopes: set[int] = set()
        frontier = [scopes[-1]] if scopes else []
        while frontier:
            query_scope = frontier.pop()
            if id(query_scope) in data_scopes:
                continue
            data_scopes.add(id(query_scope))
            frontier.extend(query_scope.union_scopes)
            frontier.extend(source for _, source in query_scope.selected_sources.values() if isinstance(source, Scope))

        def node_scope(node: exp.Expression) -> Scope | None:
            while node is not None:
                if id(node) in scope_by_select:
                    return scope_by_select[id(node)]
                node = node.parent
            return None

        def physical_refs(column: exp.Column, scope: Scope | None, seen: set | None = None,
                          value_only: bool = False) -> set[str]:
            if scope is None:
                return set()
            if not value_only:
                observed = sql_lineage.resolve(column, scope, seen)
                return observed['fields'] if not observed['unresolved'] else set()
            seen = set(seen or ())
            key = (id(scope), column.table, column.name)
            if key in seen:
                return set()
            seen.add(key)
            sources = sql_lineage.sources(column, scope)
            found = set()
            for alias, source in sources:
                if isinstance(source, exp.Table):
                    ref = field_names.get((source.name + '.' + column.name).casefold())
                    if ref in known_fields:
                        found.add(ref)
                elif isinstance(source, Scope):
                    if source.union_scopes:
                        names = source.outer_columns or [projection.alias_or_name for projection in source.expression.selects]
                        positions = [index for index, name in enumerate(names) if name.casefold() == column.name.casefold()]
                        if len(positions) == 1:
                            for branch in source.union_scopes:
                                if positions[0] < len(branch.expression.selects):
                                    found.update(value_refs(branch.expression.selects[positions[0]], branch, seen))
                        continue
                    projections = source.expression.selects
                    names = source.outer_columns or [projection.alias_or_name for projection in projections]
                    for name, projection in zip(names, projections):
                        if name.casefold() == column.name.casefold():
                            if value_only:
                                found.update(value_refs(projection, source, seen))
                            else:
                                for child in projection.find_all(exp.Column):
                                    found.update(physical_refs(child, source, seen))
            return found

        def value_refs(node: exp.Expression | None, scope: Scope | None = None, seen: set | None = None) -> set[str]:
            """Value lineage: a CASE predicate is not the amount being summed."""
            if node is None:
                return set()
            if isinstance(node, exp.Column):
                return physical_refs(node, node_scope(node) or scope, seen, value_only=True)
            if isinstance(node, exp.Case):
                children = [branch.args.get('true') for branch in node.args.get('ifs', [])] + [node.args.get('default')]
            elif isinstance(node, exp.If):
                children = [node.args.get('true'), node.args.get('false')]
            else:
                children = list(node.iter_expressions())
            return {reference for child in children for reference in value_refs(child, scope, seen)}

        def refs(node: exp.Expression) -> set[str]:
            return {ref for column in node.find_all(exp.Column)
                    for ref in physical_refs(column, node_scope(column))}

        def identity_column(node: exp.Expression, seen: set | None = None) -> bool:
            while isinstance(node, (exp.Alias, exp.Paren)):
                node = node.this
            if not isinstance(node, exp.Column):
                return False
            query_scope = node_scope(node)
            if query_scope is None:
                return False
            seen = set(seen or ())
            key = (id(query_scope), node.table, node.name)
            if key in seen:
                return False
            seen.add(key)
            for alias, (_, source) in query_scope.selected_sources.items():
                if node.table and alias.casefold() != node.table.casefold():
                    continue
                if isinstance(source, exp.Table) and (source.name + '.' + node.name).casefold() in field_names:
                    return True
                if isinstance(source, Scope):
                    names = source.outer_columns or [projection.alias_or_name for projection in source.expression.selects]
                    for name, projection in zip(names, source.expression.selects):
                        if name.casefold() == node.name.casefold() and identity_column(projection, seen):
                            return True
            return False

        sql_columns = refs(statement)
        def count_star_binding(metric: MetricContract, node: exp.Expression) -> bool:
            if metric.aggregation != 'count' or len(metric.source_fields) != 1:
                return False
            table, column = metric.source_fields[0].split('.', 1)
            if column not in definitions.get(table, {}).get('primary_key', []):
                return False
            for count in node.find_all(exp.Count):
                scope = node_scope(count)
                if not isinstance(count.this, exp.Star) or scope is None:
                    continue
                # COUNT(non-null PK) and COUNT(*) count the SAME joined rows,
                # even with fanout. This does not prove COUNT(DISTINCT PK), or
                # that the joined row set is the right business population.
                owner_aliases = [alias for alias, (_, source) in scope.selected_sources.items()
                                 if isinstance(source, exp.Table) and source.name.casefold() == table.casefold()]
                base = scope.expression.args.get('from_')
                nonnull = {base.this.alias_or_name} if base and base.this else set()
                for join in scope.expression.args.get('joins') or []:
                    alias, side = join.this.alias_or_name, str(join.args.get('side') or '').upper()
                    if side == 'FULL':
                        nonnull.clear()
                    elif side == 'RIGHT':
                        nonnull = {alias}
                    elif side != 'LEFT':
                        nonnull.add(alias)
                if len(owner_aliases) == 1 and owner_aliases[0] in nonnull:
                    if scope.expression.args.get('joins'):
                        pending('joined_row_count_not_entity_count',
                                'COUNT(*)等价于当前连接结果的非空主键计数，不代表去重实体数；连接粒度仍需核对',
                                metric=metric.name)
                    return True
            return False

        metric_bindings = []
        output_names = {p.alias_or_name for p in statement.selects if p.alias_or_name}
        for metric in contract.metrics:
            if metric.output_columns:
                actual = [name for name in metric.output_columns if name in output_names]
                metric_bindings.append({'declared_name': metric.name, 'source_fields': metric.source_fields,
                    'output_aliases': actual, 'binding_basis': 'declared_actual_output_not_semantic_proof'})
                if len(actual) != len(metric.output_columns):
                    pending('measurement_output_unbound', '声明的部分输出列不存在，未绑定或猜测对应关系',
                            metric=metric.name, missing=sorted(set(metric.output_columns)-output_names))
        for metric in alias_metric_checks:
            # Bind outputs by actual physical lineage, independently of labels.
            bound = [projection for projection in statement.selects
                     if set(metric.source_fields) <= refs(projection) or count_star_binding(metric, projection)]
            for projection in bound:
                for arithmetic in projection.find_all(exp.Mul, exp.Div):
                    if not refs(arithmetic) & set(metric.source_fields):
                        continue
                    # A raw quantity multiplied by a constant is not its SUM.
                    # Currency conversion or other nontrivial expressions are
                    # retained for review rather than guessed by a rule engine.
                    quantities = [known_fields[ref] for ref in metric.source_fields
                                  if known_fields[ref].get('unit') in {'销售单位', 'sales_units'}]
                    literals = [node for node in [arithmetic.this, arithmetic.expression] if isinstance(node, exp.Literal)]
                    if quantities and literals and any(str(node.this) not in {'1', '1.0'} for node in literals):
                        raise ValueError(f'原始数量计算被缩放，与声明计量不一致：{metric.name}')
                    pending('output_expression_not_proven', '输出含计算表达式，需结合实际单位与任务要求核对', metric=metric.name, output=projection.alias_or_name)
            if not bound:
                pending('output_expression_not_proven', '未能直接解析指标到返回列的映射，保留SQL供后续核对', metric=metric.name)
            metric_bindings.append({'declared_name': metric.name, 'source_fields': metric.source_fields,
                                    'output_aliases': list(dict.fromkeys(p.alias_or_name for p in bound))})
        equivalences: dict[str, set[str]] = {}
        for join in statement.find_all(exp.Join):
            # An inner equijoin makes the two physical keys interchangeable
            # within the returned row set. Outer joins do not provide this guarantee.
            if join.side or str(join.kind).upper() not in {"", "INNER"}:
                continue
            on = join.args.get("on")
            if on is None:
                continue
            for eq in on.find_all(exp.EQ):
                if not isinstance(eq.this, exp.Column) or not isinstance(eq.expression, exp.Column):
                    continue
                left, right = refs(eq.this), refs(eq.expression)
                if len(left) == len(right) == 1:
                    a, b = next(iter(left)), next(iter(right))
                    equivalences.setdefault(a, set()).add(b)
                    equivalences.setdefault(b, set()).add(a)
        # A grouping/filter/join dependency is not the value being measured.
        # Only waive an overinclusive declaration when the actual SQL proves
        # that the field is contextual and never an aggregate value input.
        contextual_refs = set()
        for query_scope in scopes:
            expression = query_scope.expression
            for key in ('group', 'where', 'having', 'qualify'):
                if expression.args.get(key) is not None:
                    contextual_refs.update(refs(expression.args[key]))
            for join in expression.args.get('joins') or []:
                if join.args.get('on') is not None:
                    contextual_refs.update(refs(join.args['on']))
        aggregate_value_refs = {ref for aggregate in statement.find_all(exp.AggFunc)
                                for ref in value_refs(aggregate)}
        for metric, allowed_sources in registered_source_checks:
            projections = [p for p in statement.selects if p.alias_or_name in metric.output_columns]
            used_as_value = ({ref for projection in projections for ref in value_refs(projection)}
                             if projections else aggregate_value_refs)
            for reference in metric.source_fields:
                reachable, frontier = {reference}, [reference]
                while frontier:
                    for neighbor in equivalences.get(frontier.pop(), set()) - reachable:
                        reachable.add(neighbor)
                        frontier.append(neighbor)
                if not reachable & allowed_sources:
                    if reference in contextual_refs and reference not in used_as_value:
                        pending('metric_context_dependency',
                            '该额外字段仅参与分组/筛选/关联，不作为聚合取值来源；仍需核对统计集合与连接影响',
                            metric=metric.name, field=reference, role='query_context_not_measure_value')
                        continue
                    raise ValueError(f"指标来源超出公开依赖且无等值内连接依据：{metric.name} / {reference}")
        def expanded_refs(references: set[str]) -> set[str]:
            output, frontier = set(references), list(references)
            while frontier:
                for neighbor in equivalences.get(frontier.pop(), set()) - output:
                    output.add(neighbor)
                    frontier.append(neighbor)
            return output

        where_nodes = [*statement.find_all(exp.Where), *statement.find_all(exp.Having)]
        filter_scopes = where_nodes + [
            join.args['on'] for join in statement.find_all(exp.Join) if join.args.get('on') is not None
        ]
        predicate_scopes = filter_scopes + [node.this for node in statement.find_all(exp.If)]
        operators: dict[str, type[exp.Expression]] = {
            "=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE,
            "<": exp.LT, "<=": exp.LTE, "in": exp.In,
            "between": exp.Between, "like": exp.Like,
        }

        invert = {exp.EQ: exp.NEQ, exp.NEQ: exp.EQ, exp.GT: exp.LTE, exp.GTE: exp.LT, exp.LT: exp.GTE, exp.LTE: exp.GT}
        reverse = {exp.EQ: exp.EQ, exp.NEQ: exp.NEQ, exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}

        def literal_value(node: exp.Expression | None) -> Any:
            from .sql_literals import constant_value
            return constant_value(node)

        def implies(node: exp.Expression, reference: str, kinds: tuple[type[exp.Expression], ...],
                    wanted: tuple, negated: bool = False, allow_transformed: bool = False) -> bool:
            if isinstance(node, (exp.Where, exp.Having, exp.Paren)):
                return implies(node.this, reference, kinds, wanted, negated, allow_transformed)
            if isinstance(node, exp.Not):
                return implies(node.this, reference, kinds, wanted, not negated, allow_transformed)
            if isinstance(node, (exp.And, exp.Or)):
                # Ignore literal Boolean identities without inventing a SQL
                # theorem prover for arbitrary expressions.
                for branch, other in [(node.this, node.expression), (node.expression, node.this)]:
                    if isinstance(branch, exp.Boolean):
                        truth = bool(branch.this) != negated
                        conjunction = isinstance(node, exp.And) != negated
                        if truth == conjunction:
                            return implies(other, reference, kinds, wanted, negated, allow_transformed)
                parts = [implies(node.this, reference, kinds, wanted, negated, allow_transformed),
                         implies(node.expression, reference, kinds, wanted, negated, allow_transformed)]
                # A condition in only one OR branch does not constrain all rows.
                conjunction = isinstance(node, exp.And) != negated
                return any(parts) if conjunction else all(parts)
            kind = type(node)
            field_node = node.this if isinstance(node, tuple(operators.values())) else None
            if field_node is None:
                return False
            if isinstance(node, exp.Between):
                actual = [literal_value(node.args.get('low')), literal_value(node.args.get('high'))]
                if not negated and exp.GTE in kinds:
                    kind, actual = exp.GTE, actual[:1]
                elif not negated and exp.LTE in kinds:
                    kind, actual = exp.LTE, actual[1:]
            elif isinstance(node, exp.In):
                actual = [literal_value(part) for part in node.expressions]
            else:
                value = literal_value(node.expression)
                if reference not in refs(field_node) and reference in refs(node.expression):
                    field_node, value = node.expression, literal_value(node.this)
                    kind = reverse.get(kind, kind)
                actual = [value]
            if reference not in refs(field_node) or not actual or any(value is None for value in actual):
                return False
            if not allow_transformed and not identity_column(field_node):
                return False
            if negated:
                kind = invert.get(kind) if kind != exp.In else (exp.NEQ if len(actual) == 1 else None)
            actual_values = tuple(normalized_literal(reference, value) for value in actual)
            if kind == exp.In and len(actual) == 1 and exp.EQ in kinds:
                kind = exp.EQ
            if kind == exp.EQ and len(wanted) == 1 and exp.In in kinds:
                kind = exp.In
            same_values = set(actual_values) == set(wanted) if kind == exp.In else actual_values == wanted
            return kind in kinds and same_values

        def required_value_conflict(node: exp.Expression, reference: str,
                                    kinds: tuple[type[exp.Expression], ...], wanted: tuple) -> bool:
            if isinstance(node, (exp.Where, exp.Having, exp.Paren)):
                return required_value_conflict(node.this, reference, kinds, wanted)
            if isinstance(node, exp.And):
                return any(required_value_conflict(part, reference, kinds, wanted) for part in (node.this, node.expression))
            if isinstance(node, exp.Or):
                return all(required_value_conflict(part, reference, kinds, wanted) for part in (node.this, node.expression))
            kind = type(node)
            if isinstance(node, exp.Not) and isinstance(node.this, (exp.EQ, exp.NEQ)):
                kind, node = invert[type(node.this)], node.this
            if not isinstance(node, (exp.EQ, exp.NEQ, exp.In)) or not set(kinds) & {exp.EQ, exp.In}:
                return False
            field_node = node.this
            values = [literal_value(part) for part in node.expressions] if isinstance(node, exp.In) else [literal_value(node.expression)]
            if reference not in refs(field_node) and not isinstance(node, exp.In):
                field_node, values = node.expression, [literal_value(node.this)]
            if reference not in refs(field_node) or not identity_column(field_node) or not values or any(value is None for value in values):
                return False
            actual = {normalized_literal(reference, value) for value in values}
            return (actual.isdisjoint(wanted) if kind in {exp.EQ, exp.In}
                    else len(wanted) == 1 and actual == set(wanted))

        def global_value_conflict(reference: str, kinds: tuple[type[exp.Expression], ...], wanted: tuple) -> bool:
            conflicts = [node for node in where_nodes if id(node_scope(node)) in data_scopes
                         and required_value_conflict(node, reference, kinds, wanted)]
            if not conflicts:
                return False
            matching = [node for node in where_nodes if id(node_scope(node)) in data_scopes
                        and implies(node, reference, kinds, wanted)]
            # The same physical column can describe independent relation
            # instances (e.g. opening and reply events joined after aggregation).
            # Flattening their WHEREs invents a global contradiction. Retain
            # explicit uncertainty about which component the metric binds to.
            # Same-scope contradictions and alternative UNION populations do
            # not qualify for this independent-component case.
            conflict_scopes = {id(node_scope(node)) for node in conflicts}
            matching_scopes = {id(node_scope(node)) for node in matching}
            if (matching_scopes and conflict_scopes.isdisjoint(matching_scopes)
                    and any(statement.find_all(exp.Join))
                    and not any(statement.find_all(exp.SetOperation))):
                pending('filter_relation_scope_not_proven',
                        '同一物理字段在独立关联分量使用不同条件；需按指标血缘核对各分量，不能把条件展平成总体冲突',
                        field=reference)
                return False
            return True

        def has_predicate(reference: str, kinds: tuple[type[exp.Expression], ...], values: list[Any]) -> bool:
            wanted = tuple(normalized_literal(reference, value) for value in values)
            if global_value_conflict(reference, kinds, wanted):
                return False
            strict = [node for node in predicate_scopes if implies(node, reference, kinds, wanted)]
            if any(id(node_scope(node)) in data_scopes for node in strict):
                # CASE/FILTER and an outer join's ON constrain a component or a
                # matched side, not necessarily the returned population.
                conditional = [node for node in strict if node not in where_nodes]
                if conditional:
                    pending('filter_component_scope_not_proven', '条件出现在指标分量或连接中，需核对实际约束范围，不能自动当成总体筛选', field=reference)
                global_nodes = [node for node in where_nodes if id(node_scope(node)) in data_scopes and reference in refs(node)]
                if global_nodes and not any(implies(node, reference, kinds, wanted) for node in global_nodes):
                    pending('filter_population_not_proven', '总体筛选与分量条件并存，需核对是否筛掉了声明指标的数据', field=reference)
                return True
            relaxed = strict or [node for node in predicate_scopes
                                 if implies(node, reference, kinds, wanted, allow_transformed=True)]
            if relaxed:
                if not strict:
                    pending('filter_expression_not_proven', '筛选字段经过转换或计算，不能直接认定与原字段条件等价', field=reference)
                if not any(id(node_scope(node)) in data_scopes for node in relaxed):
                    pending('filter_scope_not_proven', '筛选仅见于独立子查询，未证明约束了当前指标的数据集合', field=reference)
                return True
            return False

        def direct_boundary_conflict(reference: str, kind: type[exp.Expression], value: Any) -> bool:
            """A clear bound cannot be obscured by an unrelated complex JOIN.

            Only required AND predicates with one unique literal bound are
            decisive here. OR branches and conflicting per-period branches
            remain unproven rather than being flattened into one range.
            """
            direction = {exp.GT, exp.GTE} if kind in {exp.GT, exp.GTE} else {exp.LT, exp.LTE}

            def atoms(node):
                if isinstance(node, (exp.Where, exp.Having, exp.Paren)):
                    return atoms(node.this)
                if isinstance(node, exp.And):
                    return [*atoms(node.this), *atoms(node.expression)]
                if type(node) not in direction:
                    return []
                field_node, actual = node.this, literal_value(node.expression)
                actual_kind = type(node)
                if reference not in refs(field_node) and reference in refs(node.expression):
                    field_node, actual = node.expression, literal_value(node.this)
                    actual_kind = reverse.get(actual_kind, actual_kind)
                if (actual_kind not in direction or actual is None or not identity_column(field_node)
                        or reference not in refs(field_node)):
                    return []
                return [(actual_kind, normalized_literal(reference, actual))]

            actual = {bound for node in where_nodes if id(node_scope(node)) in data_scopes for bound in atoms(node)}
            return len(actual) == 1 and (kind, normalized_literal(reference, value)) not in actual

        def predicate_is_unproven(reference: str, kind: type[exp.Expression], values: list[Any]) -> bool:
            """A limited matcher failing is not evidence that SQL is wrong.

            Keep direct, inspectable mismatches hard. Composite/derived forms
            remain executable with an explicit unresolved check.
            """
            wanted = tuple(normalized_literal(reference, value) for value in values)
            if global_value_conflict(reference, (kind,), wanted):
                return False
            if kind in {exp.GT, exp.GTE, exp.LT, exp.LTE} and len(values) == 1:
                if direct_boundary_conflict(reference, kind, values[0]):
                    return False
            if kind == exp.Between and len(values) == 2:
                if (direct_boundary_conflict(reference, exp.GTE, values[0])
                        or direct_boundary_conflict(reference, exp.LTE, values[1])):
                    return False
            atoms = [node for predicate in predicate_scopes for node in predicate.walk()
                     if isinstance(node, tuple(operators.values())) and reference in refs(node)]
            if not atoms:
                return any(reference in refs(node) for node in predicate_scopes)
            # A recognized target in one OR/NOT branch was not sufficient to
            # imply the condition: this is a concrete Boolean scope mismatch.
            def simple_boolean(node: exp.Expression) -> bool:
                if isinstance(node, (exp.Where, exp.Having, exp.Paren, exp.Not)):
                    return simple_boolean(node.this)
                if isinstance(node, (exp.And, exp.Or)):
                    return simple_boolean(node.this) and simple_boolean(node.expression)
                if isinstance(node, exp.Boolean):
                    return True
                if isinstance(node, (exp.EQ, exp.NEQ, exp.In)):
                    return (reference in refs(node.this) and identity_column(node.this)
                            and (all(literal_value(part) is not None for part in node.expressions)
                                 if isinstance(node, exp.In) else literal_value(node.expression) is not None))
                return False
            if any(simple_boolean(node) and any(implies(atom, reference, (kind,), wanted)
                    for atom in atoms if atom in node.walk()) for node in predicate_scopes):
                return False
            atoms = list({node.sql(dialect='duckdb'): node for node in atoms}.values())
            if len(atoms) != 1:
                return True
            node = atoms[0]
            field_node = node.this
            actual_kind = type(node)
            if isinstance(node, exp.Between):
                actual = [literal_value(node.args.get('low')), literal_value(node.args.get('high'))]
            elif isinstance(node, exp.In):
                actual = [literal_value(part) for part in node.expressions]
            else:
                value = literal_value(node.expression)
                if reference not in refs(field_node) and reference in refs(node.expression):
                    field_node, value = node.expression, literal_value(node.this)
                    actual_kind = reverse.get(actual_kind, actual_kind)
                actual = [value]
            if not identity_column(field_node) or not actual or any(value is None for value in actual):
                return True
            actual_values = tuple(normalized_literal(reference, value) for value in actual)
            ordered = {exp.GT, exp.GTE, exp.LT, exp.LTE}
            explicit_mismatch = (actual_kind == kind
                or (actual_values == wanted and actual_kind in ordered and kind in ordered)
                or (actual_values == wanted and {actual_kind, kind} == {exp.EQ, exp.NEQ}))
            return not explicit_mismatch
        for reference in [*contract.dimensions, *(field for metric in contract.metrics for field in metric.source_fields)]:
            if reference in literal_dimensions:
                continue
            if reference not in sql_columns and not any(reference in metric.source_fields and count_star_binding(metric, statement) for metric in contract.metrics):
                raise ValueError(f"query_contract字段没有出现在SQL中：{reference}")
        for item in contract.filters:
            values = item.value if isinstance(item.value, list) else [item.value]
            if not has_predicate(item.field, (operators[item.operator],), values):
                if predicate_is_unproven(item.field, operators[item.operator], values):
                    pending('filter_equivalence_not_proven', '筛选使用了组合或计算表达式，有限匹配器无法证明与声明等价，需结合SQL和结果核对', field=item.field)
                else:
                    raise ValueError(f"SQL筛选字段、运算符或取值与query_contract不一致：{item.field}")
        declared_predicates = {
            reference
            for reference in [*(item.field for item in contract.filters),
                              *(time_range.field for time_range in time_ranges)]
        }
        sql_predicates = set()
        for predicate_scope in filter_scopes:
            for node in predicate_scope.walk():
                if isinstance(node, tuple(operators.values())) and (node.find(exp.Literal) or node.find(exp.Boolean)):
                    sql_predicates.update(refs(node))
        undeclared = sql_predicates - declared_predicates
        if undeclared:
            pending('undeclared_sql_predicate', 'SQL存在未在说明中重复声明的筛选；需核对是否改变任务范围', fields=sorted(undeclared))
        for time_range in time_ranges:
            field = time_range.field
            range_path = ('time_range' if time_range is contract.time_range else
                          f'time_ranges.{next(index for index, item in enumerate(contract.time_ranges) if item is time_range)}')
            field_type = str(known_fields[field].get('sql_type') or known_fields[field].get('source_type') or '').upper()
            is_date_field = field_type == 'DATE'

            def calendar_day(value: str | None) -> date | None:
                if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    try:
                        return date.fromisoformat(value)
                    except ValueError:
                        pass
                return None

            original_end = time_range.end
            end_day = calendar_day(original_end)
            if original_end and time_range.end_inclusive is None:
                # Legacy model/tool outputs used an inclusive last date while
                # SQL already used the correct next-day exclusive boundary.
                # Infer only from actual SQL, and keep the ambiguity visible.
                next_day = (end_day + timedelta(days=1)).isoformat() if end_day else None
                if next_day and (has_predicate(field, (exp.LT,), [next_day]) or
                        (is_date_field and has_predicate(field, (exp.LTE,), [original_end]))):
                    normalize(range_path + '.end', original_end, next_day,
                              '旧日期终点未声明包含性；实际SQL使用次日排他边界或DATE含末日，统一为排他终点')
                    time_range.end = next_day
                normalize(range_path + '.end_inclusive', None, False,
                          '旧格式兼容，记录边界含义待核对，不认证用户范围')
                time_range.end_inclusive = False
                pending('time_boundary_semantics_unspecified', '旧时间声明未标明终点包含性，已按实际SQL规范；需结合原问题核对是否覆盖完整期间',
                        field=field, declared_end=original_end, normalized_end=time_range.end)
            elif original_end and time_range.end_inclusive and end_day:
                next_day = (end_day + timedelta(days=1)).isoformat()
                normalize(range_path + '.end', original_end, next_day, '包含末日全天的日期终点转为次日排他边界')
                normalize(range_path + '.end_inclusive', True, False, '日期范围统一为左闭右开')
                time_range.end = next_day
                time_range.end_inclusive = False

            if (time_range.start and time_range.end
                    and (normalized_literal(field, time_range.start) > normalized_literal(field, time_range.end)
                         or (not time_range.end_inclusive and normalized_literal(field, time_range.start)
                             == normalized_literal(field, time_range.end)))):
                raise ValueError('规范化后的时间范围为空或起点晚于终点')

            profile = known_fields[field].get('data_profile') or {}
            observed_end = calendar_day(profile.get('max'))
            requested_end = calendar_day(time_range.end)
            if is_date_field and observed_end and requested_end and (
                    requested_end > observed_end + timedelta(days=0 if time_range.end_inclusive else 1)):
                pending('time_coverage_not_proven', '查询期间超过登记的最晚观测日期，后续日期不能自动当零；交付需核对并说明实际可见范围',
                        field=field, last_observed_date=observed_end.isoformat(), requested_end=time_range.end,
                        end_inclusive=time_range.end_inclusive, metadata_source='registered_schema.data_profile')

            complex_range = any(
                isinstance(node, tuple(operators.values())) and field in refs(node)
                and (any(field in refs(operand) and not isinstance(operand, exp.Column)
                         for operand in node.iter_expressions())
                     or (not isinstance(node, (exp.In, exp.Between))
                         and literal_value(node.expression if field in refs(node.this) else node.this) is None))
                for predicate in predicate_scopes for node in predicate.walk())
            # Date casts/functions can express a valid range differently. Do not
            # pretend to prove equivalence, or demand one rigid SQL spelling.
            missing = []
            if time_range.start and not has_predicate(
                field, (exp.GTE,), [time_range.start]
            ):
                missing.append(f'时间起点：{time_range.start}')
            if time_range.end:
                end_kind = exp.LTE if time_range.end_inclusive else exp.LT
                matched_end = has_predicate(field, (end_kind,), [time_range.end])
                canonical_end_day = calendar_day(time_range.end)
                if not matched_end and not time_range.end_inclusive and is_date_field and canonical_end_day:
                    # DATE <= last_day equals DATE < next_day. This is not true
                    # for a timestamp <= midnight on last_day.
                    previous_day = (canonical_end_day - timedelta(days=1)).isoformat()
                    matched_end = has_predicate(field, (exp.LTE,), [previous_day])
                if not matched_end:
                    missing.append(f'时间终点：{time_range.end}')
            if missing:
                explicit_conflict = ((time_range.start and direct_boundary_conflict(field, exp.GTE, time_range.start))
                    or (time_range.end and direct_boundary_conflict(field, exp.LTE if time_range.end_inclusive else exp.LT,
                                                                   time_range.end)))
                if complex_range and not explicit_conflict:
                    pending('time_range_expression_not_proven', '时间条件使用转换或函数，需核对与声明边界等价性', field=field, missing=missing)
                else:
                    raise ValueError('SQL缺少声明的' + '；'.join(missing))
        aggregate_types: dict[str, type[exp.Expression]] = {
            "sum": exp.Sum, "avg": exp.Avg, "count": exp.Count,
            "count_distinct": exp.Count, "min": exp.Min, "max": exp.Max,
        }
        for metric in contract.metrics:
            if metric.aggregation == 'none' and any(set(metric.source_fields) & expanded_refs(refs(aggregate))
                    for aggregate in statement.find_all(exp.AggFunc)):
                raise ValueError(f"SQL已聚合{metric.name}，contract不能声明none，应与实际聚合一致")
            if metric.aggregation in aggregate_types:
                if metric.aggregation == 'count_distinct':
                    if not any(set(metric.source_fields) & expanded_refs(refs(count)) and count.find(exp.Distinct)
                               for count in statement.find_all(exp.Count)):
                        pending('distinct_count_not_proven', '未见直接去重计数表达式；可能已在上游去重，需核对实际粒度', metric=metric.name)
                    continue
                aggregate_type = aggregate_types[metric.aggregation]
                measures = monetary_sources.get(metric.name, set(metric.source_fields))
                if not any(measures & expanded_refs(value_refs(aggregate))
                           for aggregate in statement.find_all(aggregate_type)) and not count_star_binding(metric, statement):
                    actual = [aggregate for aggregate in statement.find_all(exp.AggFunc)
                              if measures & expanded_refs(value_refs(aggregate))]
                    if actual:
                        composed = any(
                            projection.find(exp.Add, exp.Sub, exp.Mul, exp.Div)
                            and len([node for node in projection.find_all(exp.AggFunc)
                                     if measures & expanded_refs(value_refs(node))]) > 1
                            for projection in statement.selects)
                        if composed:
                            pending('aggregation_expression_not_proven', '多个聚合经表达式组合，需核对是否与声明计量等价', metric=metric.name)
                            continue
                        raise ValueError(f"SQL没有按声明方式聚合指标：{metric.name}")
                    if metric.name in monetary_sources and any(set(metric.source_fields) & refs(aggregate)
                            for aggregate in statement.find_all(aggregate_type)):
                        raise ValueError(f'指标金额没有进入实际聚合值：{metric.name}；筛选条件中出现金额不代表正在加总金额')
                    pending('aggregation_expression_not_proven', '未直接解析出声明聚合，需核对是否使用等价的上游表达式', metric=metric.name)

        error = self.engine.preflight(database, sql, scope)
        if error:
            raise ValueError(f"SQL执行前编译未通过：{error}")
        return {
            **contract.model_dump(mode="json"),
            "validation": {
                "schema_checked": True,
                "sql_compiled": True,
                "business_definition_verified": False,
                "metric_output_bindings": metric_bindings,
                "confirmed_metric_sources": confirmed_sources,
                "normalizations": normalizations,
                "pending_checks": pending_checks,
                "sql_summary": {
                    "normalized_sql": sql,
                    "tables": sorted(sql_tables), "source_fields": sorted(sql_columns),
                    "output_columns": [projection.alias_or_name for projection in statement.selects],
                    "derived_dimensions": [item.model_dump(mode='json') for item in contract.derived_dimensions],
                    "predicates": [{'sql': node.sql(dialect='duckdb'), 'fields': sorted(refs(node))}
                                   for node in filter_scopes],
                    "aggregations": [{'operation': node.key, 'sql': node.sql(dialect='duckdb'), 'fields': sorted(refs(node))}
                                     for node in statement.find_all(exp.AggFunc)],
                },
            },
        }
