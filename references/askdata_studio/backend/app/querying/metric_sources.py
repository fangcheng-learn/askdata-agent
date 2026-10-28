"""Validate source choices before a user can confirm a metric binding."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..database import SCHEMA, physical_table_name
from ..security import AccessScope


def normalize_source_clarification(
    clarification: dict[str, Any],
    database: str,
    schema_graph: dict[str, Any],
    access_scope: dict[str, Any],
) -> dict[str, Any]:
    """Keep legacy choices, but bind explicit sources only to authorized metadata.

    A query's own ``source_fields`` are deliberately not an input. A source is
    confirmed only after the user selects one of these validated options.
    """
    result = deepcopy(clarification)
    scope = AccessScope.from_dict(access_scope)
    definitions = {
        str(table['id']): table for table in SCHEMA
        if str(table.get('database') or 'askdata_mock') == database
        and scope.allows_table(database, str(table['id']))
    }
    graph_tables = {
        str(table.get('id')): definitions[str(table.get('id'))]
        for table in schema_graph.get('tables', [])
        if str(table.get('id')) in definitions
        and str(table.get('database') or schema_graph.get('database') or database) == database
        and str(table.get('name') or physical_table_name(definitions[str(table.get('id'))])).casefold()
        == physical_table_name(definitions[str(table.get('id'))]).casefold()
    }
    source_fields: dict[str, tuple[str, dict, dict]] = {}
    graph_field_names = {
        (str(field.get('table_id')), str(field.get('name')).casefold())
        for field in schema_graph.get('fields', [])
    }
    for table_id, table in graph_tables.items():
        for field in table.get('fields', []):
            # A graph that explicitly lists fields limits choices to that list.
            # Older internal graphs carry full table definitions instead.
            if 'fields' in schema_graph and (table_id, str(field['name']).casefold()) not in graph_field_names:
                continue
            reference = physical_table_name(table) + '.' + str(field['name'])
            source_fields[reference.casefold()] = (reference, table, field)

    for option in result.get('options') or []:
        if not isinstance(option, dict):
            raise ValueError('澄清选项必须为对象')
        bindings = option.get('metric_sources', {})
        if bindings is None:
            bindings = {}
        if not isinstance(bindings, dict):
            raise ValueError('澄清指标来源必须为名称到物理字段的映射')
        normalized: dict[str, str] = {}
        descriptions: list[str] = []
        for metric, reference in bindings.items():
            if not isinstance(metric, str) or not metric.strip() or not isinstance(reference, str):
                raise ValueError('澄清指标来源名称和字段必须为非空字符串')
            source = source_fields.get(reference.strip().casefold())
            if source is None:
                raise ValueError('澄清指标来源不在当前授权Schema图中：' + reference)
            canonical, table, field = source
            names = {str(name).strip().casefold() for name in
                     (field.get('name', ''), field.get('label', ''), *(field.get('aliases') or []))}
            metric_name = metric.strip().casefold()
            if metric_name not in names:
                raise ValueError('澄清指标名称与来源字段不匹配：' + metric)
            metric_key = str(field['name']).casefold()
            if metric_key in normalized and normalized[metric_key] != canonical:
                raise ValueError('澄清选项为同一指标指定了冲突来源：' + metric)
            normalized[metric_key] = canonical
            descriptions.append(f"{table.get('label') or physical_table_name(table)} · {field.get('label') or field['name']}（{canonical}）")
        option['metric_sources'] = normalized
        if descriptions:
            suffix = '数据来源：' + '；'.join(dict.fromkeys(descriptions))
            description = str(option.get('description') or '')
            if suffix not in description:
                option['description'] = description + ('；' if description else '') + suffix
    return result
