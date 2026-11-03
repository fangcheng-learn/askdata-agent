from __future__ import annotations

import uuid
from typing import Any

from langgraph.types import Command

from ..config import settings
from ..database import SCHEMA
from ..errors import PipelineStageError
from ..model_client import ModelClient
from ..models import QueryResult
from ..runtime_budget import RunBudget
from ..retrieval import SchemaIndex
from ..security import AccessController
from .. import homenova
from ..workflows.query_graph import QueryWorkflow
from .memory_store import MemoryStore
from .session_context import SessionContext


class AskDataService:
    """查询服务入口，负责会话管理和工作流调用。"""

    def __init__(
        self,
        model_client: ModelClient | None = None,
        schema_index: SchemaIndex | None = None,
    ) -> None:
        self.model_client = model_client or ModelClient(settings)
        self.schema_index = schema_index or SchemaIndex(self.model_client, settings)
        self.config = self.schema_index.config
        self.access_controller = AccessController()
        self.context = SessionContext(self.model_client, self.config)
        self.memories = MemoryStore(limit=self.config.max_saved_memories)
        self.workflow = QueryWorkflow(self.model_client, self.schema_index, self.config)
        self.tasks = self.context.tasks
        self.session_tasks = self.context.session_tasks

    def submit(
        self,
        query: str,
        session_id: str,
        workspace: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> QueryResult:
        normalized = query.strip()
        resolved_workspace = self.context.normalize_workspace(workspace)
        datasource_id = str(resolved_workspace.get("datasource_id") or "")
        access_scope = self.access_controller.scoped(user_id, datasource_id or None)
        scoped_session_id = f"{access_scope.user_id}:{datasource_id or 'legacy'}:{session_id}"
        self._restore_session(scoped_session_id, access_scope.user_id, datasource_id)
        if homenova.is_homenova(datasource_id):
            resolved_workspace["analysis_tables"] = []
            resolved_workspace["analysis_table_ids"] = [
                task_id for task_id in resolved_workspace.get("analysis_table_ids", [])
                if self.tasks.get(task_id, {}).get("session_id") == scoped_session_id
                and self.tasks.get(task_id, {}).get("workspace", {}).get("datasource_id") == datasource_id
            ]
            resolved_workspace["schema_fields"] = [field for field in resolved_workspace.get("schema_fields", [])
                if field.get("tableId") in access_scope.allowed_tables]
            resolved_workspace["confirmed_schema_tables"] = [table for table in resolved_workspace.get("confirmed_schema_tables", [])
                if table in access_scope.allowed_tables]
            resolved_workspace["confirmed_parameters"] = {"database": datasource_id}
            resolved_workspace["scenario"] = homenova.scenario(datasource_id)
        pending = self.context.latest_pending(scoped_session_id)
        if pending and pending.get("user_id") == access_scope.user_id and not self._is_report_followup(normalized):
            option_id = self.context.match_clarification(normalized, pending["result"])
            if not option_id and self._parameter_reply(normalized, pending["result"]):
                option_id = ("__continue__" if pending["result"].clarification
                             and pending["result"].clarification.parameter == "resume" else normalized)
            if option_id:
                return self.clarify(
                    pending["result"].task_id,
                    option_id,
                    user_message=normalized,
                    user_id=access_scope.user_id,
                )

        resumed = self._resume_latest_investigation(normalized, scoped_session_id, access_scope.user_id)
        if resumed is not None:
            return resumed

        task_id = uuid.uuid4().hex[:12]
        self.context.archive.save_message(
            scoped_session_id,
            "user",
            normalized,
            task_id=task_id,
        )
        try:
            result = self.workflow.invoke(
                self._payload(
                    task_id,
                    normalized,
                    scoped_session_id,
                    resolved_workspace,
                    access_scope.public(),
                ),
                task_id,
            )
        except PipelineStageError as exc:
            result = QueryResult(
                task_id=task_id,
                status="failed",
                route="data_qa" if exc.stage == "qa_answer" else "database_query",
                message=f"处理停止：{exc.stage}失败",
                analysis=exc.message,
                route_reason="未使用本地语义兜底",
                steps=[f"{exc.stage}失败，流程已停止"],
                execution_log=[{"stage": exc.stage, "success": False, "error": exc.message, **exc.context}],
                workflow_mode="failed_explicitly",
            )
        if task_id in self.workflow.budgets:
            result.budget = self.workflow.budgets[task_id].public()
        result.datasource_id = datasource_id or result.datasource_id
        if homenova.is_homenova(datasource_id):
            result.datasource = homenova.public_datasource(datasource_id)
            result.scenario = homenova.scenario(datasource_id)
        self.context.remember(
            scoped_session_id,
            result,
            normalized,
            resolved_workspace,
            access_scope.user_id,
        )
        self.context.archive.save_message(
            scoped_session_id,
            "assistant",
            result.analysis or result.message,
            task_id=task_id,
            metadata={"route": result.route, "status": result.status},
        )
        return result

    @staticmethod
    def _is_report_followup(query: str) -> bool:
        value = query.strip().rstrip('。！!？?')
        return value in {'生成报告', '整理报告', '继续生成报告', '重新生成报告', '修订报告', '完善报告',
                         '根据刚刚的结果', '根据刚才的结果', '根据刚刚的结果生成报告', '根据刚才的结果生成报告'}

    def _resume_latest_investigation(self, query: str, session_id: str, user_id: str) -> QueryResult | None:
        # A bare continuation is not a new analysis goal and must not create
        # a new task (or a fresh allowance). New substantive questions still
        # pass through normal intent routing.
        if query.strip().rstrip('。！!？?') not in {'继续', '继续调查', '恢复', '恢复调查', '__continue__'}:
            return None
        ids = self.session_tasks.get(session_id) or []
        task = self.tasks.get(ids[-1]) if ids else None
        if not task or task.get('user_id') != user_id:
            return None
        current = task['result']
        if current.workflow_mode != 'investigation_react_v1' or current.status not in {'partial', 'failed'}:
            return None
        self._authorized_task(current.task_id, user_id)
        budget = self.workflow.budgets.get(current.task_id)
        can_continue = (budget is not None and budget.elapsed() < min(budget.seconds, budget.investigation_seconds)
                        and budget.tools < budget.tool_limit and budget.react_rounds < budget.react_round_limit)
        if can_continue:
            return self.clarify(current.task_id, '__continue__', user_message=query, user_id=user_id)
        message = ('原调查预算已用尽，无法继续；已有结果和未完成项保留，未重新发起查询。' if budget else
                   '原调查的预算记录暂不可恢复，未创建新任务或重新发起查询。')
        self.context.archive.save_message(session_id, 'user', query, task_id=current.task_id)
        self.context.archive.save_message(session_id, 'assistant', message, task_id=current.task_id,
                                          metadata={'type': 'resume_unavailable', 'status': current.status})
        return current.model_copy(update={'message': message})

    def clarify(
        self,
        task_id: str,
        option_id: str,
        user_message: str | None = None,
        user_id: str | None = None,
    ) -> QueryResult:
        task = self._authorized_task(task_id, user_id)
        access_scope = self.access_controller.resolve(user_id)
        if task.get("user_id", AccessController.DEFAULT_USER) != access_scope.user_id:
            raise PermissionError("无权访问该查询任务")
        current: QueryResult = task["result"]
        if option_id == "跳过，继续分析":
            option_id = "__skip__"
        is_harness = current.workflow_mode in {"investigation_harness_v1", "investigation_react_v1"}
        resume_only = (current.workflow_mode == "investigation_react_v1"
                       and option_id == "__continue__"
                       and current.status != "completed"
                       and (current.status in {"partial", "failed"}
                            or bool(current.clarification and current.clarification.parameter == "resume")))
        report_only = current.workflow_mode == 'investigation_react_v1' and option_id == '__report__' and bool(current.evidence)
        skipping = option_id == '__skip__'
        if skipping and (not current.clarification or current.clarification.required or not current.clarification.allow_skip):
            raise ValueError('必要条件不能跳过')
        if not resume_only and not report_only and not skipping and (not current.clarification or (
                not self._free_clarification(current)
                and option_id not in {item.id for item in current.clarification.options})):
            raise ValueError(option_id)
        option = next((item for item in current.clarification.options if item.id == option_id), None) if current.clarification else None
        current_scope = self.access_controller.scoped(user_id, task["workspace"].get("datasource_id"))
        if is_harness and task_id in self.workflow.harness.records:
            self.workflow.harness.records[task_id].state["access_scope"] = current_scope.public()
        self.context.archive.save_message(
            task["session_id"],
            "user",
            user_message or (option.label if option else option_id),
            task_id=task_id,
            metadata={"type": "clarification", "option_id": option_id},
        )
        try:
            result = (self.workflow.resume_harness(task_id, option_id) if is_harness else
                      self.workflow.invoke(Command(resume={"option_id": option_id}), task_id))
        except PipelineStageError as exc:
            result = QueryResult(
                task_id=task_id,
                status="failed",
                route="database_query",
                message=f"处理停止：{exc.stage}失败",
                analysis=exc.message,
                route_reason="用户澄清后查询未通过校验",
                steps=[f"{exc.stage}失败，流程已停止"],
                execution_log=[{"stage": exc.stage, "success": False, "error": exc.message, **exc.context}],
                workflow_mode="failed_explicitly",
            )
        if task_id in self.workflow.budgets:
            result.budget = self.workflow.budgets[task_id].public()
        result.datasource_id = task["workspace"].get("datasource_id") or result.datasource_id
        if homenova.is_homenova(result.datasource_id):
            result.datasource = homenova.public_datasource(result.datasource_id)
            result.scenario = homenova.scenario(result.datasource_id)
        self.context.remember(
            task["session_id"],
            result,
            task["query"],
            task["workspace"],
            access_scope.user_id,
        )
        self.context.archive.save_message(
            task["session_id"],
            "assistant",
            result.analysis or result.message,
            task_id=task_id,
            metadata={"route": result.route, "status": result.status},
        )
        return result

    @staticmethod
    def _free_clarification(result: QueryResult) -> bool:
        clarification = result.clarification
        if not clarification or clarification.parameter == "database":
            return False
        schema_ids = {table["id"] for table in SCHEMA}
        # Multiple concrete options are a choice, even when their IDs are
        # years or business labels rather than schema IDs. Unmatched questions
        # must reach preprocessing with their entire intent preserved.
        return (len(clarification.options) <= 1
                and not any(option.id in schema_ids for option in clarification.options))

    @classmethod
    def _parameter_reply(cls, message: str, result: QueryResult) -> bool:
        """Accept a short parameter reply, never swallow a complete new goal."""
        import re
        if not cls._free_clarification(result):
            return False
        value = message.strip()
        if result.clarification and result.clarification.parameter == "resume":
            return value in {"__continue__", "继续", "继续调查", "恢复", "恢复调查"}
        if not value or len(value) > 80:
            return False
        # These are complete/new analytical requests rather than a missing
        # date, amount, scope label, or other free-form parameter.
        return not re.search(
            r"为什么|原因|分析|调查|如何|怎么|查询|查一下|统计|可视化|画图|解释|顺便|另外|同时|[?？]",
            value,
        )

    def _restore_session(self, session_id: str, user_id: str, datasource_id: str) -> None:
        store = getattr(self.workflow, "investigation_store", None)
        if store is None:
            return
        # list_tasks returns newest first. Register oldest first so the latest
        # pending task and follow-up context keep their actual order.
        for saved in reversed(store.list_tasks(user_id, session_id)):
            if saved.get("datasource_id") != datasource_id:
                continue
            try:
                self._restore_investigation(str(saved["task_id"]), user_id)
            except (PermissionError, KeyError, ValueError):
                # An old inaccessible checkpoint must not leak into context or
                # prevent a new query under the user's current permission.
                task_id = str(saved["task_id"])
                self.tasks.pop(task_id, None)
                if task_id in self.session_tasks.get(session_id, []):
                    self.session_tasks[session_id].remove(task_id)

    def _restore_investigation(self, task_id: str, user_id: str) -> dict[str, Any]:
        store = getattr(self.workflow, "investigation_store", None)
        if store is None:
            raise KeyError(task_id)
        saved = store.load_task(task_id, user_id)
        if not saved:
            raise KeyError(task_id)
        datasource_id = str(saved["datasource_id"])
        scope = self.access_controller.scoped(user_id, datasource_id)
        payload = saved.get("payload") or {}
        state = payload.get("state") or {}
        if (state.get("task_id", task_id) != task_id
                or state.get("session_id", saved["session_id"]) != saved["session_id"]
                or state.get("datasource_id") not in (None, "", datasource_id)
                or (state.get("access_scope") or {}).get("user_id", user_id) != user_id):
            raise PermissionError("保存任务的身份或数据源绑定不一致")
        self._check_evidence_permissions(payload.get("evidence") or [], scope)
        record = self.workflow.harness.records.get(task_id)
        if record is None:
            record = self.workflow.harness.restore(task_id, user_id)
        if record is None:
            raise KeyError(task_id)
        record.state["access_scope"] = scope.public()
        if task_id not in self.workflow.budgets:
            self.workflow.budgets[task_id] = RunBudget.from_state(task_id, saved.get("budget") or {})
        result = self.workflow.harness.snapshot(task_id)
        result.budget = self.workflow.budgets[task_id].public()
        result.datasource_id = datasource_id
        if homenova.is_homenova(datasource_id):
            result.datasource = homenova.public_datasource(datasource_id)
            result.scenario = homenova.scenario(datasource_id)
        session_id = str(saved["session_id"])
        prefix = f"{user_id}:{datasource_id}:"
        if "session_id" in QueryResult.model_fields:
            result.session_id = session_id[len(prefix):] if session_id.startswith(prefix) else session_id
        self.context.restore_checkpoint(
            session_id, result, str(record.state.get("query") or ""),
            {**(record.state.get("workspace") or {}), "datasource_id": datasource_id}, user_id,
        )
        return self.tasks[task_id]

    def _check_evidence_permissions(self, evidence: list[dict], scope) -> None:
        from ..database import physical_table_name
        available = {physical_table_name(table) for table in self.access_controller.filter_schema(scope)}
        for item in evidence:
            if item.get("check_status") == "superseded":
                continue
            if item.get("user_id", scope.user_id) != scope.user_id:
                raise PermissionError("保存证据不属于当前用户")
            source = item.get("datasource_id")
            if source and source not in scope.allowed_databases:
                raise PermissionError("保存证据的数据源不在当前权限中")
            tables = set((item.get("query_contract") or {}).get("tables") or [])
            if not tables <= available:
                raise PermissionError("保存证据引用的表已不在当前权限中")

    def _authorized_task(self, task_id: str, user_id: str | None) -> dict[str, Any]:
        scope = self.access_controller.resolve(user_id)
        task = self.tasks.get(task_id)
        if task and task.get("user_id", AccessController.DEFAULT_USER) != scope.user_id:
            raise PermissionError("无权访问该查询任务")
        if not task or (task["result"].workflow_mode == "investigation_react_v1"
                        and task_id not in self.workflow.harness.records):
            return self._restore_investigation(task_id, scope.user_id)
        datasource_id = str(task.get("workspace", {}).get("datasource_id") or "")
        current = self.access_controller.scoped(scope.user_id, datasource_id or None)
        if task["result"].workflow_mode == "investigation_react_v1":
            self._check_evidence_permissions(task["result"].evidence, current)
            self.workflow.harness.records[task_id].state["access_scope"] = current.public()
        if "session_id" in QueryResult.model_fields:
            prefix = f"{scope.user_id}:{datasource_id or 'legacy'}:"
            session_id = str(task["session_id"])
            task["result"].session_id = session_id[len(prefix):] if session_id.startswith(prefix) else session_id
        return task

    def search_schema(
        self,
        query: str,
        threshold: float | None = None,
        user_id: str | None = None,
        datasource_id: str | None = None,
    ) -> dict[str, Any]:
        scope = self.access_controller.scoped(user_id, datasource_id)
        if homenova.is_homenova(datasource_id):
            prepared = self.workflow.preprocessor.prepare(query, homenova.scenario_text(str(datasource_id)))
            extraction = prepared.retrieval
            base = self.workflow.home_index(str(datasource_id)).retrieve(query,
                retrieval_terms=extraction.retrieval_terms, threshold=threshold, access_scope=scope)
            retrieval = homenova.selected_retrieval(str(datasource_id), query, base, extraction.public())
            retrieval["extraction"] = extraction.public()
            retrieval["schema_graph"] = self.workflow.graph_builder.build(retrieval["hits"], scope)
            return retrieval
        prepared = self.workflow.preprocessor.prepare(query, "")
        extraction = prepared.retrieval
        retrieval = self.schema_index.retrieve(
            query,
            retrieval_terms=extraction.retrieval_terms,
            threshold=threshold,
            access_scope=scope,
        )
        retrieval["extraction"] = extraction.public()
        retrieval["schema_graph"] = self.workflow.graph_builder.build(
            retrieval["hits"],
            scope,
        )
        return retrieval

    def rebuild_schema_index(self) -> dict[str, Any]:
        self.schema_index.ensure_built(force=True)
        return self.schema_index.status()

    def get_task(self, task_id: str, user_id: str | None = None) -> QueryResult:
        return self._authorized_task(task_id, user_id)["result"]

    def list_history_summaries(self, user_id: str, datasource_id: str, limit: int = 100) -> list[dict]:
        scope = self.access_controller.scoped(user_id, datasource_id)
        prefix = f'{scope.user_id}:{datasource_id}:'
        hidden = self.context.archive.hidden_sessions(scope.user_id, datasource_id)
        records = {r['task_id']: r for r in self.workflow.investigation_store.list_task_summaries(scope.user_id, datasource_id)}
        for tid, task in list(self.tasks.items()):
            if task.get('user_id') == scope.user_id and (task.get('workspace') or {}).get('datasource_id') == datasource_id:
                records.setdefault(tid, dict(task_id=tid, session_id=str(task.get('session_id') or ''),
                    query=task['query'], updated_at=task.get('updated_at')))
        return sorted([{**r, 'session_id':r['session_id'][len(prefix):]} for r in records.values()
            if r['session_id'].startswith(prefix) and r['session_id'][len(prefix):] not in hidden],
            key=lambda r: str(r.get('updated_at') or ''), reverse=True)[:limit]

    def list_history(self, user_id: str, datasource_id: str, limit: int = 50,
                     session_ids: set[str] | None = None, task_ids: set[str] | None = None) -> list[dict]:
        """Load saved turns without resuming models, SQL or investigation budgets."""
        scope = self.access_controller.scoped(user_id, datasource_id)
        prefix = f'{scope.user_id}:{datasource_id}:'
        stamps = {}
        store = self.workflow.investigation_store
        summaries = store.list_task_summaries(scope.user_id, datasource_id)
        for saved in reversed(summaries):
            if session_ids is not None and saved['session_id'][len(prefix):] not in session_ids and saved['task_id'] not in (task_ids or set()):
                continue
            if saved['task_id'] in self.tasks:
                continue
            if not saved['session_id'].startswith(prefix):
                continue
            try:
                self._restore_investigation(saved['task_id'], scope.user_id)
            except (PermissionError, KeyError, ValueError):
                continue
        # Older investigation checkpoints already exist even though the chat
        # archive was previously disabled. Recover their original timestamps.
        stamps = store.task_timestamps(scope.user_id, datasource_id)
        hidden = self.context.archive.hidden_sessions(scope.user_id, datasource_id)
        output = []
        for task_id, task in list(self.tasks.items()):
            session = str(task.get('session_id') or '')
            if session_ids is not None and session[len(prefix):] not in session_ids and task_id not in (task_ids or set()):
                continue
            if (task.get('user_id') != scope.user_id or not session.startswith(prefix)
                    or session[len(prefix):] in hidden):
                continue
            if str((task.get('workspace') or {}).get('datasource_id') or '') != datasource_id:
                continue
            try:
                result = self.get_task(task_id, scope.user_id)
                self._check_evidence_permissions(result.evidence, scope)
                if result.sql:
                    self.workflow.database_engine._validate_sql(datasource_id, result.sql, scope)
                selected = (result.schema_graph or {}).get('tables') or []
                if any(not scope.allows_table(datasource_id, table['id']) for table in selected if table.get('id')):
                    continue
            except (PermissionError, KeyError, ValueError):
                continue
            output.append({'task_id': task_id, 'session_id': session[len(prefix):],
                'query': task['query'], 'workspace': task.get('workspace') or {},
                'updated_at': stamps.get(task_id) or task.get('updated_at'),
                'result': result.model_dump(mode='json')})
        return sorted(output, key=lambda item: str(item.get('updated_at') or ''), reverse=True)[:limit]

    def hide_history(self, user_id: str, datasource_id: str, session_id: str) -> None:
        scope = self.access_controller.scoped(user_id, datasource_id)
        if not session_id.strip() or len(session_id) > 256:
            raise ValueError('无效会话编号')
        self.context.archive.hide_session(scope.user_id, datasource_id, session_id)

    def save_memory(self, task_id: str, user_id: str | None = None) -> None:
        task = self._authorized_task(task_id, user_id)
        if task["result"].status != "completed":
            raise KeyError(task_id)
        scope = self.access_controller.resolve(user_id)
        if task.get("user_id", AccessController.DEFAULT_USER) != scope.user_id:
            raise PermissionError("无权保存该查询结果")
        result: QueryResult = task["result"]
        self._memory_store(task.get("workspace", {}).get("datasource_id")).save_result(
            task_id,
            task["query"],
            result.analysis or result.message,
            result.result_title or "查询结果",
            result.columns,
            result.rows,
            scope.user_id,
        )
        result.saved = True

    def save_field_memory(
        self,
        table_id: str,
        name: str,
        label: str,
        field_type: str,
        user_id: str | None = None,
    ) -> None:
        scope = self.access_controller.resolve(user_id)
        table = next((item for item in SCHEMA if item["id"] == table_id), None)
        database = str(table.get("database") or "askdata_mock") if table else ""
        if not table or not scope.allows_table(database, table_id):
            raise PermissionError("无权保存该字段")
        if not any(field["name"] == name for field in table["fields"]):
            raise ValueError("字段不存在")
        self._memory_store(database).save_field(table_id, name, label, field_type, scope.user_id)

    def delete_memory(self, memory_id: str, user_id: str | None = None, datasource_id: str | None = None) -> None:
        scope = self.access_controller.scoped(user_id, datasource_id)
        self._memory_store(datasource_id).delete(
            memory_id,
            scope.user_id,
            include_all="admin" in scope.roles and not homenova.is_homenova(datasource_id),
        )

    def list_memories(self, user_id: str | None = None, datasource_id: str | None = None) -> list[dict[str, Any]]:
        scope = self.access_controller.scoped(user_id, datasource_id)
        return self._memory_store(datasource_id).list(
            scope.user_id,
            include_all="admin" in scope.roles and not homenova.is_homenova(datasource_id),
        )

    def list_mcp_tools(self, user_id: str | None = None, datasource_id: str | None = None) -> list[dict[str, Any]]:
        """返回与模型看到的内容一致的MCP工具定义。"""
        scope = self.access_controller.scoped(user_id, datasource_id)
        return self.workflow.mcp_client(scope.public()).list_tools()

    def list_skills(self) -> list[dict[str, Any]]:
        """返回当前启用的应用级Skill配置。"""
        return self.workflow.skills.list()

    def visible_schema(self, user_id: str | None = None, datasource_id: str | None = None) -> list[dict[str, Any]]:
        return self.access_controller.filter_schema(
            self.access_controller.scoped(user_id, datasource_id)
        )

    def _memory_store(self, datasource_id: str | None = None) -> MemoryStore:
        if homenova.is_homenova(datasource_id):
            return MemoryStore(path=homenova.RUNTIME_ROOT / str(datasource_id) / "saved_memories.json",
                               limit=self.config.max_saved_memories)
        return self.memories

    def list_datasources(self, user_id: str | None = None) -> list[dict[str, Any]]:
        scope = self.access_controller.resolve(user_id)
        homes = [source for source in homenova.available_sources() if source["id"] in scope.allowed_databases]
        names = {"askdata_mock": "示例销售数据", "ecommerce_ops": "电商演示数据"}
        return homes + [{"id": database, "label": names.get(database, database)}
                        for database in sorted(scope.allowed_databases) if not homenova.is_homenova(database)]

    def status(self) -> dict[str, Any]:
        return {
            **self.config.public_status(),
            "workflow_engine": "langgraph",
            "human_in_the_loop": "interrupt+checkpointer",
            "mcp": {
                "transport": "in_process",
                "tool_count": len(self.list_mcp_tools()),
            },
            "skills": [item["name"] for item in self.list_skills()],
            "schema_index": self.schema_index.status(),
        }

    def _payload(
        self,
        task_id: str,
        query: str,
        session_id: str,
        workspace: dict[str, Any],
        access_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        analysis_context, analysis_sources = self.context.analysis_context(session_id, workspace)
        # Candidates are assembled by the server, restricted before the model
        # can name one; the result loader independently rechecks every binding.
        candidate_ids = [previous_id for previous_id in self.session_tasks.get(session_id, [])[-5:]
            if self.tasks[previous_id].get('user_id') == (access_scope or {}).get('user_id')
            and self.tasks[previous_id]['result'].datasource_id == workspace.get('datasource_id')
            and (self.tasks[previous_id]['result'].scenario or {}) == (workspace.get('scenario') or {})]
        candidates, _ = self.context.analysis_context(session_id, {'analysis_table_ids': candidate_ids})
        return {
            "task_id": task_id,
            "query": query,
            "datasource_id": str(workspace.get("datasource_id") or ""),
            "scenario": workspace.get("scenario") or {},
            "session_id": session_id,
            "workspace": self.context.query_workspace(workspace),
            "access_scope": access_scope or self.access_controller.resolve(None).public(),
            "route_context": self.context.route_context(session_id, workspace),
            "short_term_context": self.context.short_term_context(session_id),
            "recent_result_context": self.context.recent_result_context(session_id),
            "recent_result_candidates": candidates,
            "analysis_context": analysis_context,
            "analysis_sources": analysis_sources,
            "reusable_evidence": [evidence for previous_id in self.session_tasks.get(session_id, [])[-5:]
                for evidence in self.tasks[previous_id]["result"].evidence
                if self.tasks[previous_id].get("user_id") == (access_scope or {}).get("user_id")
                and evidence.get("datasource_id") == workspace.get("datasource_id")
                and evidence.get("check_status") == "reviewed" and evidence.get("complete")],
            "tool_facts": {},
            "execution_log": [],
            "tool_calls": [],
            "clarification": None,
        }

    _normalize_workspace = staticmethod(SessionContext.normalize_workspace)
    _query_workspace = staticmethod(SessionContext.query_workspace)
