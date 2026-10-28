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
from .query_contract import QueryContractValidator
from .metric_sources import normalize_source_clarification
from .. import homenova
from ..task_decisions import StepDecision, STEP_PROTOCOL_INSTRUCTION, validate_step_decision
from ..result_references import model_result_context


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
        self.contract_validator = QueryContractValidator(database_engine)
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

        system = f"你是单数据库问数智能体。\n\n{self.skill.instructions}"
        system += ("\nquery_contract的dimensions、time_range.field、filters.field和每个source_fields条目，"
                   "只能使用Schema真实物理列的table.physical_field形式，表名为物理表名而非别名。"
                   "不能填写SELECT别名（例如month/month_start）、CTE列或SQL表达式。Schema aggregation=none是未登记默认聚合，不是禁止聚合；数值metric按实际SQL声明sum/avg。原始计量使用真实字段名，definition可省略，由程序读取Schema描述。按月分组时grain=month，"
                   "dimensions列出参与月分组的原始时间列；SQL可用DATE_TRUNC并自由命名输出列。"
                   "同理派生指标的source_fields列底层真实列，不填计算结果别名。")
        system += ("\n所有数据源的time_range/time_ranges统一：start包含起点，end默认不包含终点，必须填写end_inclusive=false。"
                   "查完整自然月应从当月1日至下月1日前。日期工具end_date包含末日，排他边界直接使用它的end_exclusive，不能原样复制end_date。"
                   "明确声明end_inclusive=true的日期表示包含末日全天；代码会规范为次日排他边界。DATE列<=末日可与<次日等价，TIMESTAMP<=末日零点不等于包含全天。")
        if homenova.is_homenova(database):
            system += ("\nHomeNova当前公开登记的指标按id、label或aliases识别，业务指标名称不等于物理列名。"
                       "original_question由服务端绑定用户原文，模型不负责改写该字段。"
                       "DuckDB日期格式化使用strftime，日期截断使用date_trunc，不支持TO_CHAR。"
                       "可使用符合实际SQL的sum/count/count_distinct等聚合或derived；业务指标name选择registered_metrics中的id或可识别名称；definition可省略，由程序从注册信息绑定，不能用件数冒充金额。目标差额、变化量等只作为基于已登记指标的SQL展示计算，不另造业务指标定义；"
                       "source_fields列实际SQL引用的相关底层字段，不能捏造列。登记definition中的成功支付、"
                       "完成退款等条件是各指标分量自身的口径；遵循component_policies分别过滤再汇总，"
                       "不能把各分量条件混成同一全局WHERE；实际筛选需写入contract.filters。多事件时间可用可选time_ranges数组声明所有分量的时间边界（每项field/start/end），也可在filters逐项声明各事件的>=与<边界；不能只声明支付时间却遗漏其他事件的实际筛选。"
                       "使用场景时间，SQL禁止CURRENT_DATE/NOW等机器时钟。表不加库名，TIMESTAMPTZ按Europe/Berlin统计。"
                       "金额欧分展示EUR除以100。公开注册公式不是预制SQL或结果，请自行生成查询。")
        system += ("\nquery是本步目标，task_context.task_question是任务原文。只完成本步，不把任务全部挤入一条SQL。"
                   "继承task_context.conditions的时间/范围/口径，依赖参数只用真实dependency_results。task_context.required_output_fields列出了下游必须读取的列，SQL SELECT必须用这些精确别名输出所需数据。"
                   "query_contract.original_question由服务端绑定本步目标。只返回Skill要求的JSON。")
        system += ("\n若需澄清同名原始字段的来源，在每个选项填写metric_sources，格式为字段公开名称或别名到真实table.field的映射；"
                   "例如{\"metric_sources\":{\"字段名称\":\"物理表.字段\"}}。复合业务定义、目标值等其他澄清不填此字段。"
                   "用户已确认的来源见confirmed_metric_sources；旧选项的补充答复见clarification_history，不得重复询问已确认信息。")
        if (task_context or {}).get('step_protocol'):
            system += (STEP_PROTOCOL_INSTRUCTION + '\n查询步骤适用时返回原有action=call_tool及SQL/契约，并附step_decision。'
                       '不适用时返回{"action":"step_decision","step_decision":...}，不附SQL或工具。'
                       + json.dumps(StepDecision.model_json_schema(), ensure_ascii=False))
        original_question = query if task_context and task_context.get("step_id") else (original_query or query)

        observations: list[dict[str, Any]] = []
        tool_trace: list[dict[str, Any]] = []
        validation_attempts: list[dict[str, Any]] = []
        base_payload = {
            "query": query,
            "task_context": task_context or {},
            "original_question": original_question,
            "database": database,
            "schema_graph": schema_graph,
            "retrieval": {
                "threshold": retrieval.get("threshold"),
                "selected_fields": [
                    self._compact_field(hit) for hit in retrieval.get("hits", [])
                ],
                "low_confidence_candidates": [
                    self._compact_field(hit)
                    for hit in list(retrieval.get("low_confidence_candidates") or [])[:5]
                ],
            },
            "confirmed_fields": workspace.get("schema_fields", []),
            "confirmed_parameters": workspace.get("confirmed_parameters", {}),
            "confirmed_metric_sources": workspace.get("confirmed_metric_sources") or {},
            "clarification_history": workspace.get("clarification_history") or [],
            "schema_text": schema_context,
            "scenario": workspace.get("scenario") or {},
            "registered_metrics": workspace.get("selected_semantics") or [],
            "mcp_tools": tools,
        }
        if homenova.is_homenova(database):
            # The graph already contains these fields; avoid repeating its full
            # descriptions in schema_text and again in retrieval.selected_fields.
            base_payload.pop("schema_text", None)
            base_payload["retrieval"] = {
                "threshold": retrieval.get("threshold"),
                "hybrid_selected_count": retrieval.get("hybrid_selected_count"),
                "dependency_added_count": retrieval.get("dependency_added_count"),
            }
            base_payload["registered_metrics"] = [{
                "id": metric["id"], "label": metric["label"], "aliases": metric.get("aliases", []),
                "definition": metric["definition"], "formula": metric["formula"], "depends_on": metric.get("depends_on", []),
                "required_fields": [item["table"] + "." + item["field"] for item in metric["required_fields"]],
                "required_predicates": metric.get("required_predicates", []),
                "component_policies": metric.get("component_policies", []),
                "time_fields": metric.get("time_fields", []), "grain": metric.get("grain"),
                "unit": metric.get("unit"), "tax_policy": metric.get("tax_policy"),
                "shipping_policy": metric.get("shipping_policy"), "refund_policy": metric.get("refund_policy"),
                "maturity": metric.get("maturity"), "duplicate_risks": metric.get("duplicate_risks", []),
            } for metric in workspace.get("selected_semantics") or []]
            base_payload["term_notes"] = homenova.public_term_notes(database, query)

        try:
            for call_index in range(1, self.max_tool_calls + 1):
                payload = {**base_payload, "tool_results": observations}
                decision = self._validated_decision(
                    system, payload, tool_names, database_tool, original_question,
                    database, schema_graph, access_scope, validation_context={
                        "task_id": workspace.get("task_id") or (current_budget.get().task_id if current_budget.get() else None),
                        "confirmed_metric_sources": workspace.get("confirmed_metric_sources") or {},
                        "clarification_history": workspace.get("clarification_history") or [],
                    },
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
                    }
                tool_name = str(decision["tool_name"])
                arguments = decision["arguments"]

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
                    if not bool(tool_result.get("success")) and call_index < self.max_tool_calls:
                        budget = current_budget.get()
                        if budget:
                            budget.repair("SQL执行错误")
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
                    }

                observations.append({"tool": tool_name, "result": tool_result})

            raise ValueError(f"MCP工具调用超过上限：{self.max_tool_calls}")
        except BudgetStop as exc:
            exc.context = {**getattr(exc, "context", {}), "tool_trace": tool_trace,
                "validation_attempts": [*validation_attempts, *getattr(exc, "context", {}).get("validation_attempts", [])]}
            raise
        except PipelineStageError as exc:
            exc.context.setdefault("datasource_id", database)
            exc.context.setdefault("tool_trace", tool_trace)
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
                "validation_attempts": validation_attempts, "tool_trace": tool_trace}) from exc

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
    ) -> dict[str, Any]:
        """执行前最多让模型修正两次；无效SQL从不进入MCP。"""
        error = ""
        previous: dict[str, Any] | None = None
        attempts: list[dict[str, Any]] = []
        failed_signature = None
        for attempt in range(3):
            budget = current_budget.get()
            if budget:
                budget.model_node = "query_coder"
            if budget and attempt:
                try:
                    budget.repair("格式/查询契约/SQL修正")
                except BudgetStop as exc:
                    exc.context = {"validation_attempts": attempts}
                    raise
            request = dict(payload)
            if error:
                request["previous_invalid_output"] = previous
                request["output_validation_error"] = error
                request["output_validation_instruction"] = (
                    "查询前校验失败。保持原问题、指标和粒度；根据具体错误定向修复contract或SQL，保留无关已通过部分，不要重复相同无效产物。"
                    "若业务口径或必要信息无法确定则返回clarify。"
                )
            if budget:
                budget.skill(self.skill.name, self.skill.instructions, 'query_coder')
                for name, instructions in (payload.get('task_context') or {}).get('business_methods', {}).items():
                    budget.skill(name, instructions, 'query_coder')
            decision = self.model_client.chat_json(system, json.dumps(model_result_context(request), ensure_ascii=False))
            previous = decision
            action = str(decision.get("action") or "")
            audit = {"attempt": attempt + 1, "database": database, "action": action,
                     "step_decision": decision.get('step_decision'),
                     "tool_name": decision.get("tool_name"),
                     "sql": str((decision.get("arguments") or {}).get("sql") or "") if isinstance(decision.get("arguments"), dict) else "",
                     "query_contract": decision.get("query_contract"), "success": False}
            try:
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
                        attempts.append(audit)
                        decision['validation_attempts'] = attempts
                        return decision
                if action == "clarify":
                    clarification = decision.get("clarification")
                    if not isinstance(clarification, dict) or len(clarification.get("options") or []) < 2:
                        raise ValueError("智能体返回的澄清信息不完整")
                    decision['clarification'] = normalize_source_clarification(
                        clarification, database, schema_graph, access_scope)
                    audit["success"] = True
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
                    sql = str(decision["arguments"].get("sql") or "")
                    raw_contract = decision.get("query_contract")
                    if homenova.is_homenova(database) and isinstance(raw_contract, dict):
                        # The caller owns the original user message. Preserve
                        # the model's declaration in audit, never trust it as
                        # an authority or require it to parrot bytes correctly.
                        audit["model_declared_original_question"] = raw_contract.get("original_question")
                        raw_contract = {**raw_contract, "original_question": original_question}
                    contract = self.contract_validator.validate(
                        raw_contract, sql, original_question,
                        database, schema_graph, access_scope, validation_context=validation_context,
                    )
                    decision["validated_query_contract"] = contract
                    audit['validated_query_contract'] = contract
                    if budget:
                        budget.record('query_validation', normalizations=contract['validation']['normalizations'],
                                      pending_checks=contract['validation']['pending_checks'])
                audit["success"] = True
                attempts.append(audit)
                decision["validation_attempts"] = attempts
                return decision
            except ValueError as exc:
                error = str(exc)
                audit["error"] = error
                attempts.append(audit)
                signature = json.dumps({'error': error, 'action': decision.get('action'),
                    'arguments': decision.get('arguments'), 'query_contract': decision.get('query_contract'),
                    'step_decision': decision.get('step_decision')}, ensure_ascii=False, sort_keys=True)
                if signature == failed_signature:
                    raise PipelineStageError('query_contract_validation', f'查询前校验失败（相同错误与相关产物未变化，停止重复修复）：{error}',
                        {'datasource_id': database, 'validation_attempts': attempts, 'no_progress': True}) from exc
                failed_signature = signature
                if attempt == 2:
                    raise PipelineStageError("query_contract_validation", f"查询前校验失败（初次生成及2次额外修复后停止）：{error}",
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
