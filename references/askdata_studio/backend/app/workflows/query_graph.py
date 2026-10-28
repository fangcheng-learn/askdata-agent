from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from threading import RLock

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..config import Settings, settings
from ..database import SCHEMA, physical_table_name
from ..errors import PipelineStageError
from ..mcp_runtime import LocalMcpClient, create_local_mcp_server
from ..model_client import ModelClient
from ..models import QueryResult
from ..preprocessing import RequestPreprocessor
from ..querying.duckdb_engine import DuckDbEngine
from ..querying.data_qa_agent import DataQaAgent
from ..querying.models import SqlExecution
from ..querying.response_generator import ResponseGenerator
from ..querying.single_database_agent import SingleDatabaseAgent
from ..querying.metric_sources import normalize_source_clarification
from ..retrieval import SchemaGraphBuilder, SchemaIndex
from ..retrieval.homenova_index import HomeNovaSchemaIndex
from ..security import AccessScope
from .. import homenova
from ..skills import SkillRegistry
from .result_builder import ResultBuilder
from .state import QueryState
from .harness import InvestigationHarness
from ..runtime_budget import RunBudget, current_budget, BudgetStop


class QueryWorkflow:
    """基于 LangGraph 的问答和数据库查询工作流。"""

    def __init__(
        self,
        model_client: ModelClient,
        schema_index: SchemaIndex,
        config: Settings | None = None,
    ) -> None:
        self.model_client = model_client
        self.schema_index = schema_index
        self.home_indexes: dict[str, HomeNovaSchemaIndex] = {}
        self.config = config or settings
        route_model = self.config.route_model.strip()
        # Only the entry preprocessor changes model. Keep the same provider and
        # safety settings, and leave injected protocol clients untouched.
        self.route_model_client = (
            ModelClient(replace(model_client.config, llm_model=route_model, llm_enable_thinking=False))
            if route_model and isinstance(model_client, ModelClient) else model_client
        )
        self.preprocessor = RequestPreprocessor(self.route_model_client)
        self.skills = SkillRegistry()
        self.graph_builder = SchemaGraphBuilder()
        self.database_engine = DuckDbEngine()
        self.single_database_agent = SingleDatabaseAgent(
            model_client,
            self.mcp_client,
            self.skills.get("database_query"),
            self.database_engine,
            self.config.mcp_max_tool_calls,
        )
        self.response_generator = ResponseGenerator(
            model_client,
            self.config,
        )
        self.data_qa_agent = DataQaAgent(
            model_client,
            self.mcp_client,
            self.skills.get("data_qa"),
        )
        self.budgets: dict[str, RunBudget] = {}
        self.task_locks: dict[str, Any] = {}
        self.harness = InvestigationHarness(self)
        self.checkpointer = InMemorySaver()
        self.graph = self._compile()

    def mcp_client(self, access_scope: dict[str, Any]) -> LocalMcpClient:
        scope = AccessScope.from_dict(access_scope)
        return LocalMcpClient(create_local_mcp_server(self.database_engine, scope))

    def home_index(self, datasource_id: str) -> HomeNovaSchemaIndex:
        if datasource_id not in self.home_indexes:
            self.home_indexes[datasource_id] = HomeNovaSchemaIndex(datasource_id, self.model_client, self.config)
        return self.home_indexes[datasource_id]

    def _compile(self):
        builder = StateGraph(QueryState)
        builder.add_node("preprocess", self._preprocess)
        builder.add_node("investigate", self._investigate)
        builder.add_node("respond_directly", self._respond_directly)
        builder.add_node("answer_qa", self._answer_qa)
        builder.add_node("retrieve_schema", self._retrieve_schema)
        builder.add_node("human_clarification", self._human_clarification)
        builder.add_node("prepare_single_database", self._prepare_single_database)
        builder.add_node("execute_single_database", self._execute_single_database)
        builder.add_node("run_multi_database", self._run_multi_database)
        builder.add_edge(START, "preprocess")
        builder.add_conditional_edges(
            "preprocess",
            lambda state: "investigate" if state.get("needs_planner") else {
                "direct_response": "respond_directly",
                "data_qa": "answer_qa",
                "database_query": "retrieve_schema",
            }[state["intent"]["action"]],
            {
                "investigate": "investigate",
                "respond_directly": "respond_directly",
                "answer_qa": "answer_qa",
                "retrieve_schema": "retrieve_schema",
            },
        )
        builder.add_edge("investigate", END)
        builder.add_edge("respond_directly", END)
        builder.add_edge("answer_qa", END)
        builder.add_conditional_edges(
            "retrieve_schema",
            self._after_retrieval,
            {
                "end": END,
                "human_clarification": "human_clarification",
                "prepare_single_database": "prepare_single_database",
                "run_multi_database": "run_multi_database",
            },
        )
        builder.add_edge("human_clarification", "retrieve_schema")
        builder.add_conditional_edges(
            "prepare_single_database",
            lambda state: "human_clarification" if state.get("clarification") else "execute_single_database",
            {
                "human_clarification": "human_clarification",
                "execute_single_database": "execute_single_database",
            },
        )
        builder.add_edge("execute_single_database", END)
        builder.add_conditional_edges(
            "run_multi_database",
            lambda state: "human_clarification" if state.get("clarification") else "end",
            {"human_clarification": "human_clarification", "end": END},
        )
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def run_config(task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}}

    def invoke(self, payload: QueryState | Command, task_id: str) -> QueryResult:
        return self._bounded_invoke(task_id, lambda: self._state_result(
            self.graph.invoke(payload, config=self.run_config(task_id)), task_id))

    def _bounded_invoke(self, task_id: str, action) -> QueryResult:
        with self.task_locks.setdefault(task_id, RLock()):
            return self._invoke_locked(task_id, action)

    def _invoke_locked(self, task_id: str, action) -> QueryResult:
        budget = self.budgets.setdefault(task_id, RunBudget(task_id,
            tool_limit=self.config.harness_tool_limit, repair_limit=self.config.harness_repair_limit,
            replan_limit=self.config.harness_replan_limit, seconds=self.config.harness_seconds,
            investigation_seconds=self.config.harness_investigation_seconds))
        token = current_budget.set(budget)
        budget.start()
        try:
            result = action()
        except BudgetStop as exc:
            snapshot = self.graph.get_state(self.run_config(task_id)).values
            raw = snapshot.get("mcp_execution") or {}
            result = QueryResult(task_id=task_id, status="partial" if raw.get("success") else "failed", route="database_query",
                message="预算停止（尚未完成审核）", analysis="尚未完成交付审核。" + str(exc), stop_reason=str(exc),
                workflow_mode=snapshot.get("workflow_mode"),
                execution_log=[*snapshot.get("execution_log", []), {"stage": "budget_stop", "success": False, "error": str(exc), **getattr(exc, "context", {})}],
                sql=raw.get("sql"), rows=raw.get("rows") or [], columns=raw.get("columns") or [],
                total_rows=raw.get("total_rows"), preview_truncated=raw.get("preview_truncated", False),
                complete=raw.get("complete", True), result_ref=raw.get("result_ref"))
        finally:
            budget.pause()
            current_budget.reset(token)
        result.budget = budget.public()
        return result

    def resume_harness(self, task_id: str, response: str) -> QueryResult:
        task = self.harness.records[task_id]
        return self._bounded_invoke(task_id, lambda: self.harness.run(task.state, response))

    def _investigate(self, state: QueryState) -> dict[str, Any]:
        result = self.harness.run(state)
        return {"result": result.model_dump(mode="json")}

    def _state_result(self, state: dict[str, Any], task_id: str) -> QueryResult:
        if state.get("result"):
            result = QueryResult.model_validate(state["result"])
            result.datasource_id = state.get("datasource_id") or None
            result.scenario = state.get("scenario") or None
            result.selected_semantics = list(state.get("selected_semantics") or [])
            if result.workflow_mode != "investigation_harness_v1":
                result.selected_skills = list(state.get("business_skills") or [])
            return result
        state = {**state, "task_id": state.get("task_id") or task_id}
        return ResultBuilder.waiting(state)

    def _preprocess(self, state: QueryState) -> dict[str, Any]:
        """生成路由、独立查询和 Schema 检索参数。"""
        route_context = state.get("route_context", "")
        if homenova.is_homenova(state.get("datasource_id")):
            route_context = homenova.scenario_text(state["datasource_id"]) + "\n" + route_context
        decision = self.preprocessor.prepare(state["query"], route_context)
        budget = current_budget.get()
        if budget:
            budget.record('preprocess_decision', route=decision.action, reason=decision.reason,
                          needs_planner=decision.needs_planner, selected_business_skills=decision.business_skills,
                          required_questions=decision.required_questions, standalone_query=decision.standalone_query)
        execution_log = list(state.get("execution_log") or [])
        if decision.source == "model_unavailable_fallback":
            execution_log.append({
                "stage": "route_fallback",
                "success": False,
                "source": decision.source,
                "action": decision.action,
                "reason": decision.reason,
            })
        return {
            "intent": {
                "action": decision.action,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "response_type": decision.response_type,
                "source": decision.source,
            },
            "direct_response": decision.response,
            "standalone_query": decision.standalone_query,
            "rewritten": decision.rewritten,
            "extraction": decision.retrieval.public(),
            "execution_log": execution_log,
            "needs_planner": decision.needs_planner,
            "business_skills": decision.business_skills,
            "required_questions": decision.required_questions,
        }

    def _business_methods(self, state: QueryState) -> dict[str, str]:
        return {name: self.skills.get(name).instructions for name in state.get("business_skills", [])
                if name in {"business_diagnosis", "goal_analysis", "inventory_risk_check"}}

    def _respond_directly(self, state: QueryState) -> dict[str, Any]:
        """返回预处理模型生成的普通回答或自然语言澄清。"""
        result = ResultBuilder.direct_response(
            state["task_id"],
            state.get("direct_response", ""),
            state["intent"],
            list(state.get("execution_log") or []),
        )
        return {
            "workflow_mode": result.workflow_mode,
            "result": result.model_dump(mode="json"),
        }

    def _answer_qa(self, state: QueryState) -> dict[str, Any]:
        qa_result = self.data_qa_agent.run(
            state["query"],
            {
                "scenario": state.get("scenario") or {},
                "business_methods": json.dumps(self._business_methods(state), ensure_ascii=False),
                "short_term": state.get("short_term_context", ""),
                "recent_result": state.get("recent_result_context", ""),
                "selected_tables": state.get("analysis_context", ""),
            },
            state.get("access_scope") or {},
        )
        result = ResultBuilder.qa(
            state["task_id"], qa_result, state["intent"], state.get("analysis_sources") or []
        )
        if homenova.is_homenova(state.get("datasource_id")):
            budget = current_budget.get()
            if budget:
                budget.phase = "closing"
                budget.model_node = "qa_delivery_review"
            review = self.model_client.chat_json(
                "你是已有结果问答的独立交付审核。检查草稿是否符合问题、来源SQL、时间/口径/单位和完整性。"
                "只返回JSON {valid:boolean, approved_text:string, reason:string}，approved_text是可直接展示的审核定稿。"
                "预览不能用于全集计算，不把缺结果当零，不夸大因果；来源query_validations的pending_checks需要结合SQL与数据核对，"
                "不能把基础检查通过当成业务正确；不可支持的部分明确说明。",
                json.dumps({"question": state["query"], "draft": qa_result.report.markdown if qa_result.report else qa_result.answer,
                    "scenario": state.get("scenario"), "recent_result": state.get("recent_result_context"),
                    "selected_results": state.get("analysis_context")}, ensure_ascii=False))
            approved = str(review.get("approved_text") or "").strip()
            if approved:
                result.analysis = approved
                if result.report:
                    result.report.markdown = approved
            result.final_reviewed = bool(review.get("valid")) and bool(approved)
            if not result.final_reviewed:
                result.status = "partial"
                result.message = "部分完成（已有结果审核未通过）"
                result.analysis = "尚未完成交付审核：" + str(review.get("reason") or "证据不足")
                result.report = None
        return {
            "workflow_mode": "qa_report" if qa_result.report else "qa",
            "result": result.model_dump(mode="json"),
        }

    def _retrieve_schema(self, state: QueryState, charge_budget: bool = True) -> dict[str, Any]:
        budget = current_budget.get()
        if budget:
            budget.model_node = "schema_retrieval"
            if charge_budget:
                budget.tool("schema_retrieval")
        standalone_query = state["standalone_query"]
        extraction = state.get("extraction") or {}
        datasource_id = state.get("datasource_id") or ""
        if homenova.is_homenova(datasource_id):
            workspace = state.get("workspace") or {}
            index = self.home_index(datasource_id)
            base = index.retrieve(standalone_query,
                                 retrieval_terms=list(extraction.get("retrieval_terms") or [standalone_query]),
                                 access_scope=state.get("access_scope"))
            base = index.include_workspace(base, workspace, state.get("access_scope"))
            retrieval = homenova.selected_retrieval(datasource_id, standalone_query, base, extraction,
                                                    list(workspace.get("schema_fields") or []))
            graph = self.graph_builder.build(retrieval["hits"], state.get("access_scope"))
            selected = retrieval["selected_semantics"]
            context = self.graph_builder.context_text(graph) + "\n" + homenova.scenario_text(datasource_id)
            context += "\n当前公开登记指标（definition是业务定义；其余为口径和依赖，不是查询答案）：\n" + json.dumps(selected, ensure_ascii=False)
            workspace = {**workspace, "selected_semantics": selected, "scenario": state.get("scenario") or {}}
            return {"retrieval": retrieval, "schema_graph": graph, "schema_context": context,
                    "database_names": [datasource_id], "selected_semantics": selected,
                    "workspace": workspace, "clarification": None, "direct_sql": ""}
        retrieval = self.schema_index.retrieve(
            standalone_query,
            retrieval_terms=list(extraction.get("retrieval_terms") or []),
            access_scope=state.get("access_scope"),
        )
        workspace = state.get("workspace") or {}
        query_workspace = {
            "schema_fields": list(workspace.get("schema_fields") or []),
            "confirmed_schema_tables": list(workspace.get("confirmed_schema_tables") or []),
            "confirmed_parameters": dict(workspace.get("confirmed_parameters") or {}),
        }
        retrieval = self.schema_index.include_workspace(
            retrieval,
            query_workspace,
            state.get("access_scope"),
        )
        retrieval["extraction"] = extraction
        selected_database = str(query_workspace["confirmed_parameters"].get("database") or "")
        if selected_database:
            scope = AccessScope.from_dict(state.get("access_scope"))
            if not scope.allows_database(selected_database):
                raise PipelineStageError("schema_access_control", "无权访问所选数据库")
            retrieval["hits"] = [
                hit for hit in retrieval["hits"]
                if hit.get("database_id") == selected_database
            ]
            retrieval["selected_count"] = len(retrieval["hits"])
            retrieval["table_candidates"] = [
                table for table in retrieval.get("table_candidates", [])
                if self.graph_builder.tables.get(str(table.get("table_id")), {}).get("database")
                == selected_database
            ]
            retrieval["low_confidence_candidates"] = [
                hit for hit in retrieval.get("low_confidence_candidates", [])
                if self.graph_builder.tables.get(str(hit.get("table_id")), {}).get("database")
                == selected_database
            ]
        if not retrieval["hits"]:
            result = ResultBuilder.schema_not_found(state, retrieval)
            return {
                "retrieval": retrieval,
                "execution_log": result.execution_log,
                "workflow_mode": result.workflow_mode,
                "result": result.model_dump(mode="json"),
            }
        schema_graph = self.graph_builder.build(
            retrieval["hits"],
            state.get("access_scope"),
            include_time_fields=bool(extraction.get("time_expressions")),
        )
        retrieval["schema_graph"] = schema_graph
        databases = sorted({
            str(table.get("database") or schema_graph.get("database") or "askdata_mock")
            for table in schema_graph.get("tables", [])
        })
        clarification = None
        if len(databases) > 1:
            names = {
                "ecommerce_ops": ("电商经营数据", "商品、订单和店铺经营数据"),
                "askdata_mock": ("示例销售数据", "演示用的地区销售订单"),
            }
            clarification = {
                "parameter": "database",
                "question": "这次要查询哪套数据？",
                "reason": "当前问题同时命中多个数据库；请先确定数据来源。",
                "options": [
                    {
                        "id": database,
                        "label": names.get(database, (database, ""))[0],
                        "description": names.get(database, (database, ""))[1],
                    }
                    for database in databases
                ],
            }
        return {
            "standalone_query": standalone_query,
            "extraction": extraction,
            "retrieval": retrieval,
            "schema_graph": schema_graph,
            "schema_context": self.graph_builder.context_text(schema_graph),
            "database_names": databases,
            "clarification": clarification,
            "direct_sql": "",
        }

    @staticmethod
    def _after_retrieval(state: QueryState) -> str:
        if state.get("result"):
            return "end"
        if state.get("clarification"):
            return "human_clarification"
        return "prepare_single_database" if len(state.get("database_names") or []) <= 1 else "run_multi_database"

    def _human_clarification(self, state: QueryState) -> dict[str, Any]:
        # LangGraph 将 Command(resume=...) 的值作为 interrupt 返回值。
        response = interrupt(state.get("clarification") or {})
        option_id = str(response.get("option_id") if isinstance(response, dict) else response)
        workspace = dict(state.get("workspace") or {})
        payload = state.get("clarification") or {}
        database = str(state.get('datasource_id') or (
            (state.get('database_names') or [''])[0] if len(state.get('database_names') or []) == 1 else ''))
        payload = normalize_source_clarification(
            payload, database, state.get('schema_graph') or {}, state.get('access_scope') or {})
        selected = next((option for option in payload.get('options') or [] if str(option.get('id')) == option_id), None)
        selected_sources = dict((selected or {}).get('metric_sources') or {})
        parameter = str(payload.get("parameter") or "other")
        tables = list(workspace.get("confirmed_schema_tables") or [])
        if any(table["id"] == option_id for table in SCHEMA):
            tables = list(dict.fromkeys([*tables, option_id]))
        selected_fields = list(workspace.get('schema_fields') or [])
        field_keys = {(field.get('tableId'), field.get('name')) for field in selected_fields}
        for reference in set(selected_sources.values()):
            source_table, source_field = reference.split('.', 1)
            definition = next(table for table in SCHEMA
                              if str(table.get('database') or 'askdata_mock') == database
                              and physical_table_name(table) == source_table)
            table_id = str(definition['id'])
            tables = list(dict.fromkeys([*tables, table_id]))
            if (table_id, source_field) not in field_keys:
                selected_fields.append({'tableId': table_id, 'name': source_field, 'aggregation': 'auto'})
                field_keys.add((table_id, source_field))
        workspace['schema_fields'] = selected_fields
        workspace["confirmed_schema_tables"] = tables
        workspace["confirmed_parameters"] = {
            **dict(workspace.get("confirmed_parameters") or {}),
            parameter: option_id,
        }
        prior = workspace.get('confirmed_metric_sources') or {}
        sources = dict(prior.get('sources') or {}) if (
            prior.get('task_id') == state.get('task_id') and prior.get('database') == database) else {}
        if selected_sources:
            sources.update(selected_sources)
            workspace['confirmed_metric_sources'] = {
                'database': database, 'task_id': state['task_id'], 'sources': sources}
        workspace['clarification_history'] = [*list(workspace.get('clarification_history') or []), {
            'task_id': state.get('task_id'), 'database': database,
            'question': str(payload.get('question') or ''), 'parameter': parameter,
            'option_id': option_id, 'label': str((selected or {}).get('label') or option_id),
            'metric_sources': selected_sources,
        }]
        return {"workspace": workspace, "clarification": None, "direct_sql": "", "result": {},
                "standalone_query": state["standalone_query"] + "；用户补充确认：" + parameter + "=" + option_id}

    def _prepare_single_database(self, state: QueryState) -> dict[str, Any]:
        workspace = dict(state.get("workspace") or {})
        workspace['task_id'] = state['task_id']
        database = (state.get("database_names") or ["askdata_mock"])[0]
        decision = self.single_database_agent.prepare(
            state["standalone_query"],
            database,
            state["schema_graph"],
            state["schema_context"],
            state["retrieval"],
            workspace,
            state.get("access_scope") or {},
            original_query=state["query"],
            task_context={"task_question": state["query"], "conditions": workspace.get("confirmed_parameters", {}),
                          "business_methods": self._business_methods(state)} if state.get("business_skills") else None,
        )
        if decision["action"] == "clarify":
            return {
                "workflow_mode": "single_database_agent",
                "clarification": decision["clarification"],
                "mcp_tool_trace": decision.get("tool_trace", []),
                "execution_log": [*state.get("execution_log", []),
                    {"stage": "query_contract_validation", "success": True,
                     "validation_attempts": decision.get("validation_attempts", [])}],
                "direct_sql": "",
            }
        execution = decision["execution"]
        return {
            "workflow_mode": "single_database_agent",
            "clarification": None,
            "mcp_execution": execution,
            "mcp_tool_trace": decision.get("tool_trace", []),
            "execution_log": [*state.get("execution_log", []),
                {"stage": "query_contract_validation", "success": True,
                 "validation_attempts": decision.get("validation_attempts", [])}],
            "direct_sql": str(execution.get("sql") or ""),
            "sql_source": decision.get("source", "model"),
        }

    def _execute_single_database(self, state: QueryState) -> dict[str, Any]:
        database = (state.get("database_names") or ["askdata_mock"])[0]
        raw_execution = state.get("mcp_execution") or {}
        execution = SqlExecution(
            sql=str(raw_execution.get("sql") or state.get("direct_sql") or ""),
            success=bool(raw_execution.get("success")),
            columns=list(raw_execution.get("columns") or []),
            rows=list(raw_execution.get("rows") or []),
            error=raw_execution.get("error"),
            total_rows=raw_execution.get("total_rows"),
            preview_truncated=raw_execution.get("preview_truncated", False),
            complete=raw_execution.get("complete", True), result_ref=raw_execution.get("result_ref"),
        )
        trace = list(state.get("mcp_tool_trace") or [])
        log = [*state.get("execution_log", []), *[
            {
                "stage": "mcp_tool_call",
                "success": not bool(item.get("result", {}).get("error")),
                **item,
            }
            for item in trace
        ]]
        log.append({
            "stage": "execute_duckdb",
            "success": execution.success,
            "error": execution.error,
            "via": "mcp",
        })
        database_call = next(
            (item for item in reversed(trace) if item.get("tool") == f"query_{database}"),
            {},
        )
        call = {
            "call_index": int(database_call.get("call_index") or 1),
            "database": database,
            "arguments": {
                "mode": "single_database_agent",
                "transport": "mcp_in_process",
                "tool_name": database_call.get("tool"),
                "schema_graph_version": state.get("schema_graph", {}).get("graph_version"),
                "sql_source": state.get("sql_source", "model"),
                "query_contract": database_call.get("query_contract"),
            },
            "sql": execution.sql,
            "success": execution.success,
            "row_count": len(execution.rows),
            "error": execution.error,
        }
        if not execution.success:
            result = ResultBuilder.failed(state, execution, log)
            return {"execution_log": log, "tool_calls": [call], "result": result.model_dump(mode="json")}
        analysis_failed = False
        try:
            final = self.response_generator.finalize(
                state["standalone_query"], execution, state["schema_context"] + "\n选中的业务方法：" + json.dumps(self._business_methods(state), ensure_ascii=False),
                state.get("analysis_context", ""),
                business_methods=self._business_methods(state),
                query_validation=(database_call.get('query_contract') or {}).get('validation', {}),
            )
        except PipelineStageError as exc:
            # 保留已成功执行的 SQL 和数据，但整个问答任务尚未完成。
            analysis_failed = True
            log.append({"stage": exc.stage, "success": False, "error": exc.message})
            final = {
                "valid": False,
                "reason": f"{exc.stage}失败",
                "title": "查询结果（文字说明生成失败）",
                "analysis": f"SQL已成功执行，但{exc.stage}失败：{exc.message}",
            }
        result = ResultBuilder.completed(state, [execution], execution, final, [call], log)
        result.final_reviewed = bool(final.get("final_reviewed"))
        if not result.final_reviewed and not analysis_failed:
            result.status = "partial"
            result.message = "部分完成（交付审核未通过）"
            result.analysis = "交付审核未通过：" + str(final.get("reason") or "证据不足") + "。结构化结果仅供核对。"
        if not execution.rows and result.final_reviewed:
            result.message = "查询完成，但无匹配数据"
        result.total_rows = execution.total_rows
        result.preview_truncated = execution.preview_truncated
        result.complete = execution.complete
        result.result_ref = execution.result_ref
        if analysis_failed:
            result.message = "结果说明生成失败"
        return {"execution_log": log, "tool_calls": [call], "result": result.model_dump(mode="json")}

    def _run_multi_database(self, state: QueryState) -> dict[str, Any]:
        """返回尚未实现的多数据库查询结果。"""
        failure = SqlExecution(
            sql="",
            success=False,
            error="当前仅支持单库直接查询；多库 Handoff 尚未启用。",
        )
        result = ResultBuilder.failed(state, failure, [])
        return {"workflow_mode": "multi_database_pending", "result": result.model_dump(mode="json")}
