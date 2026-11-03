"""Shared SQL-facing context and mechanical contract completion.

Only authorized, retrieved schema participates. We do not infer business
definitions, invent columns, replace declared conflicting conditions, or execute
SQL here. The completed declaration still goes through the existing validator.
"""
from copy import deepcopy
from decimal import Decimal
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from ..security import AccessScope


def sql_facing_graph(graph: dict) -> dict:
    names = {item['id']: item['name'] for item in graph.get('tables', [])}
    tables = [{key: item[key] for key in ('name', 'label', 'description', 'grain', 'primary_key', 'domain',
                                        'coverage', 'snapshot_id', 'version', 'metadata_version', 'schema_version')
               if key in item} for item in graph.get('tables', [])]
    fields = [{key: item[key] for key in ('sql_name', 'label', 'description', 'role', 'aggregation',
                                        'sql_type', 'type', 'unit', 'nullable', 'enum_values', 'enum_descriptions', 'enum_metadata_source', 'enum_metadata_conflict',
                                        'aliases', 'samples', 'profile', 'data_profile', 'coverage',
                                        'null_semantics', 'snapshot_id', 'version', 'metadata_version',
                                        'schema_version') if key in item}
              for item in graph.get('fields', [])]
    joins = []
    for item in graph.get('joins', []):
        left = names.get(item.get('left_table'), item.get('left_table_name'))
        right = names.get(item.get('right_table'), item.get('right_table_name'))
        if left and right:
            joins.append({'left': f'{left}.{item["left_field"]}', 'right': f'{right}.{item["right_field"]}',
                          **{key: item[key] for key in ('relation_type', 'description', 'cardinality',
                              'cardinality_basis', 'duplicate_risk') if key in item}})
    return {'dialect': 'DuckDB', 'table_name_rule': '使用name和sql_name，不添加数据库前缀；内部索引ID不属于SQL名称',
            'tables': tables, 'fields': fields, 'joins': joins}


def model_contract_schema() -> dict:
    # Derive both generation and repair formats from the actual backend model.
    from .query_contract import QueryContract
    schema = deepcopy(QueryContract.model_json_schema())
    # This is a server observation of SQL, not another form for the model to
    # hand-write. The internal contract still retains it for evidence/review.
    schema.get('properties', {}).pop('derived_dimensions', None)
    schema['required'] = ['metrics']
    schema['description'] = ('模型声明业务指标；其他机械字段可省略，由服务端从SQL和授权上下文补齐。'
                             '若主动填写，必须与SQL一致，不会覆盖明确冲突。')
    return schema


