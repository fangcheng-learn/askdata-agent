from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from collections import defaultdict
from typing import Any

from ..config import Settings
from ..model_client import ModelClient
from ..models import QueryResult
from .session_archive import SessionArchive
from .short_term_memory import ShortTermMemory


logger = logging.getLogger(__name__)


class SessionContext:
    """管理会话历史和前端上下文。"""

    def __init__(self, model_client: ModelClient, config: Settings) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.session_tasks: dict[str, list[str]] = defaultdict(list)
        self.table_row_limit = max(1, config.context_table_row_limit)
        self.route_context_turns = max(1, config.route_context_turns)
        self.archive = SessionArchive(
            config.session_archive_file,
            enabled=config.session_archive_enabled,
        )
        self.short_term_memory = ShortTermMemory(
            model_client,
            trigger_tokens=config.short_term_summary_trigger_tokens,
            batch_tokens=config.short_term_summary_batch_tokens,
            min_recent_turns=config.short_term_min_recent_turns,
            summary_enabled=config.short_term_summary_enabled,
            on_summary_updated=self.archive.save_summary,
        )
        self._restore_archive()

    def remember(
        self,
        session_id: str,
        result: QueryResult,
        query: str,
        workspace: dict[str, Any],
        user_id: str = "demo_analyst",
    ) -> None:
        self.tasks[result.task_id] = {
            "query": query,
            "session_id": session_id,
            "workspace": workspace,
            "user_id": user_id,
            "result": result,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if result.task_id not in self.session_tasks[session_id]:
            self.session_tasks[session_id].append(result.task_id)
        self.archive.save_turn(
            result.task_id,
            session_id,
            query,
            workspace,
            result.model_dump(mode="json"),
        )
        # ReAct maintains its own structured investigation state in its existing
        # decision calls. Do not quietly add a second summary-model call.
        if result.workflow_mode != "investigation_react_v1":
            self.short_term_memory.maybe_schedule(session_id, self._memory_items(session_id))

    def restore_checkpoint(
        self, session_id: str, result: QueryResult, query: str,
        workspace: dict[str, Any], user_id: str,
    ) -> None:
        """Register an already owner-authorized durable task without model calls.

        This differs from optional chat-archive restoration: its owner, current
        data-source permission, evidence, and remaining budget were checked by
        the service and investigation store before registration.
        """
        self.tasks[result.task_id] = {
            "query": query, "session_id": session_id, "workspace": workspace,
            "user_id": user_id, "result": result,
            "restored": True, "checkpoint_restored": True,
        }
        if result.task_id not in self.session_tasks[session_id]:
            self.session_tasks[session_id].append(result.task_id)

    def _restore_archive(self) -> None:
        for item in self.archive.load_turns():
            try:
                result = QueryResult.model_validate(item["result"])
            except ValueError as exc:
                logger.warning(
                    "session_turn_restore_failed task_id=%s error=%s",
                    item.get("task_id"),
                    exc,
                )
                continue
            task_id = str(item["task_id"])
            session_id = str(item["session_id"])
            self.tasks[task_id] = {
                "query": item["query"],
                "session_id": session_id,
                "workspace": item["workspace"],
                "result": result,
                "restored": True,
                # Only service-created scoped sessions establish an owner.
                # Legacy unscoped archives remain unavailable to history APIs.
                "user_id": session_id.split(':', 1)[0] if session_id.count(':') >= 2 else None,
                "updated_at": item.get('updated_at'),
            }
            self.session_tasks[session_id].append(task_id)

        for state in self.archive.load_summaries():
            self.short_term_memory.restore(
                state["session_id"],
                state["summary"],
                state["summarized_ids"],
            )

    def latest_pending(self, session_id: str) -> dict[str, Any] | None:
        ids = self.session_tasks.get(session_id, [])
        if not ids:
            return None
        task = self.tasks.get(ids[-1])
        return (
            task
            if task
            and (not task.get("restored") or task.get("checkpoint_restored"))
            and task["result"].status == "waiting_clarification"
            else None
        )

    def latest_table_result(self, session_id: str) -> QueryResult | None:
        for task_id in reversed(self.session_tasks.get(session_id, [])):
            result: QueryResult = self.tasks[task_id]["result"]
            # A partial investigation is still the user's current subject.
            # Never silently fall back to an older successful, unrelated table.
            if result.workflow_mode == 'investigation_react_v1':
                return result
            if result.route == 'data_qa' and result.evidence:
                return result
            if result.status == "completed" and result.route == "database_query" and result.rows:
                return result
        return None

    def route_context(
        self, session_id: str, workspace: dict[str, Any] | None = None
    ) -> str:
        # 路由上下文只读取近期轮次，不读取异步摘要。
        recent_items = self._memory_items(session_id)[-self.route_context_turns :]
        context = {
            "recent_turns": [self._route_item(item) for item in recent_items],
        }
        selected_headers = self._selected_table_headers(session_id, workspace or {})
        if selected_headers:
            context["selected_table_headers"] = selected_headers
        return json.dumps(context, ensure_ascii=False) if recent_items or selected_headers else ""

    def short_term_context(self, session_id: str) -> str:
        """返回滑动窗口和异步摘要，供已有数据分析流程使用。"""
        return self.short_term_memory.context(session_id, self._memory_items(session_id))

    @staticmethod
    def _route_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "turn_id": str(item.get("task_id") or ""),
            "user_message": item.get("query", ""),
            "route": item.get("route", ""),
            "status": item.get("status", ""),
            "result_title": item.get("result_title"),
            "assistant_summary": str(item.get("analysis") or "")[:240],
            "columns": list(item.get("columns") or []),
            "row_count": int(item.get("row_count") or 0),
            "available_evidence": item.get("available_evidence") or [],
        }

    @staticmethod
    def _evidence_headers(result: QueryResult) -> list[dict]:
        # Investigations store rows under individual R entries, not the outer
        # QueryResult.rows. The router needs this catalog to distinguish an
        # existing result question from a new database request, without SQL,
        # row payloads, storage handles, or a second model call.
        if not result.evidence and result.result_ref and result.status == 'completed':
            # The evidence reader binds a saved single-query result as task:R1.
            # Advertise that same ID so the router never invents one from a
            # task ID or storage reference when continuing into a report.
            return [{'result_id': f'{result.task_id}:R1',
                     'goal': result.result_title or '已保存查询结果',
                     'columns': list(result.columns), 'complete': result.complete,
                     'total_rows': result.total_rows if result.total_rows is not None else len(result.rows),
                     'check_status': 'basic_valid', 'metric_names': [],
                     'time_range': None, 'time_ranges': []}]
        return [{
            'result_id': item.get('id'), 'goal': item.get('goal'),
            'columns': list(item.get('columns') or []),
            'complete': bool(item.get('complete')), 'total_rows': item.get('total_rows'),
            'check_status': item.get('check_status'),
            'metric_names': [m.get('name') for m in (item.get('query_contract') or {}).get('metrics') or []],
            'time_range': (item.get('query_contract') or {}).get('time_range'),
            'time_ranges': (item.get('query_contract') or {}).get('time_ranges') or [],
        } for item in result.evidence if item.get('check_status') in {'basic_valid', 'reviewed'}]

    def _selected_table_headers(
        self, session_id: str, workspace: dict[str, Any]
    ) -> list[dict[str, Any]]:
        supplied = {
            str(item.get("task_id")): item
            for item in workspace.get("analysis_tables", [])
            if item.get("task_id")
        }
        headers = []
        for task_id in list(dict.fromkeys(workspace.get("analysis_table_ids") or []))[:5]:
            provided = supplied.get(task_id)
            if provided:
                headers.append({
                    "task_id": task_id,
                    "title": str(provided.get("title") or "参考表格"),
                    "query": str(provided.get("query") or ""),
                    "columns": list(provided.get("columns") or []),
                    "row_count": len(provided.get("rows") or []),
                })
                continue
            task = self.tasks.get(task_id)
            if not task or task.get("session_id") != session_id:
                continue
            result: QueryResult = task["result"]
            headers.append({
                "task_id": task_id,
                "title": result.result_title or "参考表格",
                "query": task.get("query", ""),
                "columns": result.columns,
                "row_count": len(result.rows),
                "available_evidence": self._evidence_headers(result),
            })
        return headers

    def _memory_items(self, session_id: str) -> list[dict[str, Any]]:
        output = []
        for task_id in self.session_tasks.get(session_id, []):
            task = self.tasks[task_id]
            result: QueryResult = task["result"]
            output.append({
                "task_id": task_id,
                "query": task["query"],
                "route": result.route,
                "status": result.status,
                "result_title": result.result_title,
                "analysis": result.analysis or "",
                "columns": result.columns,
                "row_count": len(result.rows),
                "available_evidence": self._evidence_headers(result),
            })
        return output

    @staticmethod
    def _query_validations(result: QueryResult) -> list[dict[str, Any]]:
        contracts = [(item.get('arguments') or {}).get('query_contract') for item in result.tool_calls]
        contracts.extend(item.get('query_contract') for item in result.evidence
                         if item.get('check_status') != 'superseded')
        output = []
        for contract in contracts:
            validation = (contract or {}).get('validation')
            if validation and validation not in output:
                output.append(validation)
        return output

    def recent_result_context(self, session_id: str) -> str:
        result = self.latest_table_result(session_id)
        if not result:
            return ""
        return json.dumps({
            "task_id": result.task_id,
            "user_id": self.tasks.get(result.task_id, {}).get('user_id'),
            "query": self.tasks.get(result.task_id, {}).get('query', ''),
            "status": result.status, "workflow_mode": result.workflow_mode,
            "final_reviewed": result.final_reviewed, "stop_reason": result.stop_reason,
            "requirements": result.requirements,
            "report": result.report.model_dump(mode='json', exclude={'data_sources'}) if result.report else None,
            "title": result.result_title or "最近查询结果",
            "row_count": result.total_rows if result.total_rows is not None else len(result.rows),
            "result_ref": result.result_ref, "complete": result.complete,
            "context_truncated": result.preview_truncated or len(result.rows) > self.table_row_limit,
            "datasource_id": result.datasource_id, "scenario": result.scenario,
            "evidence": result.evidence,
            "query_validations": self._query_validations(result),
            "validation_metadata_available": bool(self._query_validations(result)),
            "analysis": result.analysis,
            "columns": result.columns,
            "rows": result.rows[: self.table_row_limit],
            "sql": result.sql,
        }, ensure_ascii=False)

    def analysis_context(
        self, session_id: str, workspace: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        payloads: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        supplied = {
            str(item.get("task_id")): item
            for item in workspace.get("analysis_tables", [])
            if item.get("task_id") and item.get("rows")
        }
        for task_id in list(dict.fromkeys(workspace.get("analysis_table_ids") or []))[:5]:
            provided = supplied.get(task_id)
            if provided:
                source = {
                    "task_id": task_id,
                    "title": str(provided.get("title") or "历史查询结果"),
                    "query": str(provided.get("query") or ""),
                    "row_count": len(provided.get("rows") or []),
                    "columns": list(provided.get("columns") or []),
                }
                sources.append(source)
                payloads.append({
                    **source,
                    "rows": list(provided.get("rows") or [])[: self.table_row_limit],
                    "query_validations": self._query_validations(self.tasks[task_id]['result'])
                        if task_id in self.tasks and self.tasks[task_id].get('session_id') == session_id else [],
                    "validation_metadata_available": bool(self._query_validations(self.tasks[task_id]['result']))
                        if task_id in self.tasks and self.tasks[task_id].get('session_id') == session_id else False,
                })
                continue
            task = self.tasks.get(task_id)
            if not task or task.get("session_id") != session_id:
                continue
            result: QueryResult = task["result"]
            if (result.status != "completed" or not result.rows) and result.workflow_mode != 'investigation_react_v1' and not result.evidence:
                continue
            source = {
                "task_id": task_id,
                "title": result.result_title or "历史查询结果",
                "query": task.get("query", ""),
                "row_count": result.total_rows if result.total_rows is not None else len(result.rows),
                "context_truncated": result.preview_truncated or len(result.rows) > self.table_row_limit,
                "result_ref": result.result_ref,
                "columns": result.columns,
            }
            sources.append(source)
            payloads.append({
                **source,
                "user_id": task.get('user_id'), "status": result.status,
                "workflow_mode": result.workflow_mode, "final_reviewed": result.final_reviewed,
                "stop_reason": result.stop_reason, "requirements": result.requirements,
                "datasource_id": result.datasource_id, "scenario": result.scenario,
                "evidence": result.evidence, "complete": result.complete,
                "report": result.report.model_dump(mode='json', exclude={'data_sources'}) if result.report else None,
                "analysis": result.analysis,
                "sql": result.sql, "query_validations": self._query_validations(result),
                "validation_metadata_available": bool(self._query_validations(result)),
                "rows": result.rows[: self.table_row_limit],
            })
        return json.dumps(payloads, ensure_ascii=False) if payloads else "", sources

    @staticmethod
    def normalize_workspace(workspace: dict[str, Any] | None) -> dict[str, Any]:
        resolved = dict(workspace or {})
        resolved["schema_fields"] = list(resolved.get("schema_fields") or resolved.get("fields") or [])
        resolved["analysis_table_ids"] = list(dict.fromkeys(resolved.get("analysis_table_ids") or []))
        resolved["analysis_tables"] = list(resolved.get("analysis_tables") or [])
        resolved["confirmed_schema_tables"] = list(dict.fromkeys(resolved.get("confirmed_schema_tables") or []))
        resolved["confirmed_parameters"] = dict(resolved.get("confirmed_parameters") or {})
        return resolved

    @staticmethod
    def query_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
        return {
            "datasource_id": workspace.get("datasource_id"),
            "scenario": workspace.get("scenario") or {},
            "schema_fields": list(workspace.get("schema_fields") or []),
            "confirmed_schema_tables": list(workspace.get("confirmed_schema_tables") or []),
            "confirmed_parameters": dict(workspace.get("confirmed_parameters") or {}),
        }

    @staticmethod
    def match_clarification(text: str, result: QueryResult) -> str | None:
        if not result.clarification:
            return None
        normalized = text.strip().lower().replace(" ", "")
        options = result.clarification.options
        for option in options:
            for value in (option.id, option.label):
                value = value.lower().replace(' ', '')
                # A label appearing inside a new question is not a selection.
                # Accept only the choice itself or a small selection wrapper.
                pattern = (r'^(?:我选|我选择|选择|就用|就选|请用|请按|使用|选|用|按)?'
                           + re.escape(value) + r'(?:吧|即可|就行|继续|继续查询|的数据)?[。！!，,]*$')
                if value and re.fullmatch(pattern, normalized):
                    return option.id
        ordinal = {"第一个": 0, "第一项": 0, "选一": 0, "1": 0, "第二个": 1, "第二项": 1, "选二": 1, "2": 1}
        if normalized in ordinal and ordinal[normalized] < len(options):
            return options[ordinal[normalized]].id
        return None
