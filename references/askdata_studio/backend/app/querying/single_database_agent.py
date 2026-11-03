from __future__ import annotations

import json
from fnmatch import fnmatch
from typing import Any, Callable

from ..errors import PipelineStageError
from ..runtime_budget import current_budget, BudgetStop
from ..mcp_runtime.client import LocalMcpClient
from ..model_client import ModelClient
from ..skills import SkillDefinition
from .duckdb_engine import DuckDbEngine
from .sql_module import SqlQueryModule
from .query_context import assemble_query_context
from .measurement_context import resolve_query_requirements, repair_diagnostics
from .metric_sources import normalize_source_clarification
from .. import homenova
from ..task_decisions import StepDecision, STEP_PROTOCOL_INSTRUCTION, validate_step_decision


class SingleDatabaseAgent:
    """让模型通过MCP工具完成单数据库查询。"""

    def __init__(
        self,
        model_client: ModelClient,
        mcp_client_factory: Callable[[dict[str, Any]], LocalMcpClient],
        skill: SkillDefinition,
        database_engine: DuckDbEngine,
        max_tool_calls: int = 3,
    ) -> None:
        self.model_client = model_client
        self.mcp_client_factory = mcp_client_factory
        self.skill = skill
        self.sql_module = SqlQueryModule(model_client, mcp_client_factory, skill, database_engine)
        self.contract_validator = self.sql_module.contract_validator
        self.max_tool_calls = min(max_tool_calls, skill.max_tool_calls)

    def prepare(
        self,
        query: str,
        database: str,
        schema_graph: dict[str, Any],
        schema_context: str,
        retrieval: dict[str, Any],
        workspace: dict[str, Any],
        access_scope: dict[str, Any],
        original_query: str | None = None,
        task_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        database_tool = f"query_{database}"
        # These dependencies can be refreshed by the owning workflow (and by
        # controlled tests); the shared module must use that current instance.
        self.sql_module.model_client = self.model_client
        self.sql_module.skill = self.skill
        mcp_client = self.mcp_client_factory(access_scope)

        # 每次请求都通过MCP tools/list获取当前工具定义。
        catalog = mcp_client.list_tools()
        tools = [
            tool
            for tool in catalog
            if any(fnmatch(tool["name"], pattern) for pattern in self.skill.allowed_tools)
            and (not tool["name"].startswith("query_") or tool["name"] == database_tool)
        ]
        tool_names = {tool["name"] for tool in tools}
        if database_tool not in tool_names:
            raise PipelineStageError(
                "mcp_tool_discovery",
                f"没有找到数据库工具：{database_tool}",
            )

        system = ("\n若需澄清同名字段来源，选项填写metric_sources（公开名称到真实table.field）。"
                  "已确认来源见confirmed_metric_sources，澄清历史不得重复询问。")
        if (task_context or {}).get('step_protocol'):
            system += (STEP_PROTOCOL_INSTRUCTION + '\n查询步骤适用时返回原有action=call_tool及SQL/契约，并附step_decision。'
                       '不适用时返回{"action":"step_decision","step_decision":...}，不附SQL或工具。'
                       + json.dumps(StepDecision.model_json_schema(), ensure_ascii=False))
        original_question = query if task_context and task_context.get("step_id") else (original_query or query)

        observations: list[dict[str, Any]] = []
        tool_trace: list[dict[str, Any]] = []
        validation_attempts: list[dict[str, Any]] = []
        budget = current_budget.get()
        generation_key = database + ':' + str((task_context or {}).get('step_id') or 'direct')
        generation_ledger = {
            'key': generation_key,
            'used': (budget.direct_sql_generations.get(generation_key, 0) if budget else 0),
            'limit': (budget.sql_generation_limit if budget else 3),
        }
        selected_metrics = workspace.get('selected_semantics') or []
        bound_query = {'query_id': generation_key, 'goal': query,
                       'metric_requirements': [m['id'] for m in selected_metrics if m.get('id')]}
        measurements = resolve_query_requirements([bound_query], schema_graph, selected_metrics)
        query_context = assemble_query_context(
            schema_graph, metrics=selected_metrics, retrieval=retrieval,
            access_scope=access_scope, database=database)
        base_payload = {
            **{key: value for key, value in query_context.items() if key != 'internal_schema_graph'},
            "query": query, "task_context": task_context or {},
            "original_question": original_question, "database": database,
            'measurement_requests': measurements,
            "confirmed_parameters": workspace.get("confirmed_parameters", {}),
            "confirmed_metric_sources": workspace.get("confirmed_metric_sources") or {},
            "clarification_history": workspace.get("clarification_history") or [],
            "scenario": workspace.get("scenario") or {}, "mcp_tools": tools,
            "retrieval": {key: retrieval.get(key) for key in
                          ('threshold', 'selected_count', 'hybrid_selected_count', 'dependency_added_count')},
            "time_context": {'reference_now': budget.reference_now if budget else '',
                             'rule': 'real_current_time_not_snapshot_analysis_date'},
        }
        if homenova.is_homenova(database):
            base_payload['term_notes'] = homenova.public_term_notes(database, query)

        try:
            for call_index in range(1, self.max_tool_calls + 1):
                payload = {**base_payload, "tool_results": observations}
                decision = self._validated_decision(
                    system, payload, tool_names, database_tool, original_question,
                    database, schema_graph, access_scope, validation_context={
                        "task_id": workspace.get("task_id") or (current_budget.get().task_id if current_budget.get() else None),
                        "confirmed_metric_sources": workspace.get("confirmed_metric_sources") or {},
                        "clarification_history": workspace.get("clarification_history") or [],
                        'registered_metrics': selected_metrics,
                    },
                    generation_ledger=generation_ledger,
                )
                validation_attempts.extend(decision.pop("validation_attempts", []))

                if decision['action'] == 'step_decision':
                    return {'action': 'step_decision', 'step_decision': decision['step_decision'],
                            'tool_trace': tool_trace, 'validation_attempts': validation_attempts}

                if decision["action"] == "clarify":
                    return {
                        "action": "clarify",
                        "clarification": decision["clarification"],
                        "tool_trace": tool_trace,
                        "validation_attempts": validation_attempts,
                        "step_decision": decision.get('step_decision'),
                        "sql_generations": generation_ledger['used'],
                        "sql_generation_limit": generation_ledger['limit'],
                    }
                tool_name = str(decision["tool_name"])
                arguments = decision["arguments"]

                if tool_name == database_tool:
                    prepared_result = self.sql_module.execute_prepared(
                        database=database, sql=arguments['sql'], access_scope=access_scope,
                        query_contract=decision['validated_query_contract'], mcp_client=mcp_client)
                    tool_result = prepared_result['execution']
                else:
                    tool_result = mcp_client.call_tool(tool_name, arguments)
                trace = {
                    "call_index": call_index,
                    "tool": tool_name,
                    "arguments": arguments,
                    "query_contract": decision.get("validated_query_contract"),
                    "result": tool_result,
                    "reason": str(decision.get("reason") or ""),
                }
                if tool_result.get('sql') and arguments.get('sql') != tool_result['sql']:
                    trace['sql_normalization'] = {'reason': '程序清理或展开已明确的中间结果字段',
                                                  'original_sql': arguments.get('sql'), 'executed_sql': tool_result['sql']}
                tool_trace.append(trace)

                if tool_name == database_tool:
                    if tool_result.get('error_kind') == 'storage':
                        raise PipelineStageError('result_storage', tool_result.get('error') or '结果持久化失败')
                    if not bool(tool_result.get('success')) and generation_ledger['used'] >= generation_ledger['limit']:
                        exhausted = BudgetStop(f"每条SQL最多生成{generation_ledger['limit']}次（包含首次生成），停止继续重写")
                        exhausted.context = {'last_sql_error': tool_result.get('error'),
                                             'error_kind': tool_result.get('error_kind') or 'sql_execution'}
                        raise exhausted
                    if not bool(tool_result.get("success")) and call_index < self.max_tool_calls:
                        observations.append({
                            "tool": tool_name,
                            "result": {
                                "success": False,
                                "error": tool_result.get("error"),
                                "sql": tool_result.get("sql"),
                            },
                            "instruction": (
                                "上一条SQL执行失败。只根据数据库错误和Schema图修正SQL；"
                                "保持原问题的指标、时间和分组口径，重新调用同一个数据库工具。"
                            ),
                        })
                        continue
                    return {
                        "action": "executed",
                        "execution": tool_result,
                        "tool_trace": tool_trace,
                        "source": "model_mcp",
                        "validation_attempts": validation_attempts,
                        "step_decision": decision.get('step_decision'),
                        "sql_generations": generation_ledger['used'],
                        "sql_generation_limit": generation_ledger['limit'],
                    }

                observations.append({"tool": tool_name, "result": tool_result})

            raise ValueError(f"MCP工具调用超过上限：{self.max_tool_calls}")
        except BudgetStop as exc:
            exc.context = {**getattr(exc, "context", {}), "tool_trace": tool_trace,
                "sql_generations": generation_ledger['used'], "sql_generation_limit": generation_ledger['limit'],
                "validation_attempts": [*validation_attempts, *getattr(exc, "context", {}).get("validation_attempts", [])]}
            raise
        except PipelineStageError as exc:
            exc.context.setdefault("datasource_id", database)
            exc.context.setdefault("tool_trace", tool_trace)
            exc.context.setdefault("sql_generations", generation_ledger['used'])
            exc.context.setdefault("sql_generation_limit", generation_ledger['limit'])
            exc.context.setdefault("retrieval_audit", {
                key: retrieval.get(key) for key in
                ("embedding_source", "rerank_source", "bm25_count", "dense_count", "rrf_count",
                 "hybrid_selected_count", "dependency_added_count", "selected_count")
            })
            exc.context["validation_attempts"] = [*validation_attempts, *exc.context.get("validation_attempts", [])]
            raise
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            stage = "query_contract_validation" if isinstance(exc, ValueError) and str(exc).startswith("查询前校验失败") else "single_database_agent"
            raise PipelineStageError(stage, str(exc), {"datasource_id": database,
                "validation_attempts": validation_attempts, "tool_trace": tool_trace,
                "sql_generations": generation_ledger['used'], "sql_generation_limit": generation_ledger['limit']}) from exc

    def _validated_decision(
        self,
        system: str,
        payload: dict[str, Any],
        tool_names: set[str],
        database_tool: str,
        original_question: str,
        database: str,
        schema_graph: dict[str, Any],
        access_scope: dict[str, Any],
        validation_context: dict[str, Any] | None = None,
        generation_ledger: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Share the caller's SQL allowance across format, contract and execution fixes."""
        error = ""
        previous: dict[str, Any] | None = None
        attempts: list[dict[str, Any]] = []
        failed_signature = None
        generation_ledger = generation_ledger if generation_ledger is not None else {
            'key': database + ':direct', 'used': 0, 'limit': 3}

        def generation_changed() -> None:
            budget = current_budget.get()
            if budget:
                budget.direct_sql_generations[generation_ledger['key']] = generation_ledger['used']
                budget.record('direct_sql_generation', generation_key=generation_ledger['key'],
                              used=generation_ledger['used'], limit=generation_ledger['limit'])

        for attempt in range(generation_ledger['limit']):
            budget = current_budget.get()
            if budget:
                budget.model_node = "query_coder"
            if generation_ledger['used'] >= generation_ledger['limit']:
                exc = BudgetStop(f"每条SQL最多生成{generation_ledger['limit']}次（包含首次生成），停止继续重写")
                exc.context = {'validation_attempts': attempts,
                               'sql_generations': generation_ledger['used'],
                               'sql_generation_limit': generation_ledger['limit']}
                raise exc
            repair_before = None
            repair_step = budget.step if budget else None
            if budget and generation_ledger['used']:
                try:
                    repair_before = budget.repairs.get(budget.step, 0)
                    budget.repair("格式/查询契约/SQL修正")
                except BudgetStop as exc:
                    exc.context = {"validation_attempts": attempts}
                    raise
            request = dict(payload)
            request['sql_generation'] = {'used': generation_ledger['used'],
                                         'limit': generation_ledger['limit'],
                                         'instruction': '总次数包含首次；只在剩余额度内定向修复，SQL模块澄清仍占次数，旧任务继承原上限。'}
            if error:
                request["previous_invalid_output"] = previous
                request["output_validation_error"] = error
                request["output_validation_instruction"] = (
                    "查询前校验失败。保持原问题、指标和粒度；根据具体错误定向修复contract或SQL，保留无关已通过部分，不要重复相同无效产物。"
                    "若业务口径或必要信息无法确定则返回clarify。"
                )
            # The next response can contain SQL. Reserve its generation before
            # dispatch so invalid output, unknown network response or restart
            # cannot make an authorized generation free. Valid non-SQL actions
            # refund only this tentative count after their shape is checked.
            generation_ledger['used'] += 1
            generation_changed()

            def refund_non_sql() -> None:
                generation_ledger['used'] -= 1
                if budget and repair_before is not None and budget.repairs.get(repair_step) == repair_before + 1:
                    if repair_before:
                        budget.repairs[repair_step] = repair_before
                    else:
                        budget.repairs.pop(repair_step, None)
                    budget.record('direct_sql_repair_refund', reason='实际返回有效非SQL动作', generation_key=generation_ledger['key'])
                generation_changed()

            # Both direct queries and investigation batches use this generator.
            # The existing ordinary controller still owns time tools and its
            # compatibility StepDecision protocol.
            generated = self.sql_module.generate_batch(
                [{'query_id': generation_ledger['key'], 'goal': payload.get('query') or original_question,
                  'metric_requirements': [m['id'] for m in payload.get('registered_metrics') or [] if m.get('id')]}],
                {**request, 'allow_preparation_actions': True, '_system_extension': system},
                repair=({generation_ledger['key']: {'sql': str(((previous or {}).get('arguments') or {}).get('sql') or ''),
                    'query_contract': (previous or {}).get('query_contract'), 'error': error}}
                    if error else None))
            decision = generated.get('decision')
            generation_error = None
            if decision is None:
                candidate = generated['candidates'].get(generation_ledger['key'])
                if candidate:
                    decision = {'action': 'call_tool', 'tool_name': database_tool,
                                'arguments': {'sql': candidate['sql']},
                                'query_contract': candidate['query_contract'],
                                'reason': candidate.get('technical_note', '')}
                else:
                    # Feed malformed batch output through the same bounded
                    # validation feedback, without another hidden model call.
                    raw_decision = generated.get('raw_response')
                    decision = raw_decision if isinstance(raw_decision, dict) else {}
                    generation_error = (generated.get('errors', {}).get(generation_ledger['key']) or {}).get('message')
            previous = decision
            action = str(decision.get("action") or "")
            audit = {"attempt": attempt + 1, "database": database, "action": action,
                     "step_decision": decision.get('step_decision'),
                     "tool_name": decision.get("tool_name"),
                     "sql": str((decision.get("arguments") or {}).get("sql") or "") if isinstance(decision.get("arguments"), dict) else "",
                     "query_contract": decision.get("query_contract"), "success": False}
            audit['sql_generations'] = generation_ledger['used']
            audit['sql_generation_limit'] = generation_ledger['limit']
            try:
                if generation_error:
                    raise ValueError(generation_error)
                step_context = payload.get('task_context') or {}
                protocol = bool(step_context.get('step_protocol'))
                if action not in self.skill.output_actions and not (protocol and action == 'step_decision'):
                    raise ValueError(f"Skill不允许输出动作：{action}")
                if protocol:
                    changes = []
                    front = validate_step_decision(decision.get('step_decision'), step_context, normalizations=changes)
                    if (action == 'call_tool') != (front.action == 'execute_step'):
                        raise ValueError('工具调用与前置决策动作不一致')
                    decision['step_decision'] = front.model_dump()
                    audit['normalized_step_decision'] = front.model_dump()
                    audit['reference_normalizations'] = changes
                    if changes and budget:
                        budget.record('result_reference_normalization', changes=changes)
                    if action == 'step_decision':
                        if decision.get('arguments') or decision.get('tool_name') or decision.get('query_contract'):
                            raise ValueError('不执行本步时不能附带SQL或工具调用')
                        audit['success'] = True
                        refund_non_sql()
                        audit['sql_generations'] = generation_ledger['used']
                        attempts.append(audit)
                        decision['validation_attempts'] = attempts
                        return decision
                if action == "clarify":
                    if decision.get('arguments') or decision.get('tool_name') or decision.get('query_contract'):
                        raise ValueError('澄清动作不能附带SQL、工具调用或查询契约')
                    clarification = decision.get("clarification")
                    if not isinstance(clarification, dict) or len(clarification.get("options") or []) < 2:
                        raise ValueError("智能体返回的澄清信息不完整")
                    decision['clarification'] = normalize_source_clarification(
                        clarification, database, schema_graph, access_scope)
                    audit["success"] = True
                    audit['sql_generations'] = generation_ledger['used']
                    attempts.append(audit)
                    decision["validation_attempts"] = attempts
                    return decision
                if action != "call_tool":
                    raise ValueError("智能体必须返回call_tool或clarify")
                tool_name = str(decision.get("tool_name") or "")
                if tool_name not in tool_names:
                    raise ValueError(f"智能体选择了未提供的MCP工具：{tool_name}")
                if not isinstance(decision.get("arguments"), dict):
                    raise ValueError("MCP工具参数必须是JSON对象")
                if tool_name == database_tool:
                    # Keep the public validator alias for existing injected
                    # validators, while sharing the real implementation.
                    self.sql_module.contract_validator = self.contract_validator
                    prepared = self.sql_module.validate_candidate(
                        sql=str(decision['arguments'].get('sql') or ''),
                        query_contract=decision.get('query_contract'), question=original_question,
                        database=database, schema_graph=schema_graph, access_scope=access_scope,
                        validation_context=validation_context)
                    decision['arguments']['sql'] = prepared['sql']
                    decision['validated_query_contract'] = prepared['query_contract']
                    audit['validated_query_contract'] = prepared['query_contract']
                    if prepared['source_normalizations']:
                        audit['sql_source_normalizations'] = prepared['source_normalizations']
                else:
                    if decision['arguments'].get('sql') or decision.get('query_contract'):
                        raise ValueError('非数据库工具不能附带SQL或查询契约')
                    refund_non_sql()
                    audit['sql_generations'] = generation_ledger['used']
                audit["success"] = True
                attempts.append(audit)
                decision["validation_attempts"] = attempts
                return decision
            except PermissionError as exc:
                audit.update(error=str(exc), error_kind='permission', success=False)
                attempts.append(audit)
                raise PipelineStageError('query_permission', str(exc),
                    {'datasource_id': database, 'validation_attempts': attempts,
                     'error_kind': 'permission', 'repair_available': False}) from exc
            except ValueError as exc:
                error = str(exc)
                audit["error"] = error
                audit['repair_diagnostics'] = repair_diagnostics(error,
                    sql=str((decision.get('arguments') or {}).get('sql') or ''),
                    query_contract=decision.get('query_contract'), measurements=payload.get('measurement_requests'),
                    schema_graph=schema_graph)
                attempts.append(audit)
                signature = json.dumps({'error': error, 'action': decision.get('action'),
                    'arguments': decision.get('arguments'), 'query_contract': decision.get('query_contract'),
                    'step_decision': decision.get('step_decision')}, ensure_ascii=False, sort_keys=True)
                if signature == failed_signature:
                    raise PipelineStageError('query_contract_validation', f'查询前校验失败（相同错误与相关产物未变化，停止重复修复）：{error}',
                        {'datasource_id': database, 'validation_attempts': attempts, 'no_progress': True}) from exc
                failed_signature = signature
                if attempt >= generation_ledger['limit'] - 1 or generation_ledger['used'] >= generation_ledger['limit']:
                    raise PipelineStageError("query_contract_validation", f"查询前校验失败（已达{generation_ledger['limit']}次生成上限）：{error}",
                        {"datasource_id": database, "validation_attempts": attempts}) from exc
        raise PipelineStageError("query_contract_validation", f"查询前校验失败：{error}",
                                 {"datasource_id": database, "validation_attempts": attempts})

    @staticmethod
    def _compact_field(hit: dict[str, Any]) -> dict[str, Any]:
        """只传 SQL 决策需要的字段信息，避免重复发送索引全文。"""
        return {
            "table_id": hit.get("table_id"),
            "field_name": hit.get("field_name"),
            "field_label": hit.get("field_label"),
            "aliases": list(hit.get("aliases") or [])[:5],
            "samples": list(hit.get("samples") or [])[:5],
            "score": hit.get("score"),
        }