class _SqlLineage:
    """Resolve actual outputs to all known physical origins, without guessing.

    A composite expression is different from an ambiguous bare identifier.
    COALESCE(a.month,b.month) names two inputs explicitly; an unqualified month
    in a join with two such inputs is ambiguous and remains unresolved.
    """

    def __init__(self, scopes: list[Scope], field_specs: dict[str, dict]) -> None:
        self.scopes = scopes
        self.field_specs = field_specs
        self.field_names = {name.casefold(): name for name in field_specs}
        self.scope_by_expression = {id(scope.expression): scope for scope in scopes}

    @staticmethod
    def empty() -> dict:
        return {'fields': set(), 'unresolved': set(), 'aggregated': False, 'union': False, 'expressions': []}

    @staticmethod
    def combine(parts: list[dict]) -> dict:
        return {'fields': set().union(*(part['fields'] for part in parts)),
                'unresolved': set().union(*(part['unresolved'] for part in parts)),
                'aggregated': any(part['aggregated'] for part in parts),
                'union': any(part['union'] for part in parts),
                'expressions': list(dict.fromkeys(expression for part in parts
                                                for expression in part['expressions']))}

    def owner(self, node: exp.Expression, fallback: Scope) -> Scope:
        current = node
        while current is not None:
            if id(current) in self.scope_by_expression:
                return self.scope_by_expression[id(current)]
            current = current.parent
        return fallback

    @staticmethod
    def sources(node: exp.Column, scope: Scope):
        matches = [(alias, source) for alias, (_, source) in scope.selected_sources.items()
                   if not node.table or alias.casefold() == node.table.casefold()]
        # Qualified correlation uses the nearest visible alias. Do not search
        # sibling CTEs or jump past a locally shadowing alias.
        while not matches and node.table and scope.parent and scope.can_be_correlated:
            scope = scope.parent
            matches = [(alias, source) for alias, (_, source) in scope.selected_sources.items()
                       if alias.casefold() == node.table.casefold()]
        return matches

    def output(self, scope: Scope, name: str, seen: set | None = None) -> tuple[dict, str] | None:
        projections = scope.expression.selects
        names = scope.outer_columns or [projection.alias_or_name for projection in projections]
        positions = [index for index, candidate in enumerate(names)
                     if candidate.casefold() == name.casefold()]
        if len(positions) != 1:
            return None
        index = positions[0]
        if scope.union_scopes:
            parts, expressions = [], []
            for branch in scope.union_scopes:
                if index >= len(branch.expression.selects):
                    return None
                projection = branch.expression.selects[index]
                if branch.union_scopes:
                    branch_output = self.output(branch, projection.alias_or_name, seen)
                    if branch_output is None:
                        return None
                    parts.append(branch_output[0])
                else:
                    parts.append(self.resolve(projection, branch, seen))
                expressions.append(projection.sql(dialect='duckdb'))
            result = self.combine(parts)
            result['union'] = True
            result['expressions'] = list(dict.fromkeys([*expressions, *result['expressions']]))
            return result, f'UNION OUTPUT({names[index]})'
        projection = projections[index]
        return self.resolve(projection, scope, seen), projection.sql(dialect='duckdb')

    def resolve(self, node: exp.Expression | None, scope: Scope, seen: set | None = None) -> dict:
        if node is None:
            return self.empty()
        seen = set(seen or ())
        if isinstance(node, exp.Column):
            scope = self.owner(node, scope)
            marker = (id(scope), node.table.casefold(), node.name.casefold())
            if marker in seen:
                result = self.empty()
                result['unresolved'].add(node.sql(dialect='duckdb'))
                return result
            seen.add(marker)
            if scope.union_scopes:
                output = self.output(scope, node.name, seen)
                if output is not None:
                    return output[0]
            sources = self.sources(node, scope)
            matches = []
            for _, source in sources:
                if isinstance(source, exp.Table):
                    reference = self.field_names.get(f'{source.name}.{node.name}'.casefold())
                    if reference:
                        result = self.empty()
                        result['fields'].add(reference)
                        matches.append(result)
                elif isinstance(source, Scope):
                    output = self.output(source, node.name, seen)
                    if output is not None:
                        matches.append(output[0])
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                result = self.combine(matches)
                result['unresolved'].add('ambiguous:' + node.sql(dialect='duckdb'))
                return result
            # GROUP BY aliases live in the same select, unlike input columns.
            if not node.table:
                projections = [projection for projection in scope.expression.selects
                               if isinstance(projection, exp.Alias)
                               and projection.alias.casefold() == node.name.casefold()]
                if len(projections) == 1:
                    return self.resolve(projections[0].this, scope, seen)
            result = self.empty()
            result['unresolved'].add(node.sql(dialect='duckdb'))
            return result
        result = self.combine([self.resolve(child, self.owner(child, scope), seen)
                               for child in node.iter_expressions()])
        if isinstance(node, (exp.AggFunc, exp.Window)):
            result['aggregated'] = True
        if isinstance(node, exp.Alias):
            expression = node.this.sql(dialect='duckdb')
            result['expressions'] = list(dict.fromkeys([expression, *result['expressions']]))
        return result


