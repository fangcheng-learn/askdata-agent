"""One SQL-generation protocol and execution boundary for direct and investigation queries.

The controller owns query identity, reservations, retries and persistence.  A call
to ``generate_batch`` is exactly one model request, including malformed or unknown
responses.  This module never starts an investigation or writes its final answer.
"""
from __future__ import annotations

from ..agent_configuration_runtime import effective_prompt, effective_skill
from ..prompt_defaults import SQL_QUERY_SYSTEM

from copy import deepcopy
from contextlib import nullcontext
import json
from typing import Any, Callable

from ..errors import ModelOutputError, PipelineStageError
from ..result_references import model_result_context
from ..runtime_budget import current_budget
from .query_contract import QueryContractValidator
from .query_protocol import canonical_sql, model_contract_schema
from .measurement_context import (measurement_card, measurement_lineage,
                                  repair_diagnostics, resolve_query_requirements)
from .sql_output_protocol import (sql_batch_output_schema, normalize_clarification,
                                  metric_declarations_contract, local_measurements, normalize_postprocess)
from ..models import Clarification


class SqlQueryModule:
    def __init__(self, model_client: Any, mcp_client_factory: Callable, skill: Any,
                 database_engine: Any) -> None:
        self.model_client = model_client
        self.mcp_client_factory = mcp_client_factory
        self.skill = skill
        self.database_engine = database_engine
        self.contract_validator = QueryContractValidator(database_engine)

    def technical_system(self, database: str) -> str:
        instructions = effective_skill(self.skill.name, getattr(self.skill, 'default_instructions', '') or self.skill.instructions)
        return effective_prompt('sql_query', SQL_QUERY_SYSTEM).replace('[[SKILL:database_query]]', instructions)

    def generate_batch(self, queries: list[dict], context: dict, *,
                       model_call: Callable[[str, str, dict], dict] | None = None,
                       repair: Any = None) -> dict:
        """Generate one fixed batch once; partial valid output survives sibling errors.

        ``allow_preparation_actions`` is only the ordinary-query compatibility
        adapter. New investigations use the batch protocol exclusively.
        """
        if not isinstance(queries, list) or not queries:
            raise ValueError('SQL批次必须包含绑定查询')
        ids = [str(q.get('query_id') or '') for q in queries if isinstance(q, dict)]
        if len(ids) != len(queries) or not all(ids) or len(set(ids)) != len(ids):
            raise ValueError('SQL批次query_id必须非空且唯一')
        database = str(context.get('database') or context.get('datasource_id') or '')
        payload = deepcopy(context)
        # The internal graph is retained by the caller for validation. Sending
        # it beside the physical graph duplicates every field in model input.
        payload.pop('internal_schema_graph', None)
        payload.pop('manifest', None)  # Detailed IDs, hashes and sources are backend audit, not query knowledge.
        raw_metrics = list(context.get('registered_metrics') or [])
        graph = context.get('internal_schema_graph') or context.get('schema_graph') or {}
        resolved = context.get('measurement_requests')
        if not isinstance(resolved, (dict, list)):
            resolved = resolve_query_requirements(queries, graph, raw_metrics)
        resolved_queries = (resolved.get('queries') or [] if isinstance(resolved, dict) else resolved)
        cards = [measurement_card(metric, graph) for metric in raw_metrics]
        for card, metric in zip(cards, raw_metrics):
            if metric.get('label'):
                card['label'] = metric['label']
            if metric.get('aliases'):
                card['aliases'] = deepcopy(metric['aliases'])
        shared_cards = {card.get('id'): card for card in cards}
        for card in cards:
            for policy in card.get('component_policies') or []:
                shared = shared_cards.get(policy.get('metric_id'))
                if not shared:
                    continue
                for key in ('definition', 'unit', 'grain', 'maturity', 'required_predicates'):
                    if policy.get(key) == shared.get(key):
                        policy.pop(key, None)
                if policy.get('time_fields') == shared.get('event_time'):
                    policy.pop('time_fields', None)
        payload['registered_metrics'] = cards
        # Shared definitions occur once; each bound query references them by ID.
        registered_ids = {card.get('id') for card in cards}
        payload['measurement_requests'] = [
            {**request, 'measurements': [
                {'id': card['id'], 'kind': 'registered'} if card.get('id') in registered_ids else card
                for card in request.get('measurements') or []]}
            for request in resolved_queries if isinstance(request, dict)]
        extra_system = str(payload.pop('_system_extension', '') or '')
        allow_preparation = bool(payload.pop('allow_preparation_actions', False))
        protocol_version = int(context.get('execution_protocol_version') or 0)
        business_output = protocol_version >= 4 and not allow_preparation
        payload['queries'] = deepcopy(queries)
        # The normalized request is already the dispatched query, not a second instruction.
        payload.pop('normalized_requests', None)
        if business_output:
            header = payload.pop('task_context', {})
            if header.get('time_context') and not payload.get('time_context'):
                payload['time_context'] = header['time_context']
            # Only the business protocol carries these in task.scope_and_parameters.
            payload.pop('confirmed_parameters', None)
        payload['query_protocol'] = {
            'query_contract_schema': model_contract_schema(),
            'automatic_fields': ['original_question', 'data_source', 'tables', 'dimensions',
                                 'filters', 'time_range', 'time_ranges', 'grain'],
            'output_schema': {'queries': [{'query_id': 'bound query_id', 'sql': 'one read-only SELECT/WITH',
                                          'query_contract': {'metrics': []}}]},
            'clarification_schema': Clarification.model_json_schema(),
        }
        response_schema = None
        if business_output:
            response_schema = sql_batch_output_schema(ids)
            payload['query_protocol'] = {
                'execution_protocol_version': protocol_version,
                'output_schema': response_schema,
                'automatic_fields': ['original_question', 'data_source', 'tables', 'dimensions',
                                     'derived_dimensions', 'filters', 'time_range', 'time_ranges', 'grain'],
                'instructions': '成功项仅sql与metric_declarations业务声明；不要重复填写机械query_contract。'
                    '程序从实际SQL构造表、派生维度、筛选与时间。澄清仅限用户业务条件、指标定义或实体身份；'
                    '技术字段映射、检索资料缺口使用missing_context，不要求用户挑物理字段。',
            }
        if repair is not None:
            failed = deepcopy(repair)
            entries = list(failed.values()) if isinstance(failed, dict) else failed if isinstance(failed, list) else []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                item['repair_diagnostics'] = repair_diagnostics(
                    item.get('error') or item.get('message') or '', sql=str(item.get('sql') or ''),
                    query_contract=item.get('query_contract'), measurements={'queries': resolved_queries},
                    schema_graph=graph)
            payload['failed_queries'] = failed
            payload['repair_instruction'] = ('仅修复这些已绑定失败查询；原SQL、契约和真实错误见failed_queries。'
                                             '成功项不在本调用中，不得重算、增加或换ID。')
        system = self.technical_system(database) + extra_system
        if protocol_version >= 4:
            system += ('\n继承inherited_measurements与dependency_results中本任务已有指标定义、原SQL和结果范围。'
                       '任务内定义不是登记口径认证；同名不同版本必须核对适用范围。'
                       '同一度量在下一步不要改用另一统计集合或时间字段；若需改变，明确新的local_measurements定义与原因。'
                       'observed_output仅为已有输出列，先根据原SQL核对含义，不猜它等于哪个登记指标。'
                       '每项声明的output_columns必须指向最终SELECT中的真实别名；SQL与业务ID不用强行同名。source_fields区分度量取值与分组/筛选依赖。'
                       '一次SQL返回的每项业务度量均填写metric_declarations；未登记度量用实际输出别名绑定完整local_measurements，'
                       '包含统计对象、取值来源、事件时间、集合、单位及适用范围，不漏掉顺带返回的商品件数等指标。')
        if business_output:
            system += ('\n当前执行协议4：严格按query_protocol.output_schema输出queries。成功项status=query，'
                       '只填query_id、sql、metric_declarations及必要local_measurements/technical_note/postprocess；'
                       '不要query_contract，也不要重复声明机械维度、筛选、时间和表。'
                       '澄清须提供category、question、reason和实际选项对象；技术映射问题用missing_context。')
        if allow_preparation:
            system += ('\n旧单项兼容：当前只有一个绑定查询，也接受Skill的call_tool/clarify；'
                       '时间工具和显式启用的step_decision继续按已提供协议返回。'
                       '工具动作使用action=call_tool、tool_name（来自mcp_tools）、arguments、reason；'
                       '数据库工具动作另附query_contract.metrics，时间工具不附SQL契约。'
                       '澄清动作使用action=clarify、clarification（按query_protocol.clarification_schema）。')
        node = 'sql_repair' if repair is not None else 'query_coder'
        budget = current_budget.get()
        if budget:
            budget.model_node = node
            budget.skill(self.skill.name, effective_skill(self.skill.name, getattr(self.skill, 'default_instructions', '') or self.skill.instructions), node)
        try:
            if response_schema is not None:
                from ..structured_output import using_output_schema
                schema_context = using_output_schema('sql_batch', response_schema)
            else:
                schema_context = nullcontext()
            with schema_context:
                if model_call:
                    raw = model_call(node, system, model_result_context(payload))
                else:
                    raw = self.model_client.chat_json(system, json.dumps(model_result_context(payload), ensure_ascii=False))
        except ModelOutputError as exc:
            # One response has already spent its reservation. Return the real
            # format failure to the caller's shared generation ledger;
            # never retry here. Service/network exceptions deliberately escape.
            return {'candidates': {}, 'errors': {
                qid: {'kind': 'output_format', 'message': str(exc)} for qid in ids},
                'raw_response': None}
        result = {'candidates': {}, 'errors': {}, 'raw_response': raw}
        if not isinstance(raw, dict):
            result['errors'] = {qid: {'kind': 'output_format', 'message': 'SQL模块未返回JSON对象'} for qid in ids}
            return result
        if allow_preparation and len(ids) == 1 and raw.get('action'):
            result['decision'] = deepcopy(raw)
            if raw.get('action') == 'clarify':
                try:
                    result['decision']['clarification'] = normalize_clarification(raw.get('clarification'))
                except ValueError as exc:
                    # Let the ordinary adapter consume this precise bounded
                    # format error, rather than replacing it with "empty action".
                    result.pop('decision', None)
                    result['errors'][ids[0]] = {'kind': 'output_format', 'message': str(exc)}
            if raw.get('action') == 'call_tool' and isinstance(raw.get('arguments'), dict) and raw['arguments'].get('sql'):
                result['candidates'][ids[0]] = {'query_id': ids[0], 'sql': raw['arguments']['sql'],
                                               'query_contract': raw.get('query_contract'),
                                               'technical_note': raw.get('reason', '')}
            return result
        items = raw.get('queries')
        if not isinstance(items, list):
            result['errors'] = {qid: {'kind': 'output_format', 'message': 'SQL模块缺少queries数组'} for qid in ids}
            return result
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            qid = str(item.get('query_id') or '')
            if qid not in ids:
                result.setdefault('protocol_errors', []).append('忽略未绑定query_id：' + qid)
                continue
            if qid in seen:
                result['candidates'].pop(qid, None)
                result['errors'][qid] = {'kind': 'output_format', 'message': '同一query_id返回多项，不能确定执行候选'}
                continue
            seen.add(qid)
            status = item.get('status')
            if status in {'clarify', 'missing_context', 'unsupported'}:
                if item.get('sql'):
                    result['errors'][qid] = {'kind': 'output_format', 'message': '缺口动作不能同时附带SQL'}
                else:
                    detail = item.get('clarification') or {}
                    category = detail.get('category') if isinstance(detail, dict) else None
                    message = str(item.get('reason') or (detail.get('question') if isinstance(detail, dict) else '') or '查询所需资料不完整')
                    if status == 'clarify' and category in {'technical_mapping', 'schema_field', 'missing_field', 'technical_context'}:
                        result['errors'][qid] = {'kind': 'missing_context', 'message': message,
                            'missing_requests': deepcopy(item.get('missing_requests') or [
                                {'kind': 'field', 'request': message, 'reason': '内部技术映射不得作为用户业务澄清'}])}
                    elif status == 'clarify':
                        try:
                            if business_output and category not in {'business_parameter', 'metric_definition', 'entity_identity'}:
                                raise ValueError('clarification.category必须明确为业务参数、指标定义或实体身份；技术映射使用missing_context')
                            clarification = normalize_clarification(detail)
                            result['errors'][qid] = {'kind': 'clarification', 'message': message,
                                'clarification': clarification,
                                'clarification_category': category or 'business_parameter'}
                        except ValueError as exc:
                            result['errors'][qid] = {'kind': 'output_format', 'message': str(exc)}
                    else:
                        result['errors'][qid] = {'kind': 'capability' if status == 'unsupported' else 'missing_context',
                            'message': message,
                            **{key: deepcopy(item[key]) for key in ('missing_requests',) if key in item}}
                continue
            if business_output and status not in {None, 'query'}:
                result['errors'][qid] = {'kind': 'output_format', 'message': '成功项status只能为query'}
                continue
            if not isinstance(item.get('sql'), str) or not item['sql'].strip():
                result['errors'][qid] = {'kind': 'output_format', 'message': '查询缺少非空SQL'}
            else:
                try:
                    contract = (metric_declarations_contract(item) if 'metric_declarations' in item
                                else deepcopy(item.get('query_contract')))
                    if not isinstance(contract, dict):
                        raise ValueError('成功项需metric_declarations业务声明；旧协议需query_contract对象')
                except ValueError as exc:
                    result['errors'][qid] = {'kind': 'output_format', 'message': str(exc)}
                    continue
                result['candidates'][qid] = {'query_id': qid, 'sql': item['sql'],
                                             'query_contract': contract,
                                             'technical_note': str(item.get('technical_note') or item.get('reason') or '')}
                if item.get('postprocess') is not None:
                    try:
                        if protocol_version < 4:
                            raise ValueError('postprocess仅适用于协议4')
                        result['candidates'][qid]['postprocess'] = normalize_postprocess(item['postprocess'])
                    except ValueError as exc:
                        result['candidates'].pop(qid, None)
                        result['errors'][qid] = {'kind': 'output_format', 'message': 'postprocess格式不符：' + str(exc)}
                        continue
                local = item.get('local_measurements')
                if local is not None:
                    try:
                        result['candidates'][qid]['local_measurements'] = local_measurements(local, protocol_version=protocol_version)
                    except ValueError as exc:
                        result['candidates'].pop(qid, None)
                        result['errors'][qid] = {'kind': 'output_format', 'message': str(exc)}
        for qid in ids:
            if qid not in result['candidates'] and qid not in result['errors']:
                result['errors'][qid] = {'kind': 'output_format', 'message': '模型未返回该绑定查询'}
        return result

    def validate_candidate(self, *, sql: str, query_contract: Any, question: str, database: str,
                           schema_graph: dict, access_scope: dict,
                           validation_context: dict | None = None) -> dict:
        raw_declarations = (deepcopy(query_contract.get('metrics') or [])
                            if isinstance(query_contract, dict) else None)
        sql, source_normalizations = canonical_sql(sql, database, schema_graph, access_scope)
        plan = normalize_postprocess((validation_context or {}).get('postprocess'))
        if plan and plan['operation'] == 'state_duration':
            import sqlglot
            from sqlglot import exp
            statement = sqlglot.parse_one(sql, read='duckdb')
            projections = list(statement.selects)
            if not any(node.find(exp.Star) for node in projections):
                available = {node.alias_or_name for node in projections}
                required = {plan['time_field'], plan['order_field'], plan['opening_field'],
                            *plan['partition_by'], *(term['column'] for term in plan['delta_terms'])}
                missing = required - available
                if missing:
                    raise ValueError('postprocess计算需要的列未在SQL最终结果输出：' + ', '.join(sorted(missing)) +
                                     '；实际输出列：' + ', '.join(sorted(available)))
        contract = self.contract_validator.validate(query_contract, sql, question, database,
                                                     schema_graph, access_scope,
                                                     validation_context=validation_context)
        from .grouping_constraints import disconnected_group_metrics
        grouping_errors = disconnected_group_metrics(sql, (validation_context or {}).get('request'), contract, schema_graph)
        if grouping_errors:
            raise ValueError('；'.join(grouping_errors))
        if (validation_context or {}).get('entity_resolutions'):
            from .request_constraints import validate_request_constraints
            scope_check = validate_request_constraints(sql=sql, query_contract=contract,
                request=validation_context.get('request'),
                entity_resolutions=validation_context['entity_resolutions'],
                schema_graph=schema_graph, query_id=validation_context.get('query_id'))
            contract.setdefault('validation', {})['request_scope_checks'] = scope_check
            if scope_check['errors']:
                raise ValueError('；'.join(scope_check['errors']))
            if not scope_check['complete']:
                contract['validation'].setdefault('pending_checks', []).append({
                    'kind': 'entity_scope', 'message': '复杂SQL的用户实体范围需要结合实际证据审核',
                    'checks': scope_check['checks']})
        # Exact named/validator bindings carry registered meaning into results.
        # Unproved display conversions remain explicitly unbound, not guessed.
        context_metrics = list((validation_context or {}).get('registered_metrics') or [])
        if not context_metrics:
            from .. import homenova
            if homenova.is_homenova(database):
                context_metrics = [m for entry in contract.get('metrics') or []
                                   if (m := homenova.registered_metric(database, entry.get('name') or ''))]
        local = list((validation_context or {}).get('local_measurements') or [])
        # Keep the model's business identity before a validator legitimately
        # normalizes an unknown display name to its physical source label.
        lineage = measurement_lineage(sql, contract, context_metrics, schema_graph,
                                     local_measurements=local, declared_metrics=raw_declarations)
        validation = contract.setdefault('validation', {})
        validation['column_metadata'] = lineage['column_metadata']
        validation['task_local_measurements'] = lineage['task_local_measurements']
        validation['unbound_measurements'] = lineage['unbound_measurements']
        pending = validation.setdefault('pending_checks', [])
        for issue in lineage['pending_checks']:
            if issue not in pending:
                pending.append(issue)
        if local:
            pending.append({'code': 'task_local_measurement_semantics',
                'message': '任务内指标定义必须核对统计集合与事件，不因格式合法认证为登记口径',
                'measurement_ids': [item.get('id') for item in local if isinstance(item, dict)]})
        budget = current_budget.get()
        if budget:
            budget.record('query_validation', normalizations=validation.get('normalizations', []),
                          pending_checks=pending)
        return {'sql': sql, 'query_contract': contract, 'source_normalizations': source_normalizations}

    def execute_prepared(self, *, database: str, sql: str, access_scope: dict,
                         query_contract: dict, mcp_client: Any = None,
                         source_normalizations: list | None = None) -> dict:
        client = mcp_client if mcp_client is not None else self.mcp_client_factory(access_scope)
        execution = client.call_tool('query_' + database, {'sql': sql})
        if not isinstance(execution, dict):
            raise RuntimeError('数据库工具未返回结构化结果')
        if execution.get('error_kind') == 'storage':
            raise PipelineStageError('result_storage', execution.get('error') or '结果持久化失败，禁止重试SQL',
                                     {'sql': sql, 'execution': execution})
        metadata = (query_contract.get('validation') or {}).get('column_metadata') or {}
        if metadata and execution.get('success'):
            execution['column_metadata'] = {column: deepcopy(value) for column, value in metadata.items()
                                            if column in (execution.get('columns') or [])}
        return {'sql': sql, 'query_contract': query_contract, 'execution': execution,
                'source_normalizations': list(source_normalizations or [])}

    def execute_candidate(self, *, sql: str, query_contract: Any, question: str, database: str,
                          schema_graph: dict, access_scope: dict, validation_context: dict | None = None,
                          mcp_client: Any = None, validated_contract: dict | None = None) -> dict:
        prepared = ({'sql': sql, 'query_contract': validated_contract, 'source_normalizations': []}
                    if validated_contract is not None else self.validate_candidate(
                        sql=sql, query_contract=query_contract, question=question, database=database,
                        schema_graph=schema_graph, access_scope=access_scope,
                        validation_context=validation_context))
        return self.execute_prepared(database=database, access_scope=access_scope,
                                     mcp_client=mcp_client, **prepared)
