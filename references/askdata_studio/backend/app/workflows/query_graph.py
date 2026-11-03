from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
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
from ..models import AnalysisReport, QueryResult
from ..preprocessing import RequestPreprocessor
from ..querying.duckdb_engine import DuckDbEngine
from ..querying.data_qa_agent import DataQaAgent
from ..querying.analysis_evidence import load_analysis_evidence, model_analysis_context
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
from .react_harness import ReactInvestigationHarness
from ..services.investigation_store import InvestigationStore
from ..runtime_budget import RunBudget, current_budget, BudgetStop
from ..agent_configuration_runtime import (configuration_snapshot, current_configuration, default_snapshot)


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
        self.investigation_store = InvestigationStore(
            Path(self.config.harness_state_path), max_bytes=self.config.harness_state_max_bytes)
        self.database_engine.result_store = self.investigation_store
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
        self.configuration_snapshots: dict[str, dict] = {}
        self.legacy_harness = InvestigationHarness(self)
        self.harness = ReactInvestigationHarness(self, self.investigation_store) if self.config.harness_react_enabled else self.legacy_harness
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
        builder.add_node("metadata_query", self._metadata_query)
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
                "metadata_query": "metadata_query",
                "direct_response": "respond_directly",
                "data_qa": "answer_qa",
                "database_query": "retrieve_schema",
            }[state["intent"]["action"]],
            {
                "metadata_query": "metadata_query",
                "investigate": "investigate",
                "respond_directly": "respond_directly",
                "answer_qa": "answer_qa",
                "retrieve_schema": "retrieve_schema",
            },
        )
        builder.add_edge("metadata_query", END)
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
        if task_id not in self.budgets:
            self.budgets[task_id] = RunBudget(task_id,
                tool_limit=self.config.harness_tool_limit, repair_limit=self.config.harness_repair_limit,
                replan_limit=self.config.harness_replan_limit, seconds=self.config.harness_seconds,
                investigation_seconds=self.config.harness_investigation_seconds,
                react_round_limit=self.config.harness_react_round_limit,
                sql_generation_limit=self.config.harness_sql_generation_limit)
        budget = self.budgets[task_id]
        record = self.harness.records.get(task_id) if isinstance(self.harness, ReactInvestigationHarness) else None
        if record is not None:
            snapshot = record.configuration_snapshot if record.configuration_snapshot is not None else default_snapshot()
        else:
            if task_id not in self.configuration_snapshots:
                self.configuration_snapshots[task_id] = configuration_snapshot()
            snapshot = self.configuration_snapshots[task_id]
        configuration_token = current_configuration.set(snapshot)
        token = current_budget.set(budget)
        budget.start()
        try:
            result = action()
        except BudgetStop as exc:
            snapshot = self.graph.get_state(self.run_config(task_id)).values
            raw = snapshot.get("mcp_execution") or {}
            stop_context = getattr(exc, 'context', {})
            trace = stop_context.get('tool_trace') or []
            # SQL-generation exhaustion can occur before prepare commits its
            # graph state. Preserve the last real execution and diagnostic;
            # stopping a third generation must not erase the first two.
            if not raw:
                raw = next((item.get('result') or {} for item in reversed(trace)
                            if str(item.get('tool') or '').startswith('query_')), {})
            stop_log = [*snapshot.get('execution_log', []), *[
                {'stage': 'mcp_tool_call', 'success': bool(item.get('result', {}).get('success')), **item}
                for item in trace]]
            detail = '尚未完成交付审核。' + str(exc)
            if raw.get('error'):
                detail += '；最后一次SQL执行错误：' + str(raw['error'])
            result = QueryResult(task_id=task_id, status="partial" if raw.get("success") else "failed", route="database_query",
                message="预算停止（尚未完成审核）", analysis=detail, stop_reason=str(exc),
                workflow_mode=snapshot.get("workflow_mode"),
                execution_log=[*stop_log, {"stage": "budget_stop", "success": False, "error": str(exc), **stop_context}],
                sql=raw.get("sql"), rows=raw.get("rows") or [], columns=raw.get("columns") or [],
                total_rows=raw.get("total_rows"), preview_truncated=raw.get("preview_truncated", False),
                complete=raw.get("complete", True), result_ref=raw.get("result_ref"))
        finally:
            try:
                budget.pause()
                budget.checkpoint()
            finally:
                current_budget.reset(token)
                current_configuration.reset(configuration_token)
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
            result.datasource_id = state.get("datasource_id") or result.datasource_id
            result.scenario = state.get("scenario") or None
            if result.workflow_mode != 'investigation_react_v1':
                result.selected_semantics = list(state.get("selected_semantics") or [])
            if result.workflow_mode not in {"investigation_harness_v1", "investigation_react_v1"}:
                result.selected_skills = list(state.get("business_skills") or [])
            return result
        state = {**state, "task_id": state.get("task_id") or task_id}
        return ResultBuilder.waiting(state)

    def _preprocess(self, state: QueryState) -> dict[str, Any]:
        """生成路由、独立查询和 Schema 检索参数。"""
        route_context = state.get("route_context", "")
        budget = current_budget.get()
        zone = 'Europe/Berlin' if homenova.is_homenova(state.get('datasource_id')) else 'Asia/Shanghai'
        reference = (budget.reference_now if budget and budget.reference_now else datetime.now(ZoneInfo(zone)).isoformat(timespec='seconds'))
        if budget:
            budget.reference_now = reference
        time_context = {'reference_now': reference, 'timezone': zone, 'rule': 'real_current_time',
                        'recent_without_duration': 'clarify_unless_context_has_period'}
        route_context = '本任务真实时间参考：' + json.dumps(time_context, ensure_ascii=False) + '\n' + route_context
        if homenova.is_homenova(state.get("datasource_id")):
            route_context = homenova.scenario_text(state["datasource_id"]) + "\n" + route_context
        decision = self.preprocessor.prepare(state["query"], route_context)
        execution_log = list(state.get("execution_log") or [])
        if decision.source == "model_unavailable_fallback":
            execution_log.append({
                "stage": "route_fallback",
                "success": False,
                "source": decision.source,
                "action": decision.action,
                "reason": decision.reason,
            })
        selected_context = {}
        if decision.action == 'data_qa' and decision.analysis_task_ids and not state.get('analysis_context'):
            candidates = {item['task_id']: item for item in json.loads(state.get('recent_result_candidates') or '[]')}
            if all(task_id in candidates for task_id in decision.analysis_task_ids):
                selected = [candidates[task_id] for task_id in decision.analysis_task_ids]
                selected_context = {'analysis_context': json.dumps(selected, ensure_ascii=False),
                    'analysis_sources': [{k: item.get(k) for k in ('task_id', 'title', 'row_count', 'columns')}
                                         for item in selected]}
                execution_log.append({'stage': 'analysis_source_selection', 'success': True,
                                      'source_task_ids': decision.analysis_task_ids})
            else:
                # Never replace a missing requested source with an unrelated
                # latest table or let a model-supplied ID read another session.
                decision.action = 'direct_response'
                decision.response_type = 'clarification'
                decision.response = '我没有找到这次追问所指的可用历史结果，请在参考结果中选择要继续分析的那份数据。'
                decision.needs_planner = True
                decision.required_questions = [state['query']]
                decision.task_understanding = {**decision.task_understanding,
                    'goal': state['query'], 'questions': [state['query']]}
                execution_log.append({'stage': 'analysis_source_selection', 'success': False,
                                      'error': 'requested_source_unavailable'})
        if decision.response_type == 'clarification' and not state.get('datasource_id'):
            # The legacy, unbound entry has no authenticated datasource identity
            # for a durable investigation. Keep its existing intent/source
            # clarification reply; never invent a source to satisfy storage.
            # Same-task investigation resume is guaranteed only after binding.
            decision.action = 'direct_response'
            decision.needs_planner = False
            execution_log.append({'stage': 'entry_clarification', 'success': True,
                'mode': 'legacy_unbound', 'durable_investigation': False,
                'reason': '尚未绑定数据源，保留原入口澄清回复；未创建可恢复的经营调查'})
        budget = current_budget.get()
        if budget:
            budget.record('preprocess_decision', route=decision.action, reason=decision.reason,
                          needs_planner=decision.needs_planner, selected_business_skills=decision.business_skills,
                          required_questions=decision.required_questions, standalone_query=decision.standalone_query,
                          analysis_mode=decision.analysis_mode, analysis_task_ids=decision.analysis_task_ids,
                          analysis_result_ids=decision.analysis_result_ids)
        return {
            **selected_context,
            "analysis_result_ids": decision.analysis_result_ids if decision.action == 'data_qa' else [],
            "analysis_mode": decision.analysis_mode,
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
            "business_skills": [] if self.config.harness_react_enabled else decision.business_skills,
            "required_questions": decision.required_questions,
            "task_understanding": decision.task_understanding,
            "time_context": time_context,
        }

    def _metadata_query(self, state: QueryState):
        scope = AccessScope.from_dict(state['access_scope'])
        database = state.get('datasource_id')
        tables = []
        for table in SCHEMA:
            source = table.get('database', 'askdata_mock')
            if (database and source != database) or not scope.allows_table(source, table['id']):
                continue
            tables.append({'name': physical_table_name(table), 'datasource_id': source,
                'description': table.get('description') or table.get('label') or '',
                'fields': [{'name': column['name'],
                    'type': column.get('sql_type') or column.get('type') or '',
                    'description': column.get('description') or column.get('label') or ''}
                    for column in table.get('fields', [])]})
        catalog = {'datasource_id': database, 'complete': True, 'table_count': len(tables), 'tables': tables}
        # Entry Agent selected the metadata capability; the existing DataQa
        # Agent owns the answer. No keyword branching or fixed prose/table dump.
        answer = self.data_qa_agent.run(state['query'], {
            'mode': 'metadata', 'authorized_metadata': catalog,
            'short_term': state.get('short_term_context', '')}, state['access_scope'])
        result = QueryResult(task_id=state['task_id'], status='completed', route='data_qa',
            message='已读取授权元数据并回答', analysis=answer.answer, workflow_mode='metadata_read',
            execution_log=[{'stage': 'authorized_metadata_read', 'success': True,
                            'table_count': len(tables), 'catalog': catalog},
                           {'stage': 'metadata_agent_answer', 'success': True}])
        return {'result': result.model_dump(mode='json')}

    def _business_methods(self, state: QueryState) -> dict[str, str]:
        return {name: self.skills.get(name).instructions for name in state.get("business_skills", [])
                if name in {"business_diagnosis", "goal_analysis", "inventory_risk_check"}}

    def _respond_directly(self, state: QueryState) -> dict[str, Any]:
        """返回预处理模型生成的普通回答或自然语言澄清。"""
        if state.get('datasource_id') and (state.get('intent') or {}).get('response_type') == 'clarification':
            # Defensive boundary for server-created clarification decisions as
            # well as old/injected router outputs with a bound data source.
            # Only the main agent owns a resumable investigation clarification;
            # the unbound legacy path remains an entry reply, not such a task.
            return self._investigate(state)
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
        bundle = load_analysis_evidence(state, self.database_engine)
        managed = bundle['managed']
        contexts = ({
            'scenario': state.get('scenario') or {},
            'business_methods': json.dumps(self._business_methods(state), ensure_ascii=False),
            'short_term': state.get('short_term_context', ''),
            'evidence': bundle['evidence'], 'source_tasks': bundle['source_tasks'],
            'prior_reports': bundle['prior_reports'],
            **({'report_reference_closure': bundle['report_reference_closure']}
               if bundle['report_reference_closure'] else {}),
            'inherited_evidence_gaps': bundle['evidence_gaps'],
            'source_gaps': bundle['source_gaps'], 'data_gaps': bundle['data_gaps'],
        } if managed else {
            "scenario": state.get("scenario") or {},
            "business_methods": json.dumps(self._business_methods(state), ensure_ascii=False),
            "short_term": state.get("short_term_context", ""),
            "recent_result": state.get("recent_result_context", ""),
            "selected_tables": state.get("analysis_context", ""),
        })
        from ..querying.analysis_evidence import focus_qa_context
        contexts = focus_qa_context(contexts, state.get('analysis_mode', 'explanation'))
        report_scope = bundle['report_scope'] if managed else state.get('access_scope') or {}
        qa_result = self.data_qa_agent.run(
            state['query'], contexts, report_scope,
        )
        result = ResultBuilder.qa(
            state["task_id"], qa_result, state["intent"], state.get("analysis_sources") or []
        )
        result.execution_log = [*state.get('execution_log', []), *result.execution_log]
        if managed:
            result.datasource_id = bundle['report_scope']['datasource_id'] or None
            result.execution_log.append({'stage': 'existing_evidence_reuse',
                'success': True, 'analysis_mode': state.get('analysis_mode', 'explanation'), 'source_tasks': bundle['source_tasks'],
                'warnings': qa_result.warnings, 'diagnostics': qa_result.diagnostics,
                'evidence_ids': [item['id'] for item in bundle['evidence']],
                **({'report_reference_closure': bundle['report_reference_closure']}
                   if bundle['report_reference_closure'] else {}),
                'new_sql_calls': 0})
            # Keep original result/task/version identities for the next turn;
            # the API preview remains separate from the rows used in this answer.
            result.evidence = [{**item, 'rows': item['rows'][:20],
                                'display_truncated': len(item['rows']) > 20 or item.get('display_truncated', False)}
                               for item in bundle['evidence']]
            result.analysis_sources = [{key: item.get(key) for key in
                ('id', 'task_id', 'datasource_id', 'version', 'goal', 'columns', 'total_rows', 'complete')}
                for item in bundle['evidence']]
            if qa_result.evidence_gaps:
                result.status = 'partial'
                result.message = '已整理已有证据；原调查仍有未解决缺口'
                result.stop_reason = '；'.join(qa_result.evidence_gaps)
        skip_content_review = bool(qa_result.report) and not getattr(getattr(self, 'config', None), 'report_content_review_enabled', True)
        if skip_content_review:
            result.content_review_status = 'skipped'
            result.final_reviewed = False
            result.execution_log.append({'stage': 'content_review_skipped', 'reason': 'Independent content review disabled by configuration'})
        if homenova.is_homenova(state.get("datasource_id")) and not skip_content_review:
            budget = current_budget.get()
            if budget:
                budget.phase = "closing"
                budget.model_node = "qa_delivery_review"
            from ..querying.analysis_output import analysis_schema, resolve_analysis_review
            from ..structured_output import using_output_schema
            draft = qa_result.report.markdown if qa_result.report else qa_result.answer
            review_evidence = model_analysis_context(bundle['evidence']) if managed else []
            review_context = {"question": state["query"], "analysis_mode": state.get("analysis_mode", "explanation"),
                "draft": draft, "scenario": state.get("scenario"),
                **({'evidence': review_evidence, 'source_tasks': contexts['source_tasks'],
                    'trust_boundary': contexts['trust_boundary'], 'evidence_gaps': qa_result.evidence_gaps,
                    'report_metadata': qa_result.report.model_dump(mode='json', exclude={'data_sources', 'markdown'}) if qa_result.report else None}
                   if managed else {'recent_result': state.get('recent_result_context'),
                                    'selected_results': state.get('analysis_context')})}
            review_payload = json.dumps(review_context, ensure_ascii=False)
            allowed_evidence_ids = {item['id'] for item in review_evidence}
            with using_output_schema('existing_evidence_review', analysis_schema(review=True, evidence_ids=list(allowed_evidence_ids))):
                review = self.model_client.chat_json(
                    "你是已有结果问答的独立交付审核。检查草稿是否符合问题、来源SQL、时间/口径/单位和完整性。"
                    "只返回给定Schema的JSON。approved_text必须为空，禁止重写整份报告。原稿无需修改时approve_draft_unchanged=true、text_edits=[]。"
                    "audit_findings覆盖metric_scope、calculation_method、claim_strength、capability、answer_coverage，简短说明具体核对依据；"
                    "每个具体问题分别给唯一finding_id，即使criterion相同。纯措辞修订用精确text_edits，finding用text_edit_indices绑定自己的零基补丁索引，不能跨问题共用。"
                    "supported或missing_evidence的text_edit_indices=[]、resolved_in_final=false。correction_required仅在自己的措辞补丁能修好时resolved_in_final=true；"
                    "数字、单位或计算方法错误，当前请求必需但本轮未取得的证据，不能用文字补丁解决，应valid=false并具体说明。"
                    "evidence_ids只能引用提供的结果；context_refs仅可用current_question、evidence_metadata、evidence_gaps、draft、trust_boundary。"
                    "预览不能用于全集计算，不把缺结果当零，不夸大因果；来源query_validations的pending_checks需要结合SQL与数据核对，"
                    "不能把基础检查通过当成业务正确；缺失解释限制字段不代表可以任意解释。历史已审核仅是历史交付状态，草稿保留的旧主张也须本轮核查。"
                    "本轮未取证不等于源数据不存在；重排报告不等于核实事实。用户请求列明不确定项时，准确承认未取证本身可以通过，不要求补查整个旧调查。"
                    "analysis_mode=fact时只核对本次数值/差额/比例和实际答复，不追补旧调查或保留超出问题的因果故事。",
                    review_payload)
            review_record = {'stage': 'qa_delivery_review', 'review': deepcopy(review),
                'context': {key: deepcopy(value) for key, value in review_context.items() if key != 'evidence'},
                'evidence_metadata': [{key: deepcopy(value) for key, value in item.items() if key != 'rows'}
                                      for item in review_evidence],
                'input_sha256': hashlib.sha256(review_payload.encode()).hexdigest(),
                'validation_errors': []}
            result.reviews.append(review_record)
            try:
                approved = resolve_analysis_review(review, draft, allowed_evidence_ids)
            except ValueError as exc:
                review_record['validation_errors'].append(str(exc))
                review = {'valid': False, 'reason': str(exc)}
                approved = ''
            if approved and managed:
                try:
                    candidate = (qa_result.report.model_copy(deep=True) if qa_result.report else
                                 AnalysisReport(
                                     title='已有结果追问', markdown=approved,
                                     data_sources=self.data_qa_agent._investigation_sources(bundle['evidence'], report_scope)))
                    candidate.markdown = approved
                    self.data_qa_agent.validate_investigation_report(candidate, bundle['evidence'], report_scope)
                except (ValueError, PermissionError) as exc:
                    review = {'valid': False, 'reason': f'审核定稿的证据引用未通过检查：{exc}'}
                    review_record['validation_errors'].append(review['reason'])
                    approved = ''
            if approved:
                result.analysis = approved
                if result.report:
                    result.report.markdown = approved
            result.final_reviewed = review.get("valid") is True and bool(approved)
            review_record['accepted'] = result.final_reviewed
            if not result.final_reviewed:
                result.status = "partial"
                result.message = "部分完成（已有结果审核未通过）"
                result.analysis = "尚未完成交付审核：" + str(review.get("reason") or "证据不足")
                result.stop_reason = '；'.join(filter(None, [result.stop_reason, str(review.get('reason') or '证据不足')]))
                result.report = None
        return {
            "workflow_mode": "qa_report" if qa_result.report else "qa",
            'analysis_evidence': bundle['evidence'] if managed else [],
            'analysis_evidence_gaps': qa_result.evidence_gaps if managed else [],
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
        # Bind the persisted result to the actual executed database, even when
        # the legacy entry point did not explicitly select a source.
        result.datasource_id = database
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