def canonical_sql(sql: str, database: str, graph: dict, access_scope: dict) -> tuple[str, list[dict]]:
    """Map only exact authorized metadata IDs, never strip arbitrary prefixes."""
    scope = AccessScope.from_dict(access_scope)
    aliases = {str(t['id']): str(t['name']) for t in graph.get('tables', [])
               if t.get('database') == database and scope.allows_table(database, t['id'])}
    try:
        statements = [s for s in sqlglot.parse(sql, read='duckdb') if s]
    except sqlglot.errors.ParseError:
        return sql, []  # The existing validator owns the syntax diagnostic.
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return sql, []
    changes = []
    statement = statements[0]
    for query_scope in traverse_scope(statement):
        for source in query_scope.sources.values():
            if not isinstance(source, exp.Table):
                continue
            reference = '.'.join(part.name for part in source.parts)
            physical = aliases.get(reference)
            if physical and reference != physical:
                old_name = source.name
                if not source.alias and old_name != physical:
                    source.set('alias', exp.TableAlias(this=exp.to_identifier(old_name)))
                source.set('this', exp.to_identifier(physical))
                source.set('db', None)
                source.set('catalog', None)
                changes.append({'path': 'sql.table', 'before': reference, 'after': physical,
                                'reason': '精确映射当前授权Schema内部ID，不改变查询范围'})
    return (statement.sql(dialect='duckdb') if changes else sql), changes


