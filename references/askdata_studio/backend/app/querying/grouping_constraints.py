"""Detect a disconnected aggregate broadcast as a requested group metric.

This is a narrow structural contradiction, not a universal grain/cohort proof.
Mixed-origin ratios, unions, and explicit benchmark requests remain reviewable.
"""
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


def disconnected_group_metrics(sql, request, contract, graph):
    if not request or request.get('comparison_purpose'):
        return []
    names = {t['id']:t['name'] for t in graph.get('tables') or []}
    fields = {f"{names[f['table_id']]}.{f['name']}" for f in graph.get('fields') or [] if f.get('table_id') in names}
    dimensions = {d for d in request.get('dimensions') or [] if d in fields}
    if not dimensions:
        return []
    scopes = traverse_scope(sqlglot.parse_one(sql, read='duckdb'))
    root = scopes[-1]
    if not isinstance(root.expression, exp.Select):
        return []

    def origins(node, scope, seen=None):
        seen = set(seen or ())
        key = (id(node), id(scope))
        if key in seen:
            return []
        seen.add(key)
        if any(a.find_ancestor(exp.Select) is scope.expression for a in node.find_all(exp.AggFunc)):
            return [scope]
        result=[]
        for column in node.find_all(exp.Column):
            sources=scope.selected_sources
            choices=([sources[column.table][1]] if column.table in sources else
                     [next(iter(sources.values()))[1]] if not column.table and len(sources)==1 else [])
            for child in choices:
                if isinstance(child,Scope) and isinstance(child.expression,exp.Select):
                    matches=[p for p in child.expression.expressions if p.alias_or_name==column.name]
                    if len(matches)==1:
                        result.extend(origins(matches[0],child,seen))
        return result

    def tables(scope, seen=None):
        seen=set(seen or ())
        if id(scope) in seen:
            return set()
        seen.add(id(scope));result=set()
        for _, source in scope.selected_sources.values():
            if isinstance(source,exp.Table):result.add(source.name)
            elif isinstance(source,Scope):result.update(tables(source,seen))
        return result

    bindings={c for m in contract.get('metrics') or [] for c in m.get('output_columns') or []}
    errors=[]
    for projection in root.expression.expressions:
        if projection.alias_or_name not in bindings:
            continue
        found={id(s):s for s in origins(projection,root)}
        if len(found)!=1:
            continue
        scope=next(iter(found.values()))
        if scope is root:
            continue
        available=tables(scope)
        if available and all(d.rsplit('.',1)[0] not in available for d in dimensions):
            errors.append('指标输出 '+projection.alias_or_name+' 来自独立汇总分支，不含请求分组 '+
                          ', '.join(sorted(dimensions))+' 的数据来源；不能将总体值复制成各分组指标。'+
                          '保留分组目标，使用适用统计集合重查；若要总体对照，请单独明确对照查询。')
    return errors
