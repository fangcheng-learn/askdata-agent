"""Check confirmed entity filters independently of model-authored declarations.

Only a restricted, provable predicate subset is certified. Complex SQL retains
pending checks for delivery review, never a fabricated proof of business scope.
"""
from __future__ import annotations

from typing import Any
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from .query_protocol import _SqlLineage


def _values(raw: Any) -> set[str] | None:
    if isinstance(raw, dict):
        if raw.get('operator', '=') not in ('=', 'eq', 'in'):
            return None
        raw = raw.get('values', raw.get('value'))
    values = raw if isinstance(raw, list) else [raw]
    if not values or any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in values):
        return None
    return {str(v) for v in values}


def validate_request_constraints(*, sql: str, query_contract: dict,
        request: dict | None = None, entity_resolutions: list | None = None,
        schema_graph: dict | None = None, query_id: str | None = None) -> dict:
    constraints = {r['canonical_field']: _values(r.get('canonical_value'))
        for r in entity_resolutions or [] if r.get('kind') == 'entity' and r.get('status') == 'resolved'
        and (not query_id or r.get('query_id') == query_id)}
    constraints = {field: values for field, values in constraints.items() if values is not None}
    result = {'errors': [], 'checks': [], 'constraints': {k: sorted(v) for k, v in constraints.items()},
              'passed': True, 'complete': True}
    if not constraints:
        return result
    graph = schema_graph or {}
    names = {t['id']: t['name'] for t in graph.get('tables') or []}
    fields = {f"{names[f['table_id']]}.{f['name']}": f for f in graph.get('fields') or []
              if f.get('table_id') in names}
    scopes = traverse_scope(sqlglot.parse_one(sql, read='duckdb'))
    lineage = _SqlLineage(scopes, fields)

    def literal(node):
        return str(node.this) if isinstance(node, exp.Literal) else None

    def restriction(node, scope, field):
        if node is None:
            return None
        if isinstance(node, (exp.Paren, exp.Where)):
            return restriction(node.this, scope, field)
        if isinstance(node, (exp.And, exp.Or)):
            left, right = restriction(node.this, scope, field), restriction(node.expression, scope, field)
            if isinstance(node, exp.Or):
                return left | right if left is not None and right is not None else None
            return right if left is None else left if right is None else left & right
        if isinstance(node, (exp.EQ, exp.In)) and isinstance(node.this, exp.Column):
            observed = lineage.resolve(node.this, scope)
            if observed['fields'] != {field} or observed['unresolved']:
                return None
            values = [literal(v) for v in node.expressions] if isinstance(node, exp.In) else [literal(node.expression)]
            if values and all(v is not None for v in values):
                return set(values)
        return None

    def prove(scope, field, expected, seen):
        if id(scope) in seen:
            return 'pending'
        seen = {*seen, id(scope)}
        if scope.union_scopes:
            states = [prove(branch, field, expected, seen) for branch in scope.union_scopes]
            if all(s == 'confirmed' for s in states):
                return 'confirmed'
            return 'conflict' if any(s in {'conflict', 'missing'} for s in states) else 'pending'
        values = restriction(scope.expression.args.get('where'), scope, field)
        if values is not None:
            if values == expected:
                return 'confirmed'
            return 'conflict'
        # A selected CTE preserves its own filter. Multi-source joins or
        # outer joins need cohort-aware review rather than a guessed proof.
        sources = list(scope.selected_sources.values())
        where = scope.expression.args.get('where')
        if where and any(field in lineage.resolve(column, scope)['fields'] for column in where.find_all(exp.Column)):
            return 'pending'
        if len(sources) == 1:
            source = sources[0][1]
            if isinstance(source, Scope):
                return prove(source, field, expected, seen)
            if isinstance(source, exp.Table) and field.startswith(source.name + '.'):
                return 'missing'
        return 'pending'

    root = scopes[-1]

    def aggregate_scopes(node, scope, seen=None):
        """Follow output aliases to their aggregation, not unrelated siblings.

        This is deliberately not a universal cohort prover. Ambiguous outputs,
        unions and unusual expressions remain pending in the existing reviewer.
        """
        seen = set(seen or ())
        key=(id(node),id(scope))
        if key in seen:
            return []
        seen.add(key)
        found=[scope] if node.find(exp.AggFunc) is not None else []
        for column in node.find_all(exp.Column):
            sources=scope.selected_sources
            choices=([sources[column.table][1]] if column.table in sources else
                     [next(iter(sources.values()))[1]] if not column.table and len(sources)==1 else [])
            for child in choices:
                if isinstance(child,Scope) and isinstance(child.expression,exp.Select):
                    projections=[p for p in child.expression.expressions if p.alias_or_name==column.name]
                    if len(projections)==1:
                        found.extend(aggregate_scopes(projections[0],child,seen))
        return found

    def inputs(scope, seen=None):
        seen=set(seen or ())
        if id(scope) in seen:
            return []
        seen.add(id(scope));out=[scope]
        for _,child in scope.selected_sources.values():
            if isinstance(child,Scope):out.extend(inputs(child,seen))
        for child in scope.subquery_scopes:
            out.extend(inputs(child,seen))
        return out

    def mentions_filter(scope,field):
        # A possible condition in WHERE, JOIN or CASE keeps review pending;
        # do not reject a conditional aggregate simply for lacking WHERE.
        for predicate in scope.expression.find_all(exp.Predicate):
            owner=predicate.find_ancestor(exp.Select)
            if owner is not scope.expression:
                continue
            if any(field in lineage.resolve(c,scope)['fields'] for c in predicate.find_all(exp.Column)):
                return True
        return False

    bindings={column for metric in query_contract.get('metrics') or []
              for column in metric.get('output_columns') or []}
    def bound_projections(scope,indices):
        if scope.union_scopes:
            return [entry for branch in scope.union_scopes for entry in bound_projections(branch,indices)]
        if isinstance(scope.expression,exp.Select):
            return [(scope,p,indices[index]) for index,p in enumerate(scope.expression.expressions) if index in indices]
        return []

    indices={index:name for index,name in enumerate(root.expression.named_selects) if name in bindings}
    projections=bound_projections(root,indices)
    if projections:
        for field,expected in constraints.items():
            # Only diagnose a demonstrably disconnected constraint: the SQL
            # itself contains the requested predicate, but a bound aggregate's
            # whole input tree cannot see it. Other shapes stay pending.
            matching=[sc for sc in scopes if restriction(sc.expression.args.get('where'),sc,field)==expected]
            if not matching:
                continue
            for projection_scope,projection,output_name in projections:
                for owner in aggregate_scopes(projection,projection_scope):
                    ancestry=inputs(owner)
                    if any(mentions_filter(sc,field) for sc in ancestry):
                        continue
                    result['errors'].append(
                        f'输出{output_name}的聚合输入未继承实体范围{field}={sorted(expected)}；'
                        '该条件只出现在其他计算分支或未使用CTE中，不能用另一分支的筛选代表本指标。'
                        '请将对象范围施加到该指标的真实统计集合；若为有意对照，明确不同范围与用途。')
                    result['checks'].append({'kind':'aggregate_entity_scope','output_column':output_name,
                        'field':field,'status':'disconnected','requires_business_review':False})
                    break
    for field, expected in constraints.items():
        state = prove(root, field, expected, set())
        check = {'kind': 'confirmed_entity_scope', 'field': field, 'expected_values': sorted(expected),
                 'status': state, 'requires_business_review': state == 'pending'}
        result['checks'].append(check)
        if state in {'conflict', 'missing'}:
            result['errors'].append(f'用户已确认实体范围{field}={sorted(expected)}未在SQL中正确实现（{state}）')
        if state == 'pending':
            result['complete'] = False
    result['passed'] = not result['errors']
    return result