def complete_contract(raw: Any, sql: str, question: str, database: str, graph: dict) -> tuple[Any, list[dict]]:
    if not isinstance(raw, dict):
        return raw, []
    result, changes = deepcopy(raw), []
    tables = {t['name']: t for t in graph.get('tables', [])}
    identifiers = {t['id']: t['name'] for t in tables.values()}
    field_specs = {f['sql_name']: f for f in graph.get('fields', []) if f.get('sql_name')}
    field_names = {name.casefold(): name for name in field_specs}
    try:
        statements = [s for s in sqlglot.parse(sql, read='duckdb') if s]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            return result, []
        scopes = traverse_scope(statements[0])
    except (sqlglot.errors.ParseError, ValueError):
        return result, []

    def put(key, value, reason):
        before = result.get(key)
        if key not in result or before != value:
            changes.append({'path': key, 'before': before, 'after': value, 'reason': reason})
            result[key] = value

    lineage = _SqlLineage(scopes, field_specs)

    def physical_refs(node, query_scope, seen=None):
        observed = lineage.resolve(node, query_scope, seen)
        return observed['fields'] if not observed['unresolved'] else set()

    physical_names = sorted({source.name for s in scopes for source in s.sources.values()
                             if isinstance(source, exp.Table)})
    if 'tables' not in result:
        put('tables', physical_names, '从SQL实际来源提取')
    elif isinstance(result['tables'], list):
        put('tables', [identifiers.get(v, v) if isinstance(v, str) else v for v in result['tables']],
            '当前Schema内部ID转物理名称')
    if 'original_question' not in result:
        put('original_question', question, '服务端绑定原问题')
    if 'data_source' not in result:
        put('data_source', database, '服务端绑定当前授权数据源')
    for key in ('assumptions', 'open_questions'):
        if key not in result:
            put(key, [], '省略的说明项为空，不生成业务假设')

    group_fields = set()
    grouped_outputs = set()
    for s in scopes:
        group = s.expression.args.get('group')
        if group:
            for item in group.expressions:
                if isinstance(item, exp.Literal) and item.is_int:
                    index = int(item.this) - 1
                    item = s.expression.selects[index] if 0 <= index < len(s.expression.selects) else item
                group_fields.update(physical_refs(item, s))
                if s is scopes[-1]:
                    grouped_outputs.add(item.alias_or_name.casefold())
    root_scope = scopes[-1] if scopes else None
    output_dimensions = {}
    if root_scope:
        has_groups = any(s.expression.args.get('group') for s in scopes)
        for projection in root_scope.expression.selects:
            name = projection.alias_or_name
            observed = lineage.output(root_scope, name)
            if observed is None:
                continue  # Duplicate output names are ambiguous.
            info, expression = observed
            if info['unresolved'] or info['aggregated']:
                continue
            if ((has_groups and (info['fields'] & group_fields or name.casefold() in grouped_outputs))
                    or info['union']):
                output_dimensions[name.casefold()] = (name, info, expression)

    derived_dimensions = []

    def dimension_name(name):
        if not isinstance(name, str):
            return [name]
        observed = output_dimensions.get(name.casefold())
        if observed is None and root_scope and '.' not in name:
            candidate = lineage.output(root_scope, name)
            if candidate is not None and not candidate[0]['unresolved'] and not candidate[0]['aggregated']:
                observed = (name, *candidate)
        if observed is None:
            return [field_name(name)]
        output_name, info, expression = observed
        sources = sorted(info['fields'])
        roles = {field_specs[source].get('role') for source in sources}
        derived = {'output_name': output_name, 'expression': expression,
                   'source_fields': sources, 'source_expressions': info['expressions'],
                   'kind': 'union' if info['union'] else 'expression' if sources else 'literal',
                   'role': 'time' if roles == {'time'} else 'dimension'}
        if derived not in derived_dimensions:
            derived_dimensions.append(derived)
        # Physical origins remain available to old contract consumers. A
        # source-less SQL label is represented by its observed output instead.
        return sources or [output_name]

    def field_name(name):
        if not isinstance(name, str):
            return name
        for internal, physical in identifiers.items():
            if name.startswith(internal + '.'):
                candidate = physical + name[len(internal):]
                if candidate in field_specs:
                    return candidate
        if name.casefold() in field_names:
            return field_names[name.casefold()]
        if root_scope:
            parts = name.split('.')
            if len(parts) == 1:
                projections = [projection for projection in root_scope.expression.selects
                               if projection.alias_or_name.casefold() == name.casefold()]
                if projections:
                    # A contract can name a SELECT output. Resolve its actual
                    # expression before searching every JOIN input with that
                    # column name: pm.period is not sm.period just because
                    # both CTEs expose a period column. Multiple physical
                    # origins are represented separately for dimensions;
                    # scalar metric/filter declarations still require one.
                    if len(projections) == 1:
                        observed = lineage.output(root_scope, name)
                        if observed and not observed[0]['unresolved'] and len(observed[0]['fields']) == 1:
                            return next(iter(observed[0]['fields']))
                    return name
            column = exp.column(parts[1], table=parts[0]) if len(parts) == 2 else exp.column(name)
            candidates = physical_refs(column, root_scope)
            if len(candidates) == 1:
                return next(iter(candidates))
        return name  # Ambiguous or unknown is never guessed.

    if 'dimensions' not in result:
        dimensions = [field for name, _, _ in output_dimensions.values() for field in dimension_name(name)]
        put('dimensions', list(dict.fromkeys(dimensions)), '从实际返回分组表达式提取全部真实来源')
    elif isinstance(result['dimensions'], list):
        dimensions = [field for name in result['dimensions'] for field in dimension_name(name)]
        put('dimensions', list(dict.fromkeys(dimensions)), '从明确输出表达式解析一个或多个真实来源')
        # A completed contract is idempotent: recover observed expressions for
        # already flattened physical origins, without accepting caller metadata.
        for name, info, _ in output_dimensions.values():
            if info['fields'] and info['fields'] <= set(result['dimensions']):
                dimension_name(name)
    if 'derived_dimensions' in raw and raw['derived_dimensions'] != derived_dimensions:
        raise ValueError('query_contract.derived_dimensions与实际SQL输出表达式或物理来源不一致')
    put('derived_dimensions', derived_dimensions, '记录实际SQL派生维度及所有物理来源，不认证业务含义')
    if 'grain' not in result:
        put('grain', 'dimension' if result.get('dimensions') else 'overall', '只补结构粒度，不解释业务含义')

    def literal(node):
        from .sql_literals import constant_value
        return constant_value(node)

    def conjuncts(node):
        if isinstance(node, (exp.Where, exp.Paren)):
            return conjuncts(node.this)
        if isinstance(node, exp.And):
            return [*conjuncts(node.this), *conjuncts(node.expression)]
        return [node]

    filters, ranges = [], []
    operators = {exp.EQ: '=', exp.NEQ: '!=', exp.GTE: '>=', exp.GT: '>', exp.LTE: '<=', exp.LT: '<'}
    for s in scopes:
        where = s.expression.args.get('where')
        if not where:
            continue
        bounds = {}
        for predicate in conjuncts(where):
            operation = operators.get(type(predicate))
            if not operation:
                continue  # OR, dynamic expressions and opaque predicates stay explicit.
            refs, value = physical_refs(predicate.this, s), literal(predicate.expression)
            if len(refs) != 1 or value is None:
                continue
            reference = next(iter(refs))
            filters.append({'field': reference, 'operator': operation, 'value': value})
            spec = field_specs[reference]
            is_time = spec.get('role') == 'time' or re.search(r'DATE|TIME', str(spec.get('sql_type') or spec.get('type') or ''), re.I)
            if is_time and isinstance(value, str) and re.match(r'^\d{4}-\d{2}-\d{2}', value):
                item = bounds.setdefault(reference, {'field': reference, 'start': None, 'end': None})
                if operation == '>=':
                    item['start'] = value
                elif operation in ('<', '<='):
                    item.update(end=value, end_inclusive=operation == '<=')
        ranges.extend(bounds.values())
    if 'filters' not in result:
        put('filters', filters, '从SQL可明确解析的AND筛选提取，复杂条件仍由原校验器核对')
    elif isinstance(result['filters'], list):
        put('filters', [{**v, 'field': field_name(v.get('field'))} if isinstance(v, dict) else v
                        for v in result['filters']], '按唯一物理来源补全字段')
    if isinstance(result.get('metrics'), list):
        put('metrics', [{**m, 'source_fields': [field_name(v) for v in m['source_fields']]}
                        if isinstance(m, dict) and isinstance(m.get('source_fields'), list) else m
                        for m in result['metrics']], '按唯一物理来源规范指标字段')
    if not result.get('time_range') and not result.get('time_ranges') and ranges:
        put('time_range', ranges[0], '直接使用SQL中已存在的明确时间边界')
        if len(ranges) > 1:
            put('time_ranges', ranges[1:], '保留不同事件／子查询的时间范围')
    elif 'time_range' not in result:
        put('time_range', None, 'SQL无可确定时间范围时不猜日期')
    additional_ranges = result.get('time_ranges') if isinstance(result.get('time_ranges'), list) else []
    for item in ([result['time_range']] if result.get('time_range') else []) + additional_ranges:
        if not isinstance(item, dict):
            continue
        before = deepcopy(item)
        if item.get('inclusive_start') is True:
            item.pop('inclusive_start')
        if 'inclusive_end' in item and 'end_inclusive' not in item:
            item['end_inclusive'] = item.pop('inclusive_end')
        elif 'inclusive_end' in item and item.get('inclusive_end') == item.get('end_inclusive'):
            item.pop('inclusive_end')
        if 'field' not in item:
            choices = {v['field'] for v in ranges}
            if len(choices) == 1:
                item['field'] = next(iter(choices))
        elif item.get('field'):
            item['field'] = field_name(item['field'])
        if before != item:
            changes.append({'path': 'time_range', 'before': before, 'after': deepcopy(item),
                            'reason': '补唯一时间字段及等价边界键，不改变明确日期／包含性'})
    return result, changes
