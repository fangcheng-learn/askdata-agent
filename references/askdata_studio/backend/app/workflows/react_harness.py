"""A single, bounded investigation loop with durable evidence references.

The model selects actions. Code owns execution, identities, permissions, budgets,
result validity and delivery. This module never invokes the question workflow per
step, and never uses a model to review intermediate steps.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from .. import homenova
from ..database import SCHEMA, physical_table_name
from ..models import Clarification, QueryResult
from ..errors import ModelOutputError, PipelineStageError
from ..querying.query_contract import QueryContractValidator
from ..querying.query_protocol import canonical_sql, model_contract_schema, sql_facing_graph
from ..querying.query_context import select_evidence_ids, select_query_graph
from ..runtime_budget import BudgetStop, RunBudget, current_budget
from ..agent_configuration_runtime import configuration_snapshot, effective_prompt, effective_skill
from ..structured_output import using_output_schema
from ..security import AccessScope
from .react_prompts import DRAFT_SYSTEM, REACT_SYSTEM, REVIEW_SYSTEM, SQL_REPAIR_SYSTEM, react_system_for_skills
from .sql_batch import SqlBatchController
from .investigation_progress import merge_progress_records, review_questions, reconcile_review
from .progress_protocol import normalize_progress, resolve_references, output_schema as progress_output_schema


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _prompt_json(value: Any) -> str:
    """Compact transport formatting; never changes definitions or evidence."""
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(',', ':'))


class InvestigationPersistenceError(RuntimeError):
    """Stop execution when a durable checkpoint cannot be committed."""


@dataclass
class ReactRecord:
    state: dict[str, Any]
    # Missing versions belong to saved tasks from the previous execution path.
    protocol_version: int = 1
    configuration_snapshot: dict[str, Any] | None = None
    progress: dict[str, Any] = field(default_factory=lambda: {
        'facts': [], 'hypotheses': [], 'unresolved_questions': []})
    progress_diagnostics: dict[str, Any] = field(default_factory=dict)
    business_note_rounds: dict[str, int] = field(default_factory=dict)
    business_issue_rounds: dict[str, int] = field(default_factory=dict)
    skills: dict[str, dict[str, str]] = field(default_factory=dict)
    field_pool: dict[str, dict[str, Any]] = field(default_factory=dict)
    table_pool: dict[str, dict[str, Any]] = field(default_factory=dict)
    join_pool: dict[str, dict[str, Any]] = field(default_factory=dict)
    metric_pool: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity_value_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_fields: list[str] = field(default_factory=list)
    active_requested_fields: list[str] = field(default_factory=list)
    active_metrics: list[str] = field(default_factory=list)
    semantic_conflicts: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    retrievals: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    observation: dict[str, Any] = field(default_factory=dict)
    failed_signatures: dict[str, str] = field(default_factory=dict)
    completed_queries: dict[str, str] = field(default_factory=dict)
    reviewed_signatures: list[str] = field(default_factory=list)
    delivery_evidence_signature: str = ''
    clarification: dict[str, Any] | None = None
    draft: str = ''
    approved_text: str = ''
    draft_report: dict[str, Any] | None = None
    approved_report: dict[str, Any] | None = None
    report_warnings: list[str] = field(default_factory=list)
    draft_evidence_gaps: list[str] = field(default_factory=list)
    report_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    feedback: str = ''
    final_status: str = ''
    stop_reason: str = ''
    final_reviewed: bool = False
    delivery_kind: str = ''
    revision: int = 0
    clarification_history: list[dict[str, Any]] = field(default_factory=list)
    report_only: bool = False
    draft_request_signatures: list[str] = field(default_factory=list)


class ReactInvestigationHarness(SqlBatchController):
    def __init__(self, workflow: Any, store: Any) -> None:
        self.workflow, self.store = workflow, store
        self.model = workflow.model_client
        self.records: dict[str, ReactRecord] = {}
        self.validator = QueryContractValidator(workflow.database_engine)
        query_skill = workflow.skills.get('database_query')
        self.query_instructions = (query_skill.default_instructions or query_skill.instructions).split('## 输出协议')[0].strip()

    @staticmethod
    def _identity(task: ReactRecord) -> tuple[str, str, str]:
        return (task.state['task_id'], task.state['access_scope']['user_id'], task.state['datasource_id'])

    @staticmethod
    def _sql_limit(task: ReactRecord, action: dict | None = None) -> int:
        policy = 3 if task.protocol_version >= 3 else 2
        budget = current_budget.get()
        return min(policy, budget.sql_generation_limit if budget else policy,
                   (action or {}).get('sql_generation_limit') or policy)

    def restore(self, task_id: str, user_id: str) -> ReactRecord | None:
        saved = self.store.load_task(task_id, user_id)
        if not saved:
            return None
        payload = saved['payload']
        allowed = {item.name for item in fields(ReactRecord)}
        task = ReactRecord(**{key: value for key, value in payload.items() if key in allowed})
        # An in-flight remote call cannot safely be replayed after a crash.
        interrupted = [a for a in task.actions if a.get('status') in {'running', 'rewriting', 'generating'}]
        for action in interrupted:
            action.update(status='interrupted', error='服务中断，结果未知；未自动重放')
        if interrupted or not task.final_status:
            task.observation = {'type': 'interrupted', 'actions': [a['id'] for a in interrupted],
                                'message': '已有成功结果可复用；中断调用的执行及费用状态未知。'}
            task.clarification = {'parameter': 'resume', 'question': '调查因服务重启中断，是否从已保存记录继续？',
                                  'reason': '已完成结果与预算保留；中断调用结果未知，不会自动重放。', 'options': []}
            task.final_status = 'waiting_clarification'
        task.state['schema_graph'] = {'database': task.state['datasource_id'],
                                      'tables': list(task.table_pool.values()),
                                      'fields': list(task.field_pool.values()), 'joins': list(task.join_pool.values())}
        if hasattr(RunBudget, 'from_state') and hasattr(self.workflow, 'budgets'):
            self.workflow.budgets.setdefault(task_id, RunBudget.from_state(task_id, saved['budget']))
        self.records[task_id] = task
        return task

    def snapshot(self, task_id: str) -> QueryResult:
        """Return stored state only; callers authenticate before restoring it."""
        return self._result(self.records[task_id])

    def _persist(self, task: ReactRecord, results: list[tuple[str, dict]] | None = None) -> None:
        task_id, user_id, datasource = self._identity(task)
        budget = current_budget.get()
        ledger = (budget.export_state() if hasattr(budget, 'export_state') else budget.public()) if budget else {}
        # Context and SQL rows live in their own stores; no chat transcripts or
        # unbounded full result tables are serialized into the task snapshot.
        for action in task.actions:
            if action.get('error'):
                action['last_error'] = action['error']
        for question in task.progress.get('core_questions') or []:
            if question.get('status') == 'blocked':
                task.business_issue_rounds.setdefault(question['id'], task.revision + 1)
        payload = asdict(task)
        payload['state'] = {key: value for key, value in task.state.items() if key in {
            'task_id', 'session_id', 'query', 'standalone_query', 'datasource_id', 'workspace',
            'access_scope', 'scenario', 'time_context', 'required_questions', 'task_understanding',
            'confirmed_parameters', 'route_reason', 'interpretation', 'intent', 'extraction'}}
        try:
            self.store.save_task(task_id, user_id, str(task.state.get('session_id') or 'demo-session'),
                                 datasource, payload, ledger, results=results)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            raise InvestigationPersistenceError(f'调查持久化失败（{type(exc).__name__}），停止新动作') from exc

        from ..business_progress import emit_snapshot
        emit_snapshot(payload)

    def run(self, state: dict[str, Any], response: str | None = None) -> QueryResult:
        task_id = state['task_id']
        task = self.records.get(task_id)
        user_id = state['access_scope']['user_id']
        if task is None:
            task = self.restore(task_id, user_id)
        if task is None:
            task = ReactRecord(dict(state), protocol_version=int(state.get('investigation_protocol_version', 4)),
                               configuration_snapshot=configuration_snapshot())
            task.logs.append({'stage': 'agent_configuration_bound',
                              'configuration_revision': task.configuration_snapshot['revision']})
            task.progress['unresolved_questions'] = list(state.get('required_questions') or [state['query']])
            if task.protocol_version >= 3:
                task.progress['core_questions'] = [
                    {'id': f'Q{index}', 'question': question, 'importance': 'core', 'status': 'open',
                     'original': True, 'evidence_ids': []}
                    for index, question in enumerate(task.progress['unresolved_questions'], 1)]
                task.progress['explanations'] = []
            self.records[task_id] = task
            self._inherit_semantics(task, state)
            self._reuse_input_results(task, state.get('reusable_evidence') or [])
        if self._identity(task)[1:] != (user_id, state['datasource_id']):
            raise PermissionError('调查任务的用户或数据源范围不匹配')
        task.state['access_scope'] = state['access_scope']
        if task.final_status and not response:
            return self._result(task)
        if not response and not task.actions and (state.get('intent') or {}).get('response_type') == 'clarification':
            task.clarification = Clarification(parameter='other', question=state.get('direct_response') or '请补充分析所需条件',
                reason=(state.get('intent') or {}).get('reason') or '该条件影响本次分析范围', options=[]).model_dump()
            task.final_status = 'waiting_clarification'
            self._persist(task)
            return self._result(task)
        if response:
            if not task.clarification and task.final_status not in {'partial', 'failed'} and response != '__report__':
                raise ValueError('当前调查没有等待补充的参数')
            parameter = (task.clarification or {}).get('parameter', 'other')
            pending = task.clarification
            if response == '__skip__':
                if not pending or not pending.get('allow_skip') or pending.get('required', True):
                    raise ValueError('必要条件必须回答，不能跳过后开始调查')
                task.clarification_history.append({**pending, 'status': 'skipped', 'answer': None})
                task.observation = {'type': 'clarification_skipped', 'question': pending['question']}
                task.feedback = '用户跳过该背景问题；未知不能当作否定或默认确认，无新情况不要重复询问，继续有边界分析。'
            elif response == '__report__':
                task.report_only = True
                task.feedback = _json({'instruction': '承接原任务、期间和结果，只整理或修订报告，不新增查询。保留正确内容，修复具体问题。',
                    'review': task.reviews[-1] if task.reviews else None,
                    'diagnostics': task.report_diagnostics})
                task.observation = {'type': 'delivery_repair'}
            elif response == '__continue__':
                task.observation = {'type': 'user_resume', 'message': '用户确认从已有检查点继续，不重置预算。'}
                task.feedback = '用户确认继续原任务；复用已完成结果，不自动重放结果未知的调用。'
                if task.clarification:
                    task.observation['unanswered_clarification'] = task.clarification
                    task.feedback += '此前澄清尚未获得答案；不能当作用户已选条件。内部方法问题自行处理，必要用户条件仍须澄清。'
                if task.reviews and (task.reviews[-1].get('controller_repair_requests') or
                        task.reviews[-1].get('controller_action', task.reviews[-1].get('action')) == 'repair'):
                    requests = (task.reviews[-1].get('controller_repair_requests')
                                or task.reviews[-1].get('repair_requests') or [])
                    if requests:
                        task.observation['pending_review_repairs'] = requests
                        task.feedback += '上次交付审核仍有待处理意见（不是新增事实）：' + json.dumps(requests, ensure_ascii=False)
            else:
                if not response.strip():
                    raise ValueError('请填写回答')
                task.clarification_history.append({**(pending or {}), 'status': 'answered', 'answer': response,
                                                   'source': 'user', 'verified': False})
                task.state.setdefault('confirmed_parameters', {})[parameter] = response
                task.observation = {'type': 'user_clarification', 'parameter': parameter, 'response': response}
                task.feedback = '用户补充了当前任务条件，请继承目标、已确定期间、已有证据和剩余预算。'
            task.clarification = None
            task.final_status, task.stop_reason, task.final_reviewed = '', '', False
            task.delivery_kind = ''
        budget = current_budget.get()
        previous_callback = getattr(budget, 'checkpoint_callback', None) if budget else None
        if budget is not None:
            budget.phase = 'investigation'
            budget.checkpoint_callback = lambda: self._persist(task)
        try:
            self._persist(task)
            if task.report_only:
                if budget:
                    budget.phase = 'closing'
                    budget.check()
                self._deliver(task, stopping=True)
                if not task.final_status:
                    task.final_status = 'partial'
            while not task.final_status:
                # A failed final SQL candidate closes investigation before
                # another free-form decision can generate a third candidate.
                self._check_sql_generation_gate(task)
                if budget:
                    budget.phase = 'closing' if budget.react_rounds >= budget.react_round_limit else 'investigation'
                    budget.check()
                    if task.protocol_version >= 4 and task.evidence and budget.seconds - budget.elapsed() <= 120:
                        raise BudgetStop('进入报告整理与审核预留时间，停止新增调查')
                    if task.protocol_version >= 4 and budget.tools >= budget.tool_limit:
                        raise BudgetStop('工具调用预算已用完，转入阶段性报告整理')
                    budget.step = 'react_decision'
                    budget.react_decision(allow_closing=task.protocol_version >= 4)
                decision = self._call(task, 'react', REACT_SYSTEM, self._context(task))
                try:
                    progress_update = self._update_progress(task, decision.get('progress'))
                except (ValueError, KeyError, TypeError) as exc:
                    self._protocol_error(task, str(exc))
                    continue
                action = decision.get('action')
                if not isinstance(action, dict):
                    self._protocol_error(task, '缺少结构化 action 对象')
                    continue
                if task.protocol_version >= 3:
                    try:
                        action = self._normalize_action_references(task, action, progress_update)
                    except ValueError as exc:
                        self._protocol_error(task, str(exc))
                        continue
                from ..business_progress import emit_plan
                emit_plan(task.state['task_id'], action, task.revision+1)
                self._persist(task)
                action_type = action.get('type')
                if action_type == 'deliver':
                    self._deliver(task)
                else:
                    self._dispatch(task, action, str(decision.get('action_reason') or ''))
                task.revision += 1
                self._persist(task)
        except BudgetStop as exc:
            task.stop_reason = str(exc)
            task.logs.append({'stage': 'budget_stop', 'success': False, 'error': str(exc),
                              **getattr(exc, 'context', {})})
            if task.evidence and not (task.final_reviewed or task.delivery_kind == 'agent_report'):
                try:
                    if budget:
                        budget.phase = 'closing'
                        budget.check()
                    self._deliver(task, stopping=True)
                except (BudgetStop, RuntimeError, ValueError, TypeError, KeyError) as closing_error:
                    task.logs.append({'stage': 'closing_unavailable', 'success': False,
                                      'error': str(closing_error)})
            if not task.final_status:
                task.final_status = 'partial' if task.evidence else 'failed'
        except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
            task.stop_reason = f'{type(exc).__name__}: {exc}'
            task.logs.append({'stage': 'react_error', 'success': False, 'error': task.stop_reason})
            if task.protocol_version >= 4 and task.evidence and not (task.final_reviewed or task.delivery_kind == 'agent_report'):
                try:
                    if budget:
                        budget.phase = 'closing'
                        budget.check()
                    self._deliver(task, stopping=True)
                except (BudgetStop, RuntimeError, ValueError, TypeError, KeyError, OSError) as closing_error:
                    task.logs.append({'stage': 'closing_unavailable', 'success': False, 'error': str(closing_error)})
            if not task.final_status:
                task.final_status = 'partial' if task.evidence else 'failed'
        finally:
            try:
                if (task.protocol_version >= 4 and task.final_status in {'partial', 'failed'}
                        and not (task.final_reviewed or task.delivery_kind == 'agent_report')):
                    self._deliver_saved_results(task)
                task.report_only = False
                try:
                    self._persist(task)
                except InvestigationPersistenceError as exc:
                    task.stop_reason = str(exc)
                    task.final_status = 'partial' if task.evidence else 'failed'
                    task.final_reviewed = False
                    task.logs.append({'stage': 'storage_error', 'success': False, 'error': str(exc)})
            finally:
                task.report_only = False
                if budget is not None:
                    budget.checkpoint_callback = previous_callback
        return self._result(task)

    def _deliver_saved_results(self, task: ReactRecord) -> None:
        """At most two local Agent revisions within the original time budget."""
        budget = current_budget.get()
        # A transport/budget failure is not repaired by sending the same call again.
        closing_failed = any(log.get('stage') == 'closing_unavailable' for log in task.logs[-3:])
        can_attempt = not closing_failed and (budget is None or budget.seconds - budget.elapsed() > 5)
        for revision in range(2 if can_attempt else 0):
            if budget and budget.seconds - budget.elapsed() <= 5:
                break
            previous_draft = task.draft
            task.feedback = _json({'instruction': '保留正确发现，依据真实结果修正审核指出的错误；'
                '不要照抄审核中的数字，使用numeric_reading_view核对。已知结论写在前，待核实事项放篇末。'
                '本次只整理报告，不再查数；不要求把所有原因查完才交付已有可靠分析。',
                'revision_attempt': revision + 1,
                'final_revision_policy': ('这是最后一次局部修订；删除仍无证据的判断及依赖建议，保留独立可靠发现，'
                    '将未确认原因列为未决项。不要为了完整故事继续保留受质疑的断言。' if revision else ''),
                'review': {key: task.reviews[-1].get(key) for key in ('audit_findings', 'controller_reason', 'reason', 'repair_requests', 'controller_repair_requests')} if task.reviews else None,
                'chart_diagnostics': task.report_diagnostics})
            task.observation = {'type': 'delivery_repair'}
            try:
                if budget:
                    budget.phase = 'closing'
                    budget.check()
                self._deliver(task, stopping=True)
            except (BudgetStop, RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
                task.logs.append({'stage': 'report_revision_unavailable', 'success': False, 'error': str(exc)})
                break
            if (task.final_reviewed or task.delivery_kind == 'agent_report' or not task.reviews
                    or task.reviews[-1].get('controller_action') != 'report_revision'
                    or task.draft == previous_draft):
                break
        if not (task.final_reviewed or task.delivery_kind == 'agent_report'):
            task.approved_report = None
            task.approved_text = ''
            task.delivery_kind = ''
            task.final_status = 'partial' if task.evidence else 'failed'
            task.logs.append({'stage': 'report_delivery_unavailable', 'result_ids': [e['id'] for e in task.evidence],
                              'reason': '未取得可交付的Agent报告；保留查询结果及模型记录，不生成模板报告。'})

    @staticmethod
    def _check_sql_generation_gate(task: ReactRecord) -> None:
        """Close exhausted failed SQL before any further investigation model call.

        The executor finishes independent members of the current batch first.
        Valid results are retained for draft/review; a successful second
        candidate may continue normal investigation. Restore and explicit
        resume use the same durable action counts and cannot reopen this gate.
        """
        budget = current_budget.get()
        for action in task.actions:
            if action.get('action_type') != 'query' or action.get('status') == 'completed':
                continue
            limit = ReactInvestigationHarness._sql_limit(task, action)
            used = action.get('sql_generations', action.get('attempts', 1))
            if used >= limit:
                if task.protocol_version >= 3:
                    # Exhaustion closes this logical query, not every other
                    # unanswered question. Its durable ledger stays locked.
                    action['generation_closed'] = True
                    continue
                stop = BudgetStop(f'查询{action["id"]}已用完{limit}次SQL生成且未成功；'
                                  '停止无进展调查并收尾，不再调用模型重写')
                stop.context = {'query_id': action['id'], 'sql_generations': used,
                                'sql_generation_limit': limit, 'stopped_before_model_call': True}
                raise stop

    def _call(self, task: ReactRecord, node: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        if node == 'react':
            self._check_sql_generation_gate(task)
        budget = current_budget.get()
        if budget:
            budget.model_node = node
        if node == 'react':
            system = react_system_for_skills(set(task.skills), task.protocol_version)
            if task.protocol_version >= 3:
                system = effective_prompt('react', system)
        elif node == 'delivery_review':
            system = effective_prompt('review', system)
        sql_node = node in {'query_coder', 'sql_repair'} or (node == 'react' and task.protocol_version < 2)
        if sql_node and (task.protocol_version < 2 or node == 'react'):
            default_instructions = getattr(self, 'query_instructions', '')
            instructions = (effective_skill('database_query', default_instructions).split('## 输出协议')[0].strip()
                            if default_instructions else '')
            if instructions:
                system += '\n\n【内置查询技术能力 database_query，无需另行load_skill】\n' + instructions
                if budget:
                    budget.skill('database_query', instructions, node)
            payload['query_protocol'] = {'query_contract_schema': model_contract_schema(),
                'automatic_fields': ['original_question', 'data_source', 'tables', 'dimensions',
                                     'filters', 'time_range', 'time_ranges', 'grain'],
                'instructions': '机械字段可省略，由服务端按实际SQL补齐；主动声明的明确条件仍校验，不以补全覆盖冲突。业务指标名称、来源和计算必须真实，不猜字段。'}
        methods = []
        for name, entry in task.skills.items():
            if node in {'draft', 'delivery_review'} and task.protocol_version >= 2:
                # DataQa supplies its own reporting Skill. Investigator routing
                # and query-planning instructions are not report instructions.
                continue
            if name == 'database_query' and node in {'react', 'query_coder', 'sql_repair'} and getattr(self, 'query_instructions', ''):
                continue  # Built-in technical policy is already present once.
            if node in {'draft', 'delivery_review'} and name in {'database_query', 'data_qa'}:
                continue
            if node in {'query_coder', 'sql_repair', 'sql_calculation_repair'} and name != 'database_query':
                continue
            methods.append(f'【已加载 Skill：{name}，版本 {entry["version"]}】\n{entry["instructions"]}')
            if budget:
                budget.skill(name, entry['instructions'], node)
        if methods:
            system += '\n\n' + '\n\n'.join(methods)
        if task.protocol_version >= 4:
            # Actual schemas are sent once through response_format. Keep
            # execution feedback and business meaning; omit duplicate formats.
            payload = dict(payload)
            payload.pop('progress_protocol', None)
            payload.pop('calculator_protocol', None)
            protocol = payload.get('query_protocol')
            if isinstance(protocol, dict) and isinstance(protocol.get('output_schema'), dict):
                payload['query_protocol'] = {key: value for key, value in protocol.items() if key != 'output_schema'}
                payload['query_protocol']['format_source'] = 'response_format.json_schema'
            system += '\n格式以本次接口的JSON Schema为准；不另生成字段或在进度记录中新增任意键。'
        # Context remains selected by relevance and result paging. Token
        # estimates are observations, never an admission or truncation rule.
        if budget:
            for name, value in payload.items():
                budget.record('context_block', node=node, block=name, characters=len(_prompt_json(value)),
                              estimated_tokens=len(_prompt_json(value).encode('utf-8')), token_estimate=True,
                              estimator='utf8_byte_upper_bound_not_exact_tokenization')
        schema = None
        if task.protocol_version >= 4:
            from .investigation_output import output_schema
            schema_node = 'report_revision' if node == 'draft' and payload.get('mode') == 'report_revision' else node
            schema = output_schema(schema_node, skill_names=[entry['name'] for entry in self.workflow.skills.list()
                                                      if entry['name'] not in task.skills and entry['name'] != 'database_query'],
                                   result_columns={e['id']:e.get('columns') or [] for e in task.evidence
                                                   if e.get('check_status') not in {'superseded','invalid','failed'}},
                query_ids=[a['id'] for a in task.actions if a.get('action_type')=='query'
                           and not a.get('generation_closed')
                           and a.get('status') not in {'generating','rewriting','interrupted'}],
                review_context_refs=list((payload.get('review_sources') or {}).keys()))
        if node == 'draft' and task.protocol_version >= 4:
            signature = _fingerprint({'system': system, 'payload': payload, 'schema': schema})
            if signature in task.draft_request_signatures:
                raise BudgetStop('报告实际请求没有变化，停止重复生成；保留已有草稿和审核意见')
            task.draft_request_signatures.append(signature)
            self._persist(task)
        with using_output_schema('investigation_' + node, schema) if schema is not None else nullcontext():
            result = self.model.chat_json(system, (json.dumps(payload, ensure_ascii=False, default=str, separators=(',', ':'))
                                                  if node == 'delivery_review' else _prompt_json(payload)))
        if not isinstance(result, dict):
            raise ModelOutputError(f'{node} 未返回 JSON 对象')
        task.logs.append({'stage': node, 'success': True, 'revision': task.revision})
        if node == 'react':
            task.logs.append({'stage': 'react_decision', 'revision': task.revision,
                              'action_reason': str(result.get('action_reason') or ''),
                              'action': result.get('action'), 'progress_update': result.get('progress'),
                              'input_evidence_ids': [entry.get('id') for entry in payload.get('evidence', [])],
                              'loaded_skills': list(task.skills)})
        return result

    def _context(self, task: ReactRecord, *, closing: bool = False, role: str = '') -> dict[str, Any]:
        budget = current_budget.get()
        valid = [e for e in task.evidence if e.get('check_status') != 'superseded']
        # After a tool has returned data, describe the semantics actually used
        # by those results. Earlier exploratory metric requests are not an
        # instruction to keep all their fields in every subsequent model call.
        expand_prefetch = not closing and (not valid or task.observation.get('type') in {
            'semantics_retrieved', 'semantics_reused', 'skills_loaded'})
        # Retrieval ranking is a candidate pool, not a request to expand every
        # hit. Registered metric dependency closure and explicit field requests
        # select definitions; unrelated hits remain addressable in the cache.
        active = set(task.active_requested_fields) if expand_prefetch else set()
        if expand_prefetch and not task.active_metrics:
            active.update(task.active_fields)
        if not closing:
            for selected in (task.state.get('workspace') or {}).get('schema_fields') or []:
                if isinstance(selected, dict) and selected.get('tableId') and selected.get('name'):
                    active.add(f'{selected["tableId"]}.{selected["name"]}')
        # Keep definitions required to understand the actually used evidence,
        # without appending previous prompt envelopes or duplicate schema text.
        evidence_progress = task.progress
        if task.protocol_version >= 3:
            evidence_progress = {**task.progress, 'facts': [*(task.progress.get('facts') or []),
                *[{'evidence_ids': q.get('evidence_ids') or []} for q in task.progress.get('core_questions') or []],
                *[{'evidence_ids': [*(q.get('supporting_evidence_ids') or []), *(q.get('refuting_evidence_ids') or [])]}
                  for q in task.progress.get('explanations') or []]]}
        selected_evidence = select_evidence_ids(valid, progress=evidence_progress,
            requests=task.actions[-1:] if task.actions else [], observation=task.observation)
        considered = list(valid) if closing else [e for e in valid if e['id'] in selected_evidence]
        if not closing:
            referenced = set(task.actions[-1].get('input_result_ids') or []) if task.actions else set()
            if task.observation.get('result_id'):
                referenced.add(task.observation['result_id'])
            if isinstance(task.observation.get('result'), dict) and task.observation['result'].get('id'):
                referenced.add(task.observation['result']['id'])
            for result in task.observation.get('queries') or []:
                rid = result.get('result_id') or (result.get('result') or {}).get('id')
                if rid:
                    referenced.add(rid)
            present = {e['id'] for e in considered}
            considered.extend(e for e in valid if e['id'] in referenced and e['id'] not in present)
        metric_names = set()
        measurement_fields = set()
        for entry in considered:
            contract = entry.get('query_contract') or {}
            for metric in contract.get('metrics', []):
                active.update(metric.get('source_fields') or [])
                measurement_fields.update(metric.get('source_fields') or [])
                metric_names.add(metric.get('name'))
            active.update(contract.get('dimensions') or [])
            active.update(item.get('field') for item in contract.get('filters', []) if isinstance(item, dict))
            summary = (contract.get('validation') or {}).get('sql_summary') or {}
            active.update(summary.get('source_fields') or [])
            time_ranges = ([contract['time_range']] if contract.get('time_range') else []) + (contract.get('time_ranges') or [])
            active.update(item.get('field') for item in time_ranges if isinstance(item, dict))
        metric_ids = set(task.active_metrics) if expand_prefetch else set()
        for key, item in task.metric_pool.items():
            # A display alias can be normalized to a physical field by the
            # query contract. Keep already-retrieved authoritative definitions
            # for its measurement sources, so a final reviewer still sees
            # distinctions such as sessions versus people. This is relevance
            # selection only: it does not certify SQL implements that metric.
            source_related = measurement_fields & set(self._metric_field_references(item))
            if metric_names & {key, item.get('label'), *item.get('aliases', [])} or source_related:
                metric_ids.add(key)
        # Definitions needed by the provided evidence and their declared
        # dependencies travel together, even after another retrieval became
        # active. Closing does not inherit unrelated exploratory definitions.
        pending = list(metric_ids)
        while pending:
            item = task.metric_pool.get(pending.pop(), {})
            for dependency in item.get('depends_on') or []:
                if dependency in task.metric_pool and dependency not in metric_ids:
                    metric_ids.add(dependency)
                    pending.append(dependency)
        public_metrics = [item for key, item in task.metric_pool.items() if key in metric_ids]
        for item in public_metrics:
            active.update(self._metric_field_references(item))
            # Defensive closure for authoritative policies which mention a
            # physical field not repeated in required_fields. Only known
            # authorized fields can enter; this is not schema guessing.
            policy_text = _json({key: item.get(key) for key in (
                'formula', 'required_predicates', 'component_policies', 'as_of_policy', 'time_fields')})
            active.update(re.findall(r'\b[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*\b', policy_text))
        selected_graph = select_query_graph(task.state.get('schema_graph') or {},
            metrics=public_metrics, requested_fields=list(active), database=task.state['datasource_id'],
            access_scope=task.state.get('access_scope'))
        selected_fields = selected_graph.get('fields') or []
        table_ids = {v.get('table_id') for v in selected_fields}
        joins = selected_graph.get('joins') or []
        join_refs = {(j.get(f'{side}_table'), j.get(f'{side}_field')) for j in joins for side in ('left', 'right')}
        selected_ids = {v.get('id') or v.get('sql_name') for v in selected_fields}
        selected_fields.extend(v for v in task.field_pool.values()
                               if (v.get('table_id'), v.get('name')) in join_refs
                               and (v.get('id') or v.get('sql_name')) not in selected_ids)
        selected_ids = {v.get('id') or v.get('sql_name') for v in selected_fields}
        graph = {'database': task.state['datasource_id'],
                 'tables': [v for v in task.table_pool.values() if v.get('id') in table_ids],
                 'fields': [self._compact_field(v) for v in selected_fields], 'joins': joins}
        metrics = [self._compact_metric(item) for item in public_metrics]
        expanded_names = {name for item in public_metrics
                          for name in [item.get('id'), item.get('label'), *(item.get('aliases') or [])] if name}
        observation = json.loads(_json(task.observation))
        # Last-result rows already live in evidence; a second copy adds no
        # information. An explicit result-page read retains its requested rows.
        if observation.get('type') == 'query_batch':
            for entry in observation.get('queries', []):
                if isinstance(entry.get('result'), dict):
                    result = entry.pop('result')
                    entry['result_id'] = result.get('id')
        elif isinstance(observation.get('result'), dict):
            result = observation.pop('result')
            observation['result_id'] = result.get('id')
        if closing and observation.get('type') != 'delivery_repair':
            observation = {'type': 'delivery', 'stop_reason': task.stop_reason}
        public_budget = budget.public() if budget else {}
        payload = {
            'task': {'task_id': task.state['task_id'], 'question': task.state['query'],
                     'goal': ((task.state.get('task_understanding') or {}).get('goal')
                              or task.state.get('standalone_query') or task.state['query']),
                     'required_questions': task.state.get('required_questions') or [task.state['query']],
                     'scope_and_parameters': task.state.get('confirmed_parameters') or task.state.get('workspace', {}).get('confirmed_parameters') or {},
                     'datasource_id': task.state['datasource_id'], 'snapshot': task.state.get('scenario') or {},
                     'time_context': task.state.get('time_context') or {},
                     'contextual_findings': task.state.get('task_understanding') or {}},
            'data_capabilities': (self._capability_directory(task) if task.protocol_version >= 4 else
                                  homenova.public_capabilities(task.state['datasource_id'])
                                  if homenova.is_homenova(task.state['datasource_id']) else {}),
            'progress': task.progress,
            'semantics': {'schema_graph': sql_facing_graph(graph), 'registered_metrics': metrics,
                          'conflicts': [c for c in task.semantic_conflicts
                                        if c.get('id') in selected_ids or c.get('id') in metric_ids][-5:]},
            'loaded_skill_names': list(task.skills),
            'evidence': [self._evidence_context(task, e, 20 if not closing else 30,
                                                expanded_metric_names=expanded_names) for e in considered],
            'last_observation': observation, 'feedback': task.feedback,
            'budget': {key: public_budget[key] for key in ('tools', 'tool_limit', 'remaining_seconds', 'model_budget',
                                                         'react_rounds', 'react_round_limit', 'react_round_policy',
                                                         'react_decisions', 'sql_generation_limit')
                       if key in public_budget},
        }
        if task.protocol_version >= 4:
            from ..querying.task_measurements import measurement_memory, compact_measurement_memory
            payload['measurement_memory'] = compact_measurement_memory(measurement_memory(task),expanded_names)
        if task.protocol_version >= 3:
            payload['task']['execution_protocol_version'] = task.protocol_version
            payload['execution_protocol_version'] = task.protocol_version
            if task.progress_diagnostics:
                payload['progress_validation_feedback'] = task.progress_diagnostics
            if closing:
                payload['unsuccessful_attempts'] = [
                    {key: action.get(key) for key in ('id', 'goal', 'status', 'error', 'question_ids', 'result_ids')}
                    for action in task.actions if action.get('status') in {
                        'failed', 'blocked', 'interrupted', 'no_progress', 'needs_method_decision', 'awaiting_calculation'}]
                payload['unsuccessful_attempts_policy'] = (
                    '这些动作未取得目标证据；不等于目标现象不存在。其他成功证据可以补足，'
                    '但必须检查实际结果的字段和粒度，不能用平均值替代分布或用失败当零值。')
                for entry in payload['evidence']:
                    if entry.get('result_kind') == 'calculation_source':
                        entry['rows'] = []
                        entry['provided_rows'] = 0
                        entry['usable_for_conclusions'] = False
                        entry['interpretation'] = ('仅取得计算输入；不能把原始流水作为目标指标或变化的证据。'
                            '只可引用对应calculated_result；若计算未成功，相关解释仍未核实。')
                    elif entry.get('result_kind') == 'calculated_result' and not entry.get('calculation_complete'):
                        entry['usable_for_conclusions'] = False
                        entry['interpretation'] = '计算未完整完成；不能把空结果解释为零，保留诊断缺口。'
                # The final reviewer needs the same authorized table grains,
                # primary keys and join multiplicities as the SQL generator.
                # Aggregate rows alone cannot reveal multiplication by a join.
                payload['semantics'] = {'registered_metrics': metrics,
                    'conflicts': payload['semantics']['conflicts'],
                    **({'schema_graph': payload['semantics']['schema_graph']} if role == 'review' else {})}
                if role != 'review':
                    for entry in payload['evidence']:
                        entry.pop('sql', None)
                    payload['semantics']['registered_metrics'] = [{
                        key: value for key, value in item.items() if key in
                        {'id', 'label', 'definition', 'unit', 'grain', 'maturity', 'refund_policy'}}
                        for item in metrics]
        if not closing:
            if task.protocol_version >= 3:
                payload['progress_protocol'] = progress_output_schema()
            if task.protocol_version >= 3 and valid:
                from ..querying.calculations import model_tool_schema
                payload['calculator_protocol'] = model_tool_schema()
            if task.protocol_version >= 2:
                # Business decisions get meaning and available dimensions;
                # physical SQL metadata belongs to the SQL role's fresh view.
                payload['semantics'] = {
                    'registered_metrics': [{k: v for k, v in item.items() if k not in
                        {'required_fields', 'formula', 'required_predicates', 'component_policies', 'time_fields'}}
                        for item in metrics],
                    'available_dimensions': list(dict.fromkeys(
                        str(v.get('label') or v.get('name')) for v in task.field_pool.values()
                        if v.get('role') in {'dimension', 'time'} and (v.get('label') or v.get('name')))),
                    'conflicts': payload['semantics']['conflicts']}
                for entry in payload['evidence']:
                    entry.pop('sql', None)
            if observation.get('type') == 'result_page':
                for entry in payload['evidence']:
                    if entry.get('id') == observation.get('result_id'):
                        for key in ('rows', 'provided_rows', 'display_truncated'):
                            entry.pop(key, None)
                        entry['row_data_location'] = 'last_observation'
            payload['semantic_directory'] = {
                'fields': [v.get('sql_name') or k for k, v in task.field_pool.items()
                           if k not in selected_ids and v.get('id') not in selected_ids],
                'metrics': [{'id': k, 'label': v.get('label')} for k, v in task.metric_pool.items() if k not in metric_ids]}
            if task.protocol_version >= 2:
                payload['semantic_directory'].pop('fields', None)
            provided_ids = {e['id'] for e in considered}
            payload['evidence_directory'] = [{key: entry.get(key) for key in (
                'id', 'goal', 'columns', 'complete', 'total_rows', 'stored_rows', 'check_status')}
                for entry in valid if entry['id'] not in provided_ids]
            payload['skill_catalog'] = [{'name': v['name'], 'description': v['description']}
                                         for v in self.workflow.skills.list() if v['name'] != 'database_query']
            payload['query_directory'] = [{key: entry.get(key) for key in (
                'id', 'goal', 'status', 'input_result_ids', 'result_ids', 'sql_generations', 'sql_generation_limit',
                'business_request', 'batch_id', 'request_version')}
                for entry in task.actions if entry.get('action_type') == 'query']
        if task.protocol_version >= 4:
            from .business_context import current_progress, evidence_brief, investigation_checks
            payload['investigation_checks'] = investigation_checks(task)
            payload['clarification_history'] = task.clarification_history
            payload['progress'] = current_progress(task.progress)
            if closing and task.observation.get('type') == 'delivery_repair':
                previous = task.draft_report or {'markdown': task.draft}
                payload['previous_report'] = {k: v for k, v in previous.items() if k != 'data_sources'}
            if not closing:
                # No recursive expansion through historical claims or raw calculation inputs.
                fresh = {e['id'] for e in valid[-2:]}
                for item in (task.observation.get('queries') or []):
                    if item.get('result_id'):
                        fresh.add(item['result_id'])
                if task.observation.get('result_id'):
                    fresh.add(task.observation['result_id'])
                briefs = []
                for e in valid:
                    small = e.get('stored_rows', len(e.get('rows') or [])) <= 12
                    view = self._evidence_context(task, e, 20 if e['id'] in fresh or small else 0,
                                                  expanded_metric_names=expanded_names)
                    brief = evidence_brief(view, rows=e['id'] in fresh or small)
                    if observation.get('type') == 'result_page' and e['id'] == observation.get('result_id'):
                        brief.pop('rows', None)
                        brief['data_location'] = 'last_observation'
                    briefs.append(brief)
                payload['evidence'] = briefs
                payload['evidence_directory'] = [{'id': e['id'], 'goal': e.get('goal'),
                    'columns': e.get('columns'), 'total_rows': e.get('total_rows')} for e in valid]
                memory = payload.get('measurement_memory') or {}
                payload['measurement_memory'] = {
                    'conflicts': memory.get('conflicts', []),
                    'policy': '定义与绑定见semantics.registered_metrics及evidence.column_meanings；历史明细用read_result读取。'}
                payload['query_directory'] = [{k: a.get(k) for k in
                    ('id','goal','status','result_ids','sql_generations','sql_generation_limit')}
                    for a in task.actions if a.get('action_type') == 'query']
        return payload

    @staticmethod
    def _capability_directory(task: ReactRecord) -> dict:
        from copy import deepcopy
        source = task.state['datasource_id']
        scope = AccessScope.from_dict(task.state.get('access_scope'))
        result = deepcopy(homenova.public_capabilities(source)) if homenova.is_homenova(source) else {}
        # A metric ID list alone omits useful unregistered observations, such
        # as price history. This is a compact authorized discovery catalog,
        # not business evidence or an assertion that every period has rows.
        result['available_fact_domains'] = [
            {key: table[key] for key in ('id', 'label') if key in table}
            for table in SCHEMA if str(table.get('database') or 'askdata_mock') == source
            and scope.allows_table(source, table['id'])]
        if homenova.is_homenova(source):
            result['metric_directory'] = [{k: m[k] for k in ('id', 'label') if k in m}
                                          for m in homenova.metrics(source)]
            result['supported_metric_ids'] = [metric['id'] for metric in result['metric_directory']]
        result['catalog_boundary'] = '目录说明可检索的公开能力；是否有特定期间/对象的记录仍需查证，未检索不是不支持。'
        return result

    @staticmethod
    def _metric_field_references(metric: dict) -> list[str]:
        references = []
        for item in metric.get('required_fields') or []:
            if isinstance(item, str):
                references.append(item)
            elif isinstance(item, dict) and item.get('table') and item.get('field'):
                references.append(f'{item["table"]}.{item["field"]}')
        for item in metric.get('time_fields') or []:
            if isinstance(item, str):
                references.append(item)
            elif isinstance(item, dict) and item.get('table') and item.get('field'):
                references.append(f'{item["table"]}.{item["field"]}')
        return list(dict.fromkeys(references))

    @staticmethod
    def _compact_field(item: dict) -> dict:
        # sql_name is the single physical identity; table_id/name/id repeat it.
        # Preserve all business meaning, types, allowed states and units.
        output = {key: item[key] for key in ('sql_name', 'label', 'description', 'role', 'aggregation',
                                            'sql_type', 'unit', 'nullable', 'enum_values') if key in item}
        if not item.get('sql_type') and item.get('type'):
            output['type'] = item['type']
        return output

    @classmethod
    def _compact_metric(cls, item: dict) -> dict:
        output = {key: item[key] for key in (
            'id', 'label', 'aliases', 'definition', 'formula', 'depends_on', 'required_predicates',
            'component_policies', 'time_fields', 'grain', 'unit', 'tax_policy', 'shipping_policy',
            'refund_policy', 'maturity', 'duplicate_risks', 'as_of_policy', 'fact_tables', 'join_dependencies',
            'version', 'semantic_version', 'source_version') if key in item}
        # Public metric dependencies repeat table/field IDs already present in
        # schema_graph. Keep each dependency's physical reference once.
        output['required_fields'] = cls._metric_field_references(item)
        return output

    def _inherit_semantics(self, task: ReactRecord, update: dict[str, Any]) -> None:
        graph = update.get('schema_graph') or {}
        for category, pool, key_name in [('tables', task.table_pool, 'id'), ('fields', task.field_pool, 'id')]:
            active_keys = []
            for item in graph.get(category, []):
                key = str(item.get(key_name) or item.get('sql_name') or item.get('name'))
                cleaned = {k: v for k, v in item.items() if k != 'score'}
                if key in pool and _fingerprint(pool[key]) != _fingerprint(cleaned):
                    # A different retrieval ranking is not a definition change.
                    old = pool[key]
                    incompatible = any(old.get(k) is not None and cleaned.get(k) is not None and old[k] != cleaned[k]
                                       for k in ('sql_name', 'sql_type', 'unit', 'aggregation', 'enum_values'))
                    if incompatible:
                        task.semantic_conflicts.append({'id': key, 'reason': '相同语义编号出现不一致定义，需核对',
                                                        'old': old, 'new': cleaned})
                        continue
                pool[key] = {**pool.get(key, {}), **cleaned}
                active_keys.append(key)
            if category == 'fields' and active_keys:
                task.active_fields = active_keys
        for join in graph.get('joins', []):
            task.join_pool[_fingerprint(join)] = join
        selected = update.get('selected_semantics') or []
        keys = []
        for metric in selected:
            key = str(metric.get('id') or metric.get('name') or '')
            if not key:
                continue
            if key in task.metric_pool and _fingerprint(task.metric_pool[key]) != _fingerprint(metric):
                task.semantic_conflicts.append({'id': key, 'reason': '登记指标定义发生变化，未静默合并',
                                                'old': task.metric_pool[key], 'new': metric})
                continue
            task.metric_pool[key] = metric
            keys.append(key)
        if keys:
            task.active_metrics = keys
        task.state['schema_graph'] = {'database': task.state['datasource_id'],
                                      'tables': list(task.table_pool.values()), 'fields': list(task.field_pool.values()),
                                      'joins': list(task.join_pool.values())}

    def _progress_evidence(self, task: ReactRecord) -> list[dict]:
        """Adapter authority is the current task's validated, scoped catalog."""
        task_id, user_id, datasource = self._identity(task)
        return [entry for entry in task.evidence
                if (entry.get('task_id'), entry.get('user_id'), entry.get('datasource_id'))
                == (task_id, user_id, datasource)
                and (entry.get('snapshot') or {}) == (task.state.get('scenario') or {})]

    def _normalize_action_references(self, task: ReactRecord, action: dict,
                                     update: dict | None = None) -> dict:
        """Normalize explicit dependencies; never guess a missing dependency."""
        update = update or normalize_progress(None, existing_progress=task.progress,
                                              evidence=self._progress_evidence(task))
        catalog = self._progress_evidence(task)
        lookup = update['fact_lookup']
        normalized = json.loads(_json(action))
        changes = []

        def adapt(container: dict, key: str, path: str, *, scalar: bool = False) -> None:
            if key not in container:
                return
            value = container[key]
            resolved = resolve_references([value] if scalar else value, lookup, catalog, path=path)
            if not resolved['safe'] or (scalar and len(resolved['evidence_ids']) != 1):
                allowed = [e['id'] for e in catalog if e.get('check_status') in {'basic_valid', 'reviewed'}]
                raise ValueError(_json({'path': path, 'invalid_value': value,
                    'errors': resolved['errors'] or ['此动作必须唯一指向一个真实结果'],
                    'allowed_result_ids': allowed,
                    'message': '动作依赖不能安全解析；修正此字段，不必重做其他有效记录'}))
            container[key] = resolved['evidence_ids'][0] if scalar else resolved['evidence_ids']
            changes.extend(resolved['normalizations'])
            if value != container[key] and not resolved['normalizations']:
                changes.append({'path': path, 'from': value, 'to': container[key]})

        adapt(normalized, 'input_result_ids', 'action.input_result_ids')
        adapt(normalized, 'result_id', 'action.result_id', scalar=True)
        if isinstance(normalized.get('queries'), list):
            for index, query in enumerate(normalized['queries']):
                if isinstance(query, dict):
                    adapt(query, 'input_result_ids', f'action.queries[{index}].input_result_ids')
        if changes:
            task.logs.append({'stage': 'action_reference_normalization', 'revision': task.revision,
                              'normalizations': changes})
        return normalized

    def _update_progress(self, task: ReactRecord, progress: Any) -> dict | None:
        diagnostics = None
        if task.protocol_version >= 3:
            diagnostics = normalize_progress(progress, existing_progress=task.progress,
                                             evidence=self._progress_evidence(task))
            progress = diagnostics['progress']
            task.progress_diagnostics = {key: diagnostics[key] for key in
                                        ('normalizations', 'warnings', 'rejected_fields')}
            if any(task.progress_diagnostics.values()):
                task.logs.append({'stage': 'progress_adaptation', 'revision': task.revision,
                                  **task.progress_diagnostics})
        if progress is None:
            return diagnostics
        if not isinstance(progress, dict):
            raise ValueError('progress 必须是结构化对象')
        updated = json.loads(_json(task.progress))
        valid_ids = {e['id'] for e in task.evidence if e.get('check_status') != 'superseded'}
        for category in ('facts', 'hypotheses'):
            if category not in progress:
                continue
            values = progress[category]
            if not isinstance(values, list):
                raise ValueError(f'progress.{category} 必须是数组')
            normalized = []
            for item in values:
                if not isinstance(item, dict) or not str(item.get('text') or '').strip():
                    raise ValueError('调查记录条目缺少text')
                refs = item.get('evidence_ids') or []
                if not isinstance(refs, list) or not set(refs) <= valid_ids:
                    raise ValueError('调查记录引用未知或已失效证据')
                if category == 'facts' and not refs:
                    raise ValueError('已确认事实必须引用真实证据；用户描述应保留为线索')
                normalized.append({key: value for key, value in item.items()
                                   if key in {'id', 'text', 'evidence_ids', 'fact_ids', 'status', 'scope'}})
            if task.protocol_version >= 2:
                existing = list(updated.get(category) or [])
                for item in normalized:
                    identity = item.get('id') or item['text']
                    previous = next((i for i, old in enumerate(existing)
                                     if (old.get('id') or old.get('text')) == identity), None)
                    if previous is None:
                        existing.append(item)
                    else:
                        existing[previous] = item
                updated[category] = existing
            else:
                updated[category] = normalized
        if 'unresolved_questions' in progress:
            values = progress['unresolved_questions']
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError('未解决问题必须为字符串数组')
            updated['unresolved_questions'] = values
        if task.protocol_version >= 3:
            updated = merge_progress_records(updated, progress, valid_ids)
        for category in ('facts', 'hypotheses', 'explanations', 'core_questions'):
            old = {str(item.get('id') or item.get('text') or item.get('question')): item for item in task.progress.get(category, [])}
            for item in updated.get(category, []):
                identity = str(item.get('id') or item.get('text') or item.get('question'))
                if old.get(identity) != item:
                    task.business_note_rounds[category + ':' + identity] = task.revision + 1
        task.progress = updated
        return diagnostics

    def _new_action(self, task: ReactRecord, kind: str, goal: str, inputs: list[str] | None = None) -> dict:
        action = {'id': f'A{len(task.actions)+1}', 'kind': 'query' if kind == 'query' else 'analysis',
                  'action_type': kind, 'goal': goal, 'status': 'running', 'business_round': task.revision+1, 'input_result_ids': inputs or [],
                  'result_ids': [], 'depends_on': [], 'attempts': 1}
        if kind == 'query' and current_budget.get():
            action['evidence_round'] = current_budget.get().react_rounds
        task.actions.append(action)
        return action

    def _dispatch(self, task: ReactRecord, request: dict, reason: str) -> None:
        kind = request.get('type')
        if kind == 'repair_query' and task.protocol_version >= 4:
            self._repair_business_result(task,request,reason)
            return
        if kind == 'query_batch':
            self._business_query_batch(task, request, reason)
            return
        if kind in {'query', 'retrieve'} and task.protocol_version >= 2:
            self._protocol_error(task, '经营调查请提出query_batch业务取数；SQL模块自动准备字段及生成SQL')
            return
        if kind == 'calculate':
            self._calculate_action(task, request, reason)
            return
        if kind == 'query':
            budget = current_budget.get()
            if budget:
                budget.query_round()
            queries = request.get('queries')
            if not isinstance(queries, list) or not queries or not all(isinstance(q, dict) for q in queries):
                self._protocol_error(task, 'query 必须包含非空 queries 数组')
                return
            observations = []
            batch_inputs = {e['id'] for e in task.evidence if e.get('check_status') != 'superseded'}
            for query in queries:
                outcome = self._query(task, query, reason, allowed_inputs=batch_inputs)
                if outcome.get('status') == 'failed':
                    outcome = self._repair_query(task, query, outcome, allowed_inputs=batch_inputs)
                observations.append(outcome)
            task.observation = {'type': 'query_batch', 'queries': observations}
            if observations and all(o.get('status') in {'no_progress', 'blocked', 'generation_exhausted'} for o in observations):
                raise BudgetStop('相关输入和失败产物没有变化，停止无进展重复调查')
            return
        action = self._new_action(task, str(kind), reason or str(request.get('goal') or kind))
        budget = current_budget.get()
        if budget:
            budget.step = action['id']
        self._persist(task)
        try:
            if kind == 'load_skill':
                names = request.get('names') or [request.get('name')]
                if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
                    raise ValueError('load_skill 缺少白名单名称')
                allowed = {s['name'] for s in self.workflow.skills.list()}
                if all(name in task.skills for name in names):
                    if task.protocol_version < 4:
                        raise BudgetStop('请求的Skill均已加载，没有新增调查进展，停止重复准备')
                    action.update(status='completed', reused=True)
                    task.observation = {'type': 'skills_reused', 'names': names,
                        'message': '方法正文已在当前上下文，已复用；请选择取证、读取结果、计算或交付，不重复准备。'}
                    task.logs.append({'stage': 'react_action', **action})
                    return
                for name in names:
                    if name not in allowed:
                        raise ValueError(f'Skill不在白名单：{name}')
                    if name in task.skills:
                        continue
                    if budget:
                        budget.tool('load_skill')
                    skill = self.workflow.skills.get(name)
                    task.skills[name] = {'instructions': skill.instructions,
                                         'version': _fingerprint(skill.instructions)}
                    if budget:
                        budget.record('skill_loaded', skill=name, version=task.skills[name]['version'],
                                      characters=len(skill.instructions))
                task.observation = {'type': 'skills_loaded', 'names': names}
            elif kind == 'retrieve':
                self._retrieve(task, request)
            elif kind == 'resolve_time':
                self._resolve_time(task, request)
            elif kind == 'read_result':
                evidence, data = self._read_evidence(task, request.get('result_id'))
                offset, limit = int(request.get('offset', 0)), int(request.get('limit', 200))
                if offset < 0 or not 1 <= limit <= 1000:
                    raise ValueError('结果分页范围无效')
                columns = request.get('columns') or data.get('columns') or []
                missing_columns = sorted(set(columns) - set(data.get('columns') or []))
                if missing_columns:
                    # Reading a projection is presentation, not a new query.
                    # Show real columns, never invent a requested alias.
                    columns = list(data.get('columns') or [])
                signature = _fingerprint({'result':evidence['id'], 'version':evidence.get('fingerprint'),
                                          'offset':min(offset,len(data['rows'])), 'end':min(offset+limit,len(data['rows'])), 'columns':sorted(columns)})
                previous = [a for a in task.actions if a.get('read_signature') == signature
                            and a.get('status') == 'completed']
                action['read_signature'] = signature
                if previous:
                    action['cache_hit'] = True
                    if len(previous) >= 3:
                        raise BudgetStop('同一版本结果的同一页反复读取，没有新增证据，停止空转')
                elif budget:
                    budget.tool('read_result')
                rows = [{k: row[k] for k in columns if k in row}
                        for row in data['rows'][offset:offset+limit]]
                task.observation = {'type': 'result_page', 'result_id': evidence['id'], 'columns': columns,
                                    'rows': rows, 'offset': offset, 'complete': data.get('complete'),
                                    'total_rows': data.get('total_rows'), 'provided_rows': len(rows),
                                    'has_more': offset+limit < len(data['rows']), 'cache_hit':bool(previous),
                                    'missing_requested_columns':missing_columns,
                                    'result_kind':evidence.get('result_kind','query_result'),
                                    'pending_calculation':evidence.get('pending_calculation'),
                                    'read_note':'只展示真实列；原始流水不等于已算出的目标指标。相同版本和分页已提供时应直接分析或处理待修复计算，不要反复读取。'}
            elif kind == 'clarify':
                clarification = {'parameter': request.get('parameter') or 'other',
                                 'question': str(request.get('question') or ''),
                                 'reason': str(request.get('reason') or ''), 'options': [],
                                 'required': bool(request.get('required', True)) or not bool(task.evidence),
                                 'allow_skip': request.get('required') is False and bool(task.evidence)}
                if any(h.get('question') == clarification['question'] and h.get('status') in {'answered', 'skipped'}
                       for h in task.clarification_history):
                    task.observation = {'type': 'clarification_already_handled', 'message': '此问题已回答或跳过，请依据已有记录继续或交付。'}
                    action['status'] = 'completed'
                    return
                task.clarification = Clarification.model_validate(clarification).model_dump()
                if not task.clarification['question']:
                    raise ValueError('澄清问题不能为空')
                task.final_status = 'waiting_clarification'
                task.observation = {'type': 'clarification', **task.clarification}
            else:
                raise ValueError(f'未知动作类型：{kind}')
            action['status'] = 'completed'
        except BudgetStop:
            action.update(status='not_executed', error='预算停止')
            raise
        except InvestigationPersistenceError as exc:
            action.update(status='failed', error=str(exc), error_kind='storage')
            raise
        except PipelineStageError as exc:
            if exc.stage == 'result_storage':
                action.update(status='failed', error=str(exc), error_kind='storage')
                raise InvestigationPersistenceError(str(exc)) from exc
            raise
        except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
            action.update(status='failed', error=str(exc))
            if kind in {'load_skill', 'retrieve', 'resolve_time'}:
                action['error_kind'] = 'preparation'
                task.observation = {'type': 'preparation_error', 'action_id': action['id'],
                    'category': 'skill_loading' if kind == 'load_skill' else kind, 'error': str(exc),
                    'instruction': '修正本准备动作或选择不依赖它的合法取证；这不是SQL错误，不应重写SQL。'}
            else:
                task.observation = {'type': 'action_error', 'action_id': action['id'], 'error': str(exc)}
            self._charge_protocol_repair(task, action, str(exc))
        task.logs.append({'stage': 'react_action', **action})

    def _resolve_time(self, task: ReactRecord, request: dict) -> None:
        if set(request) - {'type', 'expression', 'timezone_name'}:
            raise ValueError('resolve_time只接受expression和timezone_name，不能指定任意工具')
        expression = request.get('expression') or ''
        zone = request.get('timezone_name') or (task.state.get('time_context') or {}).get('timezone') or 'UTC'
        if not isinstance(expression, str) or not isinstance(zone, str) or len(expression) > 100:
            raise ValueError('日期表达式和时区参数无效')
        params = task.state.setdefault('confirmed_parameters', {})
        ranges = params.setdefault('time_ranges', {})
        cache_key = _json({'expression': expression, 'timezone_name': zone})
        if cache_key in ranges:
            task.observation = {'type': 'time_resolved', 'value': ranges[cache_key], 'cached': True}
            return
        budget = current_budget.get()
        if budget and not getattr(budget, 'reference_now', ''):
            budget.reference_now = (task.state.get('time_context') or {}).get('reference_now') or ''
        # Tool names are code constants. The MCP schema validates the expression
        # and the datasource's allowed timezone enum, without arbitrary dispatch.
        if expression:
            name, arguments = 'resolve_date_range', {'expression': expression, 'timezone_name': zone}
        else:
            name, arguments = 'current_datetime', {'timezone_name': zone}
        result = self.workflow.mcp_client(task.state['access_scope']).call_tool(name, arguments)
        if not isinstance(result, dict):
            raise ValueError('时间工具没有返回结构化结果')
        if expression and not all(result.get(key) for key in ('start_date', 'end_date')):
            raise ValueError('时间工具结果缺少明确日期边界')
        ranges[cache_key] = result
        if expression:
            params.setdefault('time_range', dict(result))
        task.observation = {'type': 'time_resolved', 'tool': name, 'value': result, 'cached': False}

    def _retrieve(self, task: ReactRecord, request: dict) -> None:
        wanted_fields = request.get('fields') or []
        wanted_metrics = request.get('metric_ids') or []
        if (not isinstance(wanted_fields, list) or not isinstance(wanted_metrics, list)
                or not all(isinstance(v, str) for v in [*wanted_fields, *wanted_metrics])):
            raise ValueError('检索 fields 和 metric_ids 必须为字符串数组')
        task.active_requested_fields = list(wanted_fields)
        query = str(request.get('goal') or task.state['query'])
        signature = _fingerprint({'source': task.state['datasource_id'], 'goal': query,
                                  'fields': sorted(wanted_fields), 'metrics': sorted(wanted_metrics)})
        previous = next((r for r in reversed(task.retrievals) if r.get('request_signature') == signature), None)
        if previous and previous.get('missing_requests'):
            raise BudgetStop('相同语义缺口和检索请求没有变化，停止重复Embedding／Rerank')
        available = {v.get('sql_name') for v in task.field_pool.values()} | set(task.field_pool)
        if (wanted_fields or wanted_metrics) and set(wanted_fields) <= available and set(wanted_metrics) <= set(task.metric_pool):
            if task.observation.get('type') == 'semantics_reused' and task.observation.get('request_signature') == signature:
                raise BudgetStop('相同已缓存语义被重复读取，没有新增调查进展')
            task.active_fields = [k for k, v in task.field_pool.items()
                                  if k in wanted_fields or v.get('sql_name') in wanted_fields]
            if wanted_metrics:
                task.active_metrics = list(wanted_metrics)
            task.observation = {'type': 'semantics_reused', 'fields': wanted_fields, 'metric_ids': wanted_metrics,
                                'request_signature': signature}
            return
        if previous:
            task.active_fields = previous.get('active_fields') or task.active_fields
            task.active_metrics = previous.get('active_metrics') or task.active_metrics
            task.observation = {'type': 'semantics_reused', 'request_signature': signature,
                                'fields': len(task.active_fields), 'metric_ids': task.active_metrics}
            return
        scope = AccessScope.from_dict(task.state['access_scope'])
        known = {}
        for table in SCHEMA:
            if (table.get('database', 'askdata_mock') != task.state['datasource_id']
                    or not scope.allows_table(task.state['datasource_id'], table['id'])):
                continue
            for field_spec in table.get('fields', []):
                value = {'tableId': table['id'], 'name': field_spec['name']}
                known[f'{physical_table_name(table)}.{field_spec["name"]}'] = value
                known[f'{table["id"]}.{field_spec["name"]}'] = value
        workspace = dict(task.state.get('workspace') or {})
        explicit_fields = [known[name] for name in sorted(set(wanted_fields)) if name in known]
        workspace['schema_fields'] = list({(item['tableId'], item['name']): item
                                          for item in [*(workspace.get('schema_fields') or []), *explicit_fields]
                                          if item.get('tableId') and item.get('name')}.values())
        original_extraction = task.state.get('extraction') or {}
        understanding = task.state.get('task_understanding') or {}
        time_expressions = list(original_extraction.get('time_expressions') or [])
        for value in (understanding.get('period'), understanding.get('baseline'),
                      (task.state.get('confirmed_parameters') or {}).get('time_range')):
            if value:
                time_expressions.append(str(value))
        if not time_expressions and re.search(r'20\d{2}|最近|本月|上月|今天|昨天|本周|近\d+', task.state['query']):
            time_expressions.append(task.state['query'])
        terms = [query, task.state['query'], *time_expressions, *wanted_metrics, *wanted_fields]
        query_with_ids = query + ('；明确需要的登记指标：' + '、'.join(wanted_metrics) if wanted_metrics else '')
        state = {**task.state, 'workspace': workspace, 'standalone_query': query_with_ids,
                 'extraction': {**original_extraction, 'retrieval_terms': list(dict.fromkeys(terms)),
                                'metric_ids': wanted_metrics, 'time_expressions': time_expressions}, 'result': {}}
        update = self.workflow._retrieve_schema(state, charge_budget=True)
        if update.get('result'):
            raise ValueError('新增语义未检索到可用资料；已有有效资料仍保留')
        self._inherit_semantics(task, update)
        retrieval = update.get('retrieval') or {}
        available = {v.get('sql_name') for v in task.field_pool.values()} | set(task.field_pool)
        missing = {'fields': sorted(set(wanted_fields)-available),
                   'metric_ids': sorted(set(wanted_metrics)-set(task.metric_pool))}
        task.retrievals.append({'query': query, 'request_signature': signature,
                               'requested_fields': list(wanted_fields), 'requested_metric_ids': list(wanted_metrics),
                               'active_fields': list(task.active_fields), 'active_metrics': list(task.active_metrics),
                               'missing_requests': missing if any(missing.values()) else None,
                               **{k: retrieval[k] for k in (
            'embedding_source', 'rerank_source', 'selected_count', 'bm25_count', 'dense_count') if k in retrieval}})
        task.observation = {'type': 'semantics_retrieved', 'fields': len(task.active_fields),
                            'metric_ids': list(task.active_metrics), 'missing_requests': missing}

    def _repair_query(self, task: ReactRecord, request: dict, outcome: dict, *,
                      allowed_inputs: set[str] | None = None) -> dict:
        """One server-bound SQL rewrite, with no new investigation decision."""
        action = next((entry for entry in task.actions
                       if entry.get('id') == outcome.get('action_id') and entry.get('action_type') == 'query'), None)
        if not action or outcome.get('status') != 'failed':
            return outcome
        budget = current_budget.get()
        limit = self._sql_limit(task, action)
        used = action.get('sql_generations', action.get('attempts', 1))
        if used >= limit:
            return {**outcome, 'sql_generations': used, 'sql_generation_limit': limit,
                    'repair_available': False, 'repair_instruction': '该逻辑SQL已达生成上限，保留独立有效结果，不再生成。'}
        if budget:
            budget.step = action['id']
            budget.repair('原逻辑SQL的一次技术重写，不占ReAct轮次')
        # Persist before the model call. Invalid JSON, network failure and
        # restart must not refund this generation or automatically replay it.
        action.update(status='rewriting', sql_generations=used + 1,
                      sql_generation_limit=limit, attempts=action.get('attempts', 1) + 1)
        self._persist(task)
        context = self._context(task)
        payload = {'task': context['task'], 'semantics': context['semantics'],
                   'query_to_repair': {'query_id': action['id'], 'goal': action['goal'],
                       'sql': action.get('sql'), 'query_contract': request.get('query_contract'),
                       'input_result_ids': list(action.get('input_result_ids') or []), 'error': outcome.get('error')},
                   'sql_generations': action['sql_generations'], 'sql_generation_limit': limit}
        try:
            replacement = self._call(task, 'sql_repair', SQL_REPAIR_SYSTEM, payload)
            if (set(replacement) != {'query_id', 'sql', 'query_contract'}
                    or replacement.get('query_id') != action['id']
                    or not isinstance(replacement.get('sql'), str) or not replacement['sql'].strip()
                    or not isinstance(replacement.get('query_contract'), dict)):
                raise ValueError('SQL重写只允许返回原query_id、非空sql及query_contract，不得改目标、依赖或提出新动作')
            updated = {**request, 'goal': action['goal'], 'sql': replacement['sql'],
                       'query_contract': replacement['query_contract'],
                       'input_result_ids': list(action.get('input_result_ids') or []),
                       'repair_action_id': action['id']}
            return self._query(task, updated, action['goal'], allowed_inputs=allowed_inputs,
                               generation_reserved=True)
        except (BudgetStop, InvestigationPersistenceError):
            action.update(status='failed', error='SQL重写中断或预算停止；生成次数不返还')
            raise
        except (ValueError, RuntimeError, TypeError, KeyError, OSError) as exc:
            action.update(status='failed', error=str(exc))
            return {'action_id': action['id'], 'status': 'failed', 'error': str(exc),
                    'sql_generations': action['sql_generations'], 'sql_generation_limit': limit,
                    'repair_available': False}
        finally:
            task.logs.append({'stage': 'sql_repair', 'action_id': action['id'],
                              'status': action['status'], 'sql_generations': action['sql_generations'],
                              'sql_generation_limit': limit, 'error': action.get('error')})
            self._persist(task)

    def _query(self, task: ReactRecord, request: dict, reason: str, *, allowed_inputs: set[str] | None = None,
               generation_reserved: bool = False) -> dict:
        goal, sql = str(request.get('goal') or reason), str(request.get('sql') or '')
        raw_refs = request.get('input_result_ids') or []
        refs_valid = isinstance(raw_refs, list) and all(isinstance(ref, str) for ref in raw_refs)
        refs = raw_refs if refs_valid else []
        valid = {e['id']: e for e in task.evidence if e.get('check_status') != 'superseded'}
        repair_id = request.get('repair_action_id')
        raw_contract = request.get('query_contract') or {}
        contract_valid = isinstance(raw_contract, dict)
        if not contract_valid:
            raw_contract = {}
        intent = {key: raw_contract.get(key) for key in ('tables', 'dimensions', 'filters', 'time_range', 'time_ranges', 'grain')}
        intent['metrics'] = [{key: metric.get(key) for key in ('source_fields', 'aggregation')}
                             for metric in raw_contract.get('metrics', []) if isinstance(metric, dict)]
        logical_key = _fingerprint({'source': task.state['datasource_id'], 'inputs': refs, 'intent': intent})
        action = next((a for a in task.actions if a['id'] == repair_id and a['action_type'] == 'query'), None)
        bound_mismatch = bool(action and (refs != (action.get('input_result_ids') or [])
                                         or goal != action.get('goal')))
        sql_signature = _fingerprint(sql.strip())
        if not action:
            action = next((a for a in task.actions if a['action_type'] == 'query' and a.get('logical_key') == logical_key
                           and a.get('input_result_ids') == refs and a.get('status') in {'failed', 'blocked'}), None)
        if not action:
            # Merely renaming a goal or a client-provided ID cannot create a
            # new allowance for exactly the same SQL and real inputs.
            action = next((a for a in task.actions if a.get('action_type') == 'query'
                           and (a.get('sql', '').strip() == sql.strip()
                                or sql_signature in a.get('sql_signatures', []))
                           and a.get('input_result_ids') == refs), None)
        previous_results = list(action.get('result_ids') or []) if action else []
        budget = current_budget.get()
        limit = self._sql_limit(task, action)
        if action:
            used = action.get('sql_generations', action.get('attempts', 1))
            if not generation_reserved and used >= limit:
                return {'action_id': action['id'], 'status': 'generation_exhausted',
                        'error': f'该逻辑SQL最多生成{limit}次（包含首次生成）',
                        'sql_generations': used, 'sql_generation_limit': limit, 'repair_available': False}
            if budget and not generation_reserved:
                budget.step = action['id']
                budget.repair('同一逻辑查询局部修复')
            if not generation_reserved:
                action.update(attempts=action['attempts']+1, sql_generations=used+1)
            action['status'] = 'running'
        else:
            if generation_reserved:
                raise ValueError('SQL重写必须绑定原逻辑动作')
            action = self._new_action(task, 'query', goal, refs)
            action['sql_generations'] = 1
        action['sql_generation_limit'] = limit
        if sql_signature not in action.setdefault('sql_signatures', []):
            action['sql_signatures'].append(sql_signature)
        action['logical_key'] = logical_key
        if budget:
            budget.step = action['id']
        # Logical identity ignores display names, but a repaired contract is a
        # changed execution candidate even when its SQL text stays identical.
        signature = _fingerprint({'sql': sql.strip(), 'contract': raw_contract, 'inputs': refs,
                                  'source': task.state['datasource_id'],
                                  **({'local_measurements': request.get('local_measurements') or [],
                                      'postprocess': request.get('postprocess')}
                                     if task.protocol_version >= 4 else {})})
        action['sql'] = sql
        if task.protocol_version >= 4 or request.get('local_measurements'):
            action['local_measurements'] = request.get('local_measurements') or []
            action['postprocess'] = request.get('postprocess')
        self._persist(task)
        try:
            # The SQL has already been generated even when its declared
            # dependencies or contract cannot enter execution. Count it first.
            if bound_mismatch:
                action.update(status='blocked', error='SQL重写不能更换原目标或输入依赖；新的取证需求须使用独立动作')
                return {'action_id': action['id'], 'status': 'blocked', 'error': action['error'],
                        'sql_generations': action['sql_generations'], 'sql_generation_limit': limit}
            if (not refs_valid or not set(refs) <= set(valid)
                    or (allowed_inputs is not None and not set(refs) <= allowed_inputs)):
                action.update(status='blocked', error='查询依赖不存在、已失效，或引用了同批尚未产生的结果')
                return {'action_id': action['id'], 'status': 'blocked', 'error': action['error'],
                        'sql_generations': action['sql_generations'], 'sql_generation_limit': limit}
            if any(not valid[ref].get('complete') for ref in refs):
                action.update(status='blocked', error='不完整上游结果不能作为完整数值依赖')
                return {'action_id': action['id'], 'status': 'blocked', 'error': action['error'],
                        'sql_generations': action['sql_generations'], 'sql_generation_limit': limit}
            if not contract_valid:
                raise ValueError('query_contract必须为结构化对象')
            if signature in task.completed_queries:
                rid = task.completed_queries[signature]
                action.update(status='completed', result_ids=[rid], reused=True)
                reused = {'action_id': action['id'], 'status': 'reused', 'result_id': rid,
                        'sql_generations': action['sql_generations'], 'sql_generation_limit': limit}
                if task.protocol_version >= 4 and request.get('postprocess'):
                    saved = next(e for e in task.evidence if e['id']==rid)
                    self._read_evidence(task,rid)
                    reused['result'] = self._evidence_context(task,saved,20)
                return reused
            if signature in task.failed_signatures:
                action.update(status='failed', error=task.failed_signatures[signature])
                task.feedback = '相同失败与相关产物未变化，禁止重复修复；改用有依据的新取证或有边界交付。'
                return {'action_id': action['id'], 'status': 'no_progress', 'error': action['error'],
                        'sql_generations': action['sql_generations'], 'sql_generation_limit': limit}
            # Business methods guide investigation; they are not authority to
            # execute SQL. The shared module always supplies query technology
            # and independently enforces actual scope, contract and SQL checks.
            datasource = task.state['datasource_id']
            sql, source_normalizations = canonical_sql(sql, datasource,
                task.state.get('schema_graph') or {}, task.state['access_scope'])
            if source_normalizations:
                action['sql'] = sql
                task.logs.append({'stage': 'sql_source_normalization', 'action_id': action['id'],
                                  'changes': source_normalizations})
            if task.protocol_version >= 2:
                execution = self._sql_module().execute_candidate(sql=sql, query_contract=request.get('query_contract'),
                    question=goal, database=datasource, schema_graph=task.state.get('schema_graph') or {},
                    access_scope=task.state['access_scope'],
                    validation_context={'confirmed_parameters': task.state.get('confirmed_parameters') or {},
                        'request': action.get('normalized_business_request'),
                        'entity_resolutions': action.get('entity_resolutions') or [],
                        'registered_metrics': list(task.metric_pool.values()),
                        'local_measurements': action.get('local_measurements') or [],
                        'postprocess': action.get('postprocess'),
                        'query_id': action['id']})
                sql, contract, result = execution['sql'], execution['query_contract'], execution['execution']
            else:
                contract = self.validator.validate(request.get('query_contract'), sql, task.state['query'], datasource,
                                                   task.state.get('schema_graph') or {}, task.state['access_scope'],
                                                   validation_context={'confirmed_parameters': task.state.get('confirmed_parameters') or {}})
                error = self.workflow.database_engine.preflight(datasource, sql, AccessScope.from_dict(task.state['access_scope']))
                if error:
                    raise ValueError('SQL编译／安全检查失败：' + error)
                result = self.workflow.mcp_client(task.state['access_scope']).call_tool(f'query_{datasource}', {'sql': sql})
            if result.get('error_kind') == 'storage':
                raise InvestigationPersistenceError(result.get('error') or '结果持久化失败，禁止重试SQL')
            if not result.get('success'):
                action['error_kind'] = result.get('error_kind') or 'sql_execution'
                raise RuntimeError(result.get('error') or '数据库查询失败')
            full = self._full_execution(task, result)
            result['query_contract'] = contract
            invalidated = self._invalidate(task, previous_results) if previous_results else set()
            evidence = self._save_evidence(task, action, result, full)
            task.completed_queries[signature] = evidence['id']
            task.failed_signatures.pop(signature, None)
            action.update(status='completed', result_ids=[evidence['id']], error=None)
            action['error_kind'] = None
            return {'action_id': action['id'], 'status': 'completed',
                    'sql_generations': action['sql_generations'], 'sql_generation_limit': limit,
                    'invalidated_result_ids': sorted(invalidated),
                    'result': self._evidence_context(task, evidence, 20)}
        except BudgetStop:
            action.update(status='not_executed', error='预算停止，未获得可靠新结果')
            raise
        except InvestigationPersistenceError as exc:
            action.update(status='failed', error=str(exc), error_kind='storage')
            raise
        except PipelineStageError as exc:
            if exc.stage == 'result_storage':
                action.update(status='failed', error=str(exc), error_kind='storage')
                raise InvestigationPersistenceError(str(exc)) from exc
            raise
        except PermissionError as exc:
            action.update(status='failed', error=str(exc), error_kind='permission')
            return {'action_id': action['id'], 'status': 'failed', 'error': str(exc),
                    'error_kind': 'permission', 'repair_available': False,
                    'sql_generations': action['sql_generations'], 'sql_generation_limit': limit}
        except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
            error = str(exc)
            task.failed_signatures[signature] = error
            action.update(status='failed', error=error)
            if task.protocol_version >= 3:
                from ..querying.measurement_context import repair_diagnostics
                action['repair_diagnostics'] = repair_diagnostics(exc, sql=sql,
                    query_contract=raw_contract, measurements=action.get('measurement_request') or {},
                    schema_graph=task.state.get('schema_graph') or {})
                action['error_kind'] = action.get('error_kind') or action['repair_diagnostics'].get('kind')
            return {'action_id': action['id'], 'status': 'failed', 'error': error,
                    'sql_generations': action['sql_generations'], 'sql_generation_limit': limit,
                    'attempted_sql': sql, 'query_contract': request.get('query_contract'),
                    'repair_instruction': '保持逻辑动作ID，修复SQL或明确契约冲突；不重复同一产物。'}
        finally:
            task.logs.append({'stage': 'query_action', **action})
            self._persist(task)

    def _full_execution(self, task: ReactRecord, data: dict) -> dict:
        ref = data.get('result_ref')
        if not ref:
            raise ValueError('真实查询结果缺少受控结果引用')
        task_id, user_id, datasource = self._identity(task)
        memory = self.workflow.database_engine.results.get(ref)
        if memory is None:
            memory = self.store.load_result(ref, user_id, task_id, datasource)
        if (memory.get('user_id'), memory.get('task_id'), memory.get('datasource_id')) != (user_id, task_id, datasource):
            raise PermissionError('查询结果引用范围不匹配')
        return memory

    def _save_evidence(self, task: ReactRecord, action: dict, data: dict, full: dict) -> dict:
        rid = f'{task.state["task_id"]}:R{len(task.evidence)+1}'
        evidence = {key: data[key] for key in ('sql', 'columns', 'complete', 'total_rows', 'query_contract', 'result_ref') if key in data}
        evidence.update(id=rid, step_id=action['id'], goal=action['goal'],
                        user_id=task.state['access_scope']['user_id'], task_id=task.state['task_id'],
                        datasource_id=task.state['datasource_id'], snapshot=task.state.get('scenario') or {},
                        input_result_ids=list(action.get('input_result_ids') or []),
                        rows=full['rows'][:20], display_truncated=len(full['rows'])>20,
                        complete=bool(full.get('complete')), total_rows=full.get('total_rows'),
                        stored_rows=len(full['rows']), check_status='basic_valid',
                        fingerprint=_fingerprint({'sql': data.get('sql'), 'columns': full.get('columns'), 'rows': full['rows']}))
        if task.protocol_version >= 3:
            evidence['question_ids'] = list(action.get('question_ids') or [])
            if action.get('execution_source') == 'registered_recipe':
                evidence['calculation_definition'] = action['recipe_selection']['recipe']
                evidence['execution_source'] = 'registered_recipe'
                evidence['interpretation_limits'] = list(action.get('interpretation_limits') or [])
            elif action.get('execution_source'):
                evidence['execution_source'] = action['execution_source']
            evidence['column_metadata'] = data.get('column_metadata') or (
                (data.get('query_contract') or {}).get('validation') or {}).get('column_metadata') or full.get('column_metadata') or {}
            if action.get('execution_source') == 'registered_recipe':
                # Compiler metadata describes the actual cohort/additivity; it
                # supplements the same validated columns, never invents data.
                for column, metadata in (action.get('recipe_output_metadata') or {}).items():
                    if column in evidence.get('columns', []):
                        evidence['column_metadata'][column] = {
                            **evidence['column_metadata'].get(column, {}), **metadata}
            evidence['fingerprint'] = _fingerprint({'sql': data.get('sql'), 'columns': full.get('columns'),
                'rows': full['rows'], 'query_contract': data.get('query_contract'),
                'column_metadata': evidence['column_metadata'],
                'calculation': {key: full.get(key) for key in ('operation', 'parameters', 'metric_metadata')}})
        task.evidence.append(evidence)
        previous_ids = list(action.get('result_ids') or [])
        action['result_ids'] = [rid]
        try:
            self._persist(task, [(data['result_ref'], full)])
        except InvestigationPersistenceError:
            task.evidence.remove(evidence)
            action['result_ids'] = previous_ids
            raise
        return evidence

    def _read_evidence(self, task: ReactRecord, rid: Any) -> tuple[dict, dict]:
        evidence = next((e for e in task.evidence if e['id'] == rid and e.get('check_status') != 'superseded'), None)
        if not evidence:
            raise ValueError('结果编号不存在或已失效')
        ref = evidence.get('result_ref')
        task_id, user_id, datasource = self._identity(task)
        data = self.workflow.database_engine.results.get(ref)
        if data is None:
            data = self.store.load_result(ref, user_id, task_id, datasource)
        if (data.get('user_id'), data.get('task_id'), data.get('datasource_id')) != (user_id, task_id, datasource):
            raise PermissionError('结果用户、任务或快照范围不匹配')
        return evidence, data

    def _evidence_context(self, task: ReactRecord, evidence: dict, limit: int,
                          *, expanded_metric_names: set[str] | None = None) -> dict:
        # Do not expose storage references. SQL and actual query-specific checks
        # remain available; definitions are supplied once in semantics.
        output = {k: v for k, v in evidence.items() if k not in {'result_ref', 'user_id', 'snapshot', 'fingerprint'}}
        output['version'] = evidence.get('version') or evidence.get('fingerprint') or _fingerprint(evidence)
        output['rows'] = list(evidence.get('rows') or [])[:limit]
        if limit > len(output['rows']) and evidence.get('stored_rows', 0) > len(output['rows']):
            try:
                _, data = self._read_evidence(task, evidence['id'])
                output['rows'] = data['rows'][:limit]
            except KeyError:
                output['evidence_read_error'] = '保存的结果引用不可用，不能视为已取得完整证据'
        output['provided_rows'] = len(output['rows'])
        output['display_truncated'] = evidence.get('stored_rows', len(output['rows'])) > len(output['rows'])
        contract = dict(output.get('query_contract') or {})
        validation = contract.get('validation') or {}
        metadata = output.get('column_metadata') or validation.get('column_metadata')
        if isinstance(metadata, dict):
            # Registered definitions already appear once in semantics. Keep
            # the actual output binding/unit caveats here; local/unresolved
            # measurements retain their complete definitions and boundaries.
            output['column_metadata'] = {column: ({key: value for key, value in entry.items()
                if key in {'kind', 'metric_id', 'measurement_id', 'measurement_version',
                           'binding_basis', 'source_unit', 'unit', 'requires_review',
                           'display_unit_check', 'statistical_entity'}}
                if entry.get('kind') == 'registered' and entry.get('metric_id') in (expanded_metric_names or set())
                else dict(entry)) for column, entry in metadata.items()}
        if contract:
            contract.pop('original_question', None)  # task.question is sent once.
            contract['metrics'] = [
                {k: v for k, v in metric.items()
                 if k != 'definition' or metric.get('name') not in (expanded_metric_names or set())}
                for metric in contract.get('metrics', [])]
            validation = contract.get('validation')
            if isinstance(validation, dict):
                compact = {key: value for key, value in validation.items()
                           if key not in {'normalizations', 'sql_summary', 'column_metadata'}}
                normalizations = validation.get('normalizations') or []
                compact['normalization_count'] = len(normalizations)
                if normalizations:
                    compact['normalized_paths'] = list(dict.fromkeys(
                        str(item.get('path')) for item in normalizations if isinstance(item, dict)))
                summary = validation.get('sql_summary') or {}
                if summary:
                    compact['sql_summary'] = {key: summary[key] for key in ('source_fields', 'predicates') if key in summary}
                contract['validation'] = compact
            output['query_contract'] = contract
        return output

    def _calculate_action(self, task: ReactRecord, request: dict, reason: str) -> None:
        """Bind calculation repairs to real inputs, never to a fresh display ID."""
        refs = [request.get('result_id')] if isinstance(request.get('result_id'), str) else []
        operation = request.get('operation')
        logical_key = _fingerprint({'source': task.state['datasource_id'], 'inputs': refs,
                                    'operation': operation})
        fields_by_operation = {'sum': ('column',), 'column_stats': ('column',),
                               'change': ('base_field', 'current_field'), 'ratio': ('numerator', 'denominator')}
        used_fields = fields_by_operation.get(operation, ()) if isinstance(operation, str) else ()
        parameters = {key: request.get(key) for key in ('operation', 'result_id', *used_fields)}
        if task.protocol_version >= 3:
            parameters = {key: value for key, value in request.items() if key not in
                          {'type', 'goal', 'repair_action_id', 'action_reason'}}
            try:
                from ..querying.calculations import parse_calculation_request
                parameters = parse_calculation_request(request).model_dump(mode='json')
            except ValueError:
                pass  # The real action records the precise invalid arguments.
        signature = 'calculate:' + _fingerprint({'source': task.state['datasource_id'], 'parameters': parameters})
        action = next((item for item in task.actions if item['action_type'] == 'calculate'
                       and item.get('logical_key') == logical_key and item.get('status') in {'failed', 'blocked'}), None)
        if not action:
            action = next((item for item in task.actions if item['action_type'] == 'calculate'
                           and item.get('calculation_signature') == signature and item.get('status') == 'completed'), None)
            if action:
                task.observation = {'type': 'calculation', 'action_id': action['id'], 'status': 'reused',
                                    'result_ids': list(action.get('result_ids') or [])}
                return
            action = self._new_action(task, 'calculate', reason or str(request.get('goal') or '计算真实结果'), refs)
        budget = current_budget.get()
        if budget:
            budget.step = action['id']
        action['logical_key'] = logical_key
        try:
            if signature in task.failed_signatures:
                task.observation = {'type': 'calculation_error', 'action_id': action['id'], 'status': 'no_progress',
                                    'error': task.failed_signatures[signature], 'input_result_ids': refs,
                                    'repair_instruction': '相同结果和参数已失败；不要改动作ID重试。应修正参数、查询所需宽表或有边界收尾。'}
                raise BudgetStop('相同计算参数与输入结果没有变化，停止无进展重复修复')
            if action.get('calculation_signature'):
                if budget:
                    budget.repair('同一逻辑计算局部修复')
                elif action['attempts'] >= 3:
                    raise BudgetStop('同一逻辑计算已用完2次额外修复')
                action['attempts'] += 1
            action.update(status='running', calculation_signature=signature)
            self._persist(task)
            if budget:
                budget.tool('calculate')
            self._calculate(task, action, request)
            action.update(status='completed', error=None)
            task.failed_signatures.pop(signature, None)
        except BudgetStop:
            if action['status'] == 'running':
                action.update(status='not_executed', error='预算停止')
            raise
        except InvestigationPersistenceError as exc:
            action.update(status='failed', error=str(exc), error_kind='storage')
            raise
        except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
            error = str(exc)
            action.update(status='failed', error=error)
            task.failed_signatures[signature] = error
            valid = next((item for item in task.evidence if item['id'] in refs
                          and item.get('check_status') != 'superseded'), None)
            task.observation = {'type': 'calculation_error', 'action_id': action['id'], 'status': 'failed',
                                'error': error, 'operation': operation, 'input_result_ids': refs,
                                'available_columns': list((valid or {}).get('columns') or []),
                                'expected_parameters': {
                                    'sum': ['column'], 'column_stats': ['column'],
                                    'change': ['base_field', 'current_field'],
                                    'ratio': ['numerator', 'denominator']}.get(operation, []) if isinstance(operation, str) else [],
                                'repair_instruction': '参数必须引用真实数值列名，不是行值。change逐行比较同一行的基期列和本期列；长表请用SQL条件聚合成宽表后计算。修复使用本action_id；不要重发相同参数。'}
            if task.protocol_version >= 3:
                from ..querying.calculations import model_tool_schema
                task.observation.update(error_code=getattr(exc, 'code', 'calculation_error'),
                    details=getattr(exc, 'details', {}), calculator_protocol=model_tool_schema(),
                    repair_instruction='按实际计算协议修正参数；长表用period_change，不必为简单期间比较重新查SQL。')
        finally:
            task.logs.append({'stage': 'react_action', **action})
            self._persist(task)

    def _calculate(self, task: ReactRecord, action: dict, request: dict) -> None:
        evidence, data = self._read_evidence(task, request.get('result_id'))
        action['input_result_ids'] = [evidence['id']]
        action['depends_on'] = [evidence['step_id']] if evidence.get('step_id') else []
        if not data.get('complete'):
            raise ValueError('完整聚合需要完整结果；当前结果超过保存上限，应重新查询完整聚合')
        if task.protocol_version >= 3:
            from ..querying.calculations import execute_calculation
            calculated = execute_calculation(request, {**data,
                'schema_graph': {'tables': list(task.table_pool.values()),
                                 'fields': list(task.field_pool.values())},
                'observation_cutoff_at': (task.state.get('scenario') or {}).get('observation_cutoff_at'),
                'column_metadata':
                                             evidence.get('column_metadata') or data.get('column_metadata') or {}})
            task_id, user_id, datasource = self._identity(task)
            ref = uuid.uuid4().hex
            full = {**calculated, 'user_id': user_id, 'task_id': task_id,
                    'datasource_id': datasource, 'sql': '', 'result_ref': ref}
            self.workflow.database_engine.results[ref] = full
            output = {'sql': '', 'columns': full['columns'], 'rows': full['rows'],
                      'result_ref': ref, 'complete': True, 'total_rows': len(full['rows'])}
            saved = self._save_evidence(task, action, output, full)
            saved.update({key: calculated[key] for key in ('column_metadata', 'metric_metadata',
                'diagnostics', 'calculation_complete', 'operation', 'parameters', 'interpretation',
                'group_field_map') if key in calculated})
            saved['source_result_id'] = evidence['id']
            saved['source_version'] = evidence.get('fingerprint') or evidence.get('version')
            task.observation = {'type': 'calculation', 'action_id': action['id'],
                                'result': self._evidence_context(task, saved, 30),
                                'calculation_complete': calculated.get('calculation_complete'),
                                'diagnostics': calculated.get('diagnostics') or []}
            return
        operation = request.get('operation')
        required = {'sum': ('column',), 'column_stats': ('column',),
                    'change': ('base_field', 'current_field'), 'ratio': ('numerator', 'denominator')}
        if not isinstance(operation, str) or operation not in required:
            raise ValueError('计算只允许sum、column_stats、change、ratio，不执行任意表达式或代码')
        for parameter in required[operation]:
            key = request.get(parameter)
            if not isinstance(key, str) or not key or key not in data['columns']:
                raise ValueError(f'计算参数{parameter}必须是真实结果列名，收到：{key}；可用列：{data["columns"]}')
        rows = data['rows']
        def number(row: dict, key: str) -> Decimal | None:
            if key not in data['columns']:
                raise ValueError(f'计算字段不存在：{key}')
            value = row.get(key)
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError('布尔字段不能当作数值')
            try:
                result = Decimal(str(value))
                if not result.is_finite():
                    raise ValueError('计算值不是有限数')
                return result
            except InvalidOperation as exc:
                raise ValueError('计算字段不是数值') from exc
        out = []
        if operation in {'sum', 'column_stats'}:
            column = str(request.get('column') or '')
            values = [value for row in rows if (value := number(row, column)) is not None]
            result = {'field': column, 'sum': float(sum(values, Decimal(0))) if values else None,
                      'non_null_count': len(values), 'row_count': len(rows)}
            if operation == 'column_stats':
                result.update(positive_sum=float(sum((v for v in values if v > 0), Decimal(0))),
                              negative_sum=float(sum((v for v in values if v < 0), Decimal(0))),
                              positive_count=sum(v > 0 for v in values), negative_count=sum(v < 0 for v in values))
            out = [result]
        elif operation in {'change', 'ratio'}:
            for row in rows:
                result = dict(row)
                if operation == 'change':
                    base = number(row, str(request.get('base_field') or ''))
                    current = number(row, str(request.get('current_field') or ''))
                    delta = current-base if current is not None and base is not None else None
                    result.update(change=float(delta) if delta is not None else None,
                                  change_rate=float(delta/base) if delta is not None and base else None)
                else:
                    numerator = number(row, str(request.get('numerator') or ''))
                    denominator = number(row, str(request.get('denominator') or ''))
                    result['ratio'] = float(numerator/denominator) if numerator is not None and denominator else None
                out.append(result)
        else:
            raise ValueError('计算只允许sum、column_stats、change、ratio，不执行任意表达式或代码')
        action['input_result_ids'] = [evidence['id']]
        ref = uuid.uuid4().hex
        task_id, user_id, datasource = self._identity(task)
        full = {'user_id': user_id, 'task_id': task_id, 'datasource_id': datasource,
                'sql': '', 'columns': list(out[0]) if out else [], 'rows': out, 'complete': True, 'total_rows': len(out)}
        self.workflow.database_engine.results[ref] = full
        saved = self._save_evidence(task, action, {**full, 'result_ref': ref,
                                   'query_contract': {'calculation': operation, 'parameters': request}}, full)
        task.observation = {'type': 'calculation', 'result': self._evidence_context(task, saved, 20)}

    def _reuse_input_results(self, task: ReactRecord, entries: list[dict]) -> None:
        task_id, user_id, datasource = self._identity(task)
        for entry in entries:
            if (entry.get('user_id'), entry.get('datasource_id')) != (user_id, datasource):
                continue
            if entry.get('check_status') not in {'basic_valid', 'reviewed'} or not entry.get('complete'):
                continue
            if (entry.get('snapshot') or {}) != (task.state.get('scenario') or {}):
                continue
            ref = entry.get('result_ref')
            if not ref:
                continue
            try:
                original = self.store.load_result(ref, user_id, entry['task_id'], datasource)
                self.workflow.database_engine.results[ref] = original
            except KeyError:
                original = self.workflow.database_engine.results.get(ref)
            if not original:
                continue
            cloned = self.workflow.database_engine.clone_result_reference(ref, user_id, entry['task_id'], task_id, datasource)
            action = self._new_action(task, 'reuse', str(entry.get('goal') or '复用已有证据'))
            full = self.workflow.database_engine.results[cloned]
            saved = self._save_evidence(task, action, {**entry, 'result_ref': cloned}, full)
            saved['source_result_id'] = entry['id']
            action.update(status='completed', result_ids=[saved['id']])

    def _protocol_error(self, task: ReactRecord, message: str) -> None:
        budget = current_budget.get()
        if budget:
            budget.step = 'react_protocol'
            if task.protocol_version >= 4:
                budget.step = 'protocol:' + _fingerprint(message)[:12]
                try:
                    budget.repair('ReAct动作格式修复')
                except BudgetStop:
                    task.observation = {'type': 'action_exhausted', 'error': message,
                        'message': '此动作重复失败且修复额度耗尽；保留其他有效结果，选择其他可行动作或有边界交付。'}
                    return
            else:
                budget.repair('ReAct动作格式修复')
        task.observation = {'type': 'protocol_error', 'error': message}

    def _charge_protocol_repair(self, task: ReactRecord, action: dict, message: str) -> None:
        budget = current_budget.get()
        if budget:
            budget.step = 'react_protocol'
            if task.protocol_version >= 4:
                budget.step = 'action:' + _fingerprint({'type': action.get('action_type'), 'message': message})[:12]
                try:
                    budget.repair(message)
                except BudgetStop:
                    action.update(status='blocked', repair_available=False)
                    task.observation = {'type': 'action_exhausted', 'action_id': action['id'], 'error': message,
                        'message': '该失败动作已关闭，其他独立有效结果保留；选择不同的可行取证或交付。'}
            else:
                budget.repair(message)

    def _invalidate(self, task: ReactRecord, ids: list[str]) -> set[str]:
        affected = set(ids)
        for entry in task.evidence:
            if entry['id'] in affected or set(entry.get('input_result_ids') or []) & affected:
                affected.add(entry['id'])
                entry['check_status'] = 'superseded'
        task.progress['facts'] = [f for f in task.progress['facts'] if not set(f.get('evidence_ids') or []) & affected]
        for hypothesis in task.progress['hypotheses']:
            if set(hypothesis.get('evidence_ids') or []) & affected:
                hypothesis['evidence_ids'] = [rid for rid in hypothesis.get('evidence_ids', []) if rid not in affected]
                hypothesis['status'] = 'open'
        for question in task.progress.get('core_questions') or []:
            if set(question.get('evidence_ids') or []) & affected:
                question['evidence_ids'] = [rid for rid in question.get('evidence_ids', []) if rid not in affected]
                question.update(status='open', reason='支撑证据已失效，需重新核实')
        for explanation in task.progress.get('explanations') or []:
            if any(set(explanation.get(key) or []) & affected for key in ('supporting_evidence_ids', 'refuting_evidence_ids')):
                for key in ('supporting_evidence_ids', 'refuting_evidence_ids'):
                    explanation[key] = [rid for rid in explanation.get(key, []) if rid not in affected]
                explanation['status'] = 'open'
        task.completed_queries = {signature: rid for signature, rid in task.completed_queries.items() if rid not in affected}
        for action in task.actions:
            if set(action.get('result_ids') or []) & affected:
                action.update(status='blocked', error='上游证据已修正，相关旧结果失效，需要按实际依赖重算')
        if affected:
            task.feedback = '已修正上游证据并使实际依赖失效：' + '、'.join(sorted(affected)) + '；只补回必要依赖，保留独立有效结果。'
        return affected

    def _deliver(self, task: ReactRecord, *, stopping: bool = False) -> None:
        budget = current_budget.get()
        if budget:
            budget.phase = 'closing'
            budget.step = 'closing'
        evidence_signature = _fingerprint({'evidence': [(e['id'], e.get('fingerprint'), e.get('check_status')) for e in task.evidence],
                                           'progress': task.progress, 'feedback': task.feedback})
        if task.delivery_evidence_signature == evidence_signature:
            task.stop_reason = task.stop_reason or '证据和修正要求没有实质变化，停止重复生成或审核同稿'
            task.final_status = 'partial' if task.evidence else 'failed'
            return
        task.delivery_evidence_signature = evidence_signature
        report_context = self._context(task, closing=True)
        if task.observation.get('type') == 'delivery_repair':
            report_context['last_observation'] = {'type': 'delivery_repair'}
            report_context['feedback'] = task.feedback
        if task.protocol_version >= 4:
            report_context['delivery_policy'] = {
                'stage': 'partial' if stopping else 'normal',
                'instruction': '面向业务负责人汇报，必须整合已有可信发现，不能把查询结果和证据清单当报告。'
                    '正文先给当前结论，再解释主要变化、已支持的原因判断和下一步。用少量关键数字支撑叙述；'
                    '月度对比优先四列：指标、基期、本期、变化。原始明细不逐表堆进正文。'
                    '即使partial，也先报告已查明什么；未完成事项、预算限制和待确认条件集中在篇末，'
                    '不以整段免责声明开篇。只在具体的不确定判断旁简短注明边界。'
                    '取证失败或预算耗尽不等于没有报告；未确认原因明确标为待验证，禁止把未知当零或已排除。'
                    '阶段性报告不得宣称分析完成。技术报错保留在运行记录，不堆在业务正文。'}
        if task.protocol_version >= 2:
            report_scope = {**task.state['access_scope'], 'task_id': task.state['task_id'],
                            'datasource_id': task.state['datasource_id'], 'snapshot': task.state.get('scenario') or {}}
            draft_result = self.workflow.data_qa_agent.draft_investigation(
                task.state['query'], report_context, report_scope,
                call_model=lambda system, payload: self._call(task, 'draft', system, payload))
            task.draft_report = draft_result.report.model_dump(mode='json')
            task.report_warnings = list(draft_result.warnings)
            task.draft_evidence_gaps = list(draft_result.evidence_gaps)
            task.report_diagnostics = list(getattr(draft_result, 'diagnostics', []))
            if task.report_diagnostics:
                task.logs.append({'stage': 'report_diagnostics', 'items': task.report_diagnostics})
            task.draft = draft_result.report.markdown
        else:
            draft = self._call(task, 'draft', DRAFT_SYSTEM, report_context)
            task.draft = str(draft.get('draft') or draft.get('answer') or '')
        if not task.draft.strip():
            raise ValueError('交付草稿为空')
        self._persist(task)
        config = getattr(self.workflow, 'config', None)
        if task.protocol_version >= 2 and not getattr(config, 'report_content_review_enabled', True):
            # The report was already source/permission/reference-validated by DataQa.
            # Delivery is explicit; it never certifies the prose as reviewed.
            task.approved_report = task.draft_report
            task.approved_text = task.draft
            task.delivery_kind = 'agent_report'
            task.final_reviewed = False
            unresolved = any(q.get('status') not in {'answered', 'not_applicable'}
                             for q in task.progress.get('core_questions') or [])
            task.final_status = 'partial' if stopping or task.draft_evidence_gaps or unresolved else 'completed'
            task.logs.append({'stage': 'content_review_skipped', 'reason': 'Independent content review disabled by configuration'})
            self._persist(task)
            return
        # Review only a materially new draft/evidence pair. Feedback, notes and
        # log formatting must not create permission to re-review the same text.
        actual_evidence = [(e.get('fingerprint'), e.get('query_contract'), e.get('complete')) for e in task.evidence
                           if e.get('check_status') != 'superseded']
        material_report = ({key: value for key, value in task.draft_report.items() if key != 'markdown'}
                           if task.draft_report else None)
        signature = _fingerprint({'evidence': actual_evidence, 'draft': re.sub(r'\s+', '', task.draft),
                                  'report': material_report})
        if signature in task.reviewed_signatures:
            raise BudgetStop('同稿同证据已审核，拒绝无变化再审')
        task.reviewed_signatures.append(signature)
        payload = (self._context(task, closing=True, role='review') if task.protocol_version >= 3
                   else self._context(task, closing=True))
        if task.protocol_version < 3:
            payload['draft'] = task.draft
        if task.draft_report is not None:
            payload['report'] = {**task.draft_report, 'data_sources': [
                {key: value for key, value in source.items() if key != 'rows'}
                for source in task.draft_report.get('data_sources') or []]}
            payload['draft_evidence_gaps'] = task.draft_evidence_gaps
        questions_to_assess = []
        from .business_context import current_progress
        active_explanations = current_progress(task.progress).get('explanations') or []
        if task.protocol_version >= 3:
            questions_to_assess = review_questions(task.progress, [] if task.protocol_version >= 4 else task.draft_evidence_gaps)
            payload['core_questions_to_assess'] = questions_to_assess
            payload['explanations_to_assess'] = active_explanations
        if task.protocol_version >= 4:
            # The reviewer sees immutable evidence and claims to audit, not
            # the investigator's own completion verdict as an authority.
            for key in ('progress', 'feedback', 'last_observation'):
                payload.pop(key, None)
            payload['core_questions_to_assess'] = [
                {key: q[key] for key in ('id', 'question', 'importance', 'original') if key in q}
                for q in questions_to_assess]
            payload['explanations_to_assess'] = [
                {key: value for key, value in explanation.items() if key != 'status'}
                for explanation in active_explanations]
        if task.protocol_version >= 2:
            from ..querying.report_reading import compact_reading_view, reading_view
            payload['numeric_reading_view'] = compact_reading_view(reading_view(
                self.workflow.data_qa_agent._investigation_sources(report_context.get('evidence') or [], report_scope)))
        if task.observation.get('type') == 'delivery_repair' and task.reviews:
            payload['previous_review_findings'] = {key: task.reviews[-1].get(key)
                for key in ('audit_findings', 'reason', 'repair_requests')}
        payload['audit_requirements'] = ['metric_scope', 'calculation_method', 'claim_strength', 'capability', 'answer_coverage']
        # Present authoritative inputs and observations before the candidate
        # narrative. The main agent's assessments are unverified claims.
        payload = {key: payload[key] for key in ('task', 'data_capabilities', 'semantics', 'evidence') if key in payload} | {
            key: value for key, value in payload.items() if key not in {'task', 'data_capabilities', 'semantics', 'evidence'}}
        payload['delivery_constraints'] = {
            'investigation_stopped': stopping,
            'stop_reason': task.stop_reason,
            'instruction': '技术停止不代表目标已回答；逐项说明核心缺口。已有充分答案或前提已被可靠推翻时仍可完成。',
        }
        from .review_references import context_directory, normalize_finding_references
        payload['review_sources'] = context_directory(payload)
        payload['reference_policy'] = ('evidence_ids reference actual query result IDs only; '
            'context_refs reference supplied review_sources. Context can support a scope/capability review '
            'but is not proof of numeric facts. Missing evidence can have an empty evidence_ids list.')
        review = self._call(task, 'delivery_review', REVIEW_SYSTEM, payload)
        review, reference_changes = normalize_finding_references(review, payload['review_sources'],
            {e['id'] for e in task.evidence if e.get('check_status') != 'superseded'})
        if reference_changes:
            task.logs.append({'stage': 'review_reference_normalized', 'changes': reference_changes})
        # This is a newly generated review, not a stored historical result.
        # Do not treat omitted or malformed checks as implicit confirmation.
        completion = review.get('completion_check')
        completion_errors = []
        from ..querying.analysis_output import resolve_review_text
        try:
            resolved_review_text = resolve_review_text(review, task.draft)
        except ValueError as exc:
            resolved_review_text = ''
            completion_errors.append(str(exc))
        reconciled_gaps = []
        audit_gaps = []
        audit_repair_requests = []
        audit_unfixed_errors = False
        # Historical records/test adapters can omit this new field; the live
        # provider schema requires it. When supplied, findings are binding:
        # a later optimistic status cannot erase a declared evidence gap.
        if 'audit_findings' in review:
            findings = review['audit_findings']
            expected = set(payload['audit_requirements'])
            if (not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings)
                    or len(findings) != len(expected) or {item.get('criterion') for item in findings} != expected):
                completion_errors.append('audit_findings必须逐项涵盖五类审核依据')
            else:
                evidence_ids = {e['id'] for e in task.evidence if e.get('check_status') != 'superseded'}
                for item in findings:
                    verdict = item.get('verdict')
                    if (verdict not in {'supported', 'correction_required', 'missing_evidence'}
                            or not isinstance(item.get('finding'), str) or not item['finding'].strip()
                            or type(item.get('resolved_in_final')) is not bool
                            or not isinstance(item.get('evidence_ids'), list)
                            or not all(isinstance(ref, str) for ref in item['evidence_ids'])
                            or not set(item['evidence_ids']) <= evidence_ids
                            or not isinstance(item.get('context_refs', []), list)
                            or not all(isinstance(ref, str) and ref in payload['review_sources']
                                       for ref in item.get('context_refs', []))):
                        completion_errors.append('审核依据结构或证据引用无效')
                        continue
                    method_gap = (item['criterion'] == 'calculation_method'
                                  and verdict == 'correction_required')
                    matching_edits = any(edit.get('criterion') == item['criterion']
                                         for edit in review.get('text_edits') or []
                                         if isinstance(edit, dict))
                    unbound_correction = ('text_edits' in review and verdict == 'correction_required'
                                          and not matching_edits)
                    if verdict == 'missing_evidence' or method_gap or (verdict == 'correction_required' and (
                            not item['resolved_in_final'] or review.get('approve_draft_unchanged') is True
                            or unbound_correction)):
                        audit_gaps.append(item['criterion'] + '：' + item['finding'])
                        if verdict == 'correction_required':
                            audit_unfixed_errors = True
                        if (method_gap and review.get('repair_scope') != 'report') or (verdict == 'missing_evidence' and (
                                review.get('action') == 'complete' or review.get('can_repair')
                                or review.get('repair_requests'))):
                            audit_repair_requests.append({
                                'criterion': item['criterion'], 'finding': item['finding'],
                                'evidence_ids': item['evidence_ids'],
                                'instruction': '先复用并核对已有结果；仅对必要证据缺口补查或重算。'
                                    '若确实无法补足，明确覆盖、权限或预算依据，不能靠改写断言宣告修复。'})
        if task.protocol_version >= 3:
            reconciliation = reconcile_review(review, questions_to_assess, active_explanations,
                {e['id'] for e in task.evidence if e.get('check_status') != 'superseded'})
            completion_errors.extend(reconciliation['errors'])
            reconciled_gaps = reconciliation['gaps']
        if not isinstance(completion, dict):
            completion_errors.append('completion_check必须为对象')
        else:
            goal_status = completion.get('goal_status')
            if not isinstance(goal_status, str) or goal_status not in {'answered', 'partial', 'unanswered', 'not_applicable'}:
                completion_errors.append('goal_status缺失或无效')
            for key in ('metric_scope_matches', 'capability_claims_supported'):
                if type(completion.get(key)) is not bool:
                    completion_errors.append(key + '必须为布尔值')
            core_gaps = completion.get('remaining_core_gaps')
            if not isinstance(core_gaps, list) or any(not isinstance(item, str) or not item.strip()
                                                      for item in core_gaps):
                completion_errors.append('remaining_core_gaps必须为非空文字项数组或空数组')
            basis = completion.get('completion_basis')
            if not isinstance(basis, str) or not basis.strip():
                completion_errors.append('completion_basis必须为非空文字')
        if completion_errors:
            detail = '最终审核检查结构不完整，未展示该稿：' + '；'.join(completion_errors)
            task.reviews.append({'final': True, **review, 'controller_action': 'reject',
                                 'controller_reason': detail})
            task.final_status = ('partial' if any(e.get('check_status') != 'superseded'
                                                 for e in task.evidence) else 'failed')
            task.final_reviewed = False
            task.approved_text = ''
            task.stop_reason = (task.stop_reason + '；' if task.stop_reason else '') + detail
            return
        if task.protocol_version >= 3:
            by_question = {entry['question_id']: entry for entry in review.get('question_assessments') or []}
            for question in task.progress.get('core_questions') or []:
                assessment = by_question.get(question['id'])
                if assessment:
                    question.update(status='blocked' if assessment['status'] == 'unresolved' else assessment['status'],
                                    evidence_ids=list(assessment.get('evidence_ids') or []),
                                    reason=assessment['reason'], answer_level=assessment['answer_level'])
        valid_ids = {e['id'] for e in task.evidence if e.get('check_status') != 'superseded'}
        for item in review.get('answered') or []:
            if not set(item.get('evidence_ids') or []) <= valid_ids:
                raise ValueError('最终审核引用了未知或失效结果')
        affected = review.get('affected_evidence_ids') or []
        if not set(affected) <= valid_ids:
            raise ValueError('最终审核修复引用未知结果')
        action = review.get('action')
        repair_requests = list(review.get('repair_requests') or []) + audit_repair_requests
        can_repair = bool(review.get('can_repair') or repair_requests)
        # The review's status label cannot overrule its own structured findings.
        # These checks enforce declared conflicts, not semantic keyword guesses
        # about arbitrary SQL or a business explanation.
        gaps = list(review.get('unanswered') or []) + list(completion.get('remaining_core_gaps') or []) + reconciled_gaps + audit_gaps
        incomplete_goal = completion.get('goal_status') in {'partial', 'unanswered'}
        invalid_content = (completion.get('metric_scope_matches') is False
                           or completion.get('capability_claims_supported') is False or audit_unfixed_errors)
        if action == 'complete' and (gaps or incomplete_goal or can_repair or invalid_content):
            action = 'repair' if can_repair else 'partial'
            review['controller_action'] = action
            review['controller_reason'] = '审核完成标签与其核心缺口、修复要求或口径检查矛盾'
        if repair_requests:
            review['controller_repair_requests'] = repair_requests
        task.reviews.append({'final': True, **review})
        if action == 'clarify' and isinstance(review.get('clarification'), dict) and not stopping:
            task.clarification = Clarification.model_validate(review['clarification']).model_dump()
            task.final_status = 'waiting_clarification'
            return
        publishable = bool(resolved_review_text) and not invalid_content
        if publishable and action == 'repair':
            action = 'partial'
            task.reviews[-1].update(controller_action='partial_delivery',
                controller_reason='正文已获认可；保留未决项并交付阶段报告，进一步取证不阻止本次交付')
        if task.protocol_version >= 4 and action in {'repair', 'partial'} and can_repair and review.get('repair_scope') == 'report' and not affected and not publishable:
            # Report-only corrections never go back through investigation or invalidate sound data.
            task.reviews[-1]['controller_action'] = 'report_revision'
            task.feedback = _json({'reason': review.get('reason'), 'repair_requests': repair_requests})
            task.observation = {'type': 'delivery_repair', 'requests': repair_requests}
            task.final_status, task.final_reviewed = 'partial', False
            return
        remaining = (budget is None or (budget.elapsed() < min(budget.seconds, budget.investigation_seconds)
                                       and budget.tools < budget.tool_limit
                                       and budget.react_rounds < budget.react_round_limit))
        if action in {'repair', 'partial'} and can_repair and remaining and not stopping and not publishable:
            self._invalidate(task, affected)
            task.reviews[-1]['controller_action'] = 'repair'
            task.feedback = str(review.get('reason') or '') + '\n必要补查／修复：' + _json(repair_requests)
            task.observation = {'type': 'delivery_repair', 'requests': repair_requests,
                                'affected_evidence_ids': affected}
            task.final_reviewed = False
            return
        approved = resolved_review_text
        if action in {'complete', 'partial', 'repair'} and approved and not invalid_content:
            if task.protocol_version >= 2:
                reviewed_report = {**task.draft_report, 'markdown': approved}
                if reviewed_report.get('visualizations') and review.get('visualizations_approved') is not True:
                    reviewed_report['visualizations'] = []
                    reviewed_report['warnings'] = list(reviewed_report.get('warnings') or []) + ['最终审核未明确认可图表，定稿仅展示正文。']
                checked_report = self.workflow.data_qa_agent.validate_investigation_report(
                    reviewed_report, report_context.get('evidence') or [], report_scope)
                task.approved_report = checked_report.model_dump(mode='json')
            task.approved_text = approved
            task.final_status = 'completed' if action == 'complete' else 'partial'
            task.final_reviewed = True
            task.delivery_kind = 'reviewed_report'
            if task.final_status == 'partial':
                gap_reason = str(review.get('reason') or '核心问题尚有未回答部分')
                if incomplete_goal:
                    gap_reason += '；审核确认原目标尚未完全回答'
                if gaps:
                    gap_reason += '；未完成：' + _json(gaps)
                task.stop_reason = (task.stop_reason + '；' if task.stop_reason else '') + gap_reason
                # The Agent already writes business-facing open questions at the end.
                # Keep budget/audit details in structured records, not a second template footer.
            for entry in task.evidence:
                if entry.get('check_status') != 'superseded':
                    entry['check_status'] = 'reviewed'
        else:
            task.final_status = 'partial' if task.evidence else 'failed'
            detail = ('最终审核仍有口径或能力断言错误，未展示该稿' if invalid_content
                      else str(review.get('reason') or '最终审核未完成'))
            task.stop_reason = (task.stop_reason + '；' if task.stop_reason else '') + detail

    def _result(self, task: ReactRecord) -> QueryResult:
        active = [e for e in task.evidence if e.get('check_status') != 'superseded']
        latest = active[-1] if active else {}
        if task.final_status == 'waiting_clarification':
            message, analysis = '需要补充信息', None
        elif task.final_reviewed or task.delivery_kind in {'evidence_summary', 'agent_report'}:
            message = '调查完成' if task.final_status == 'completed' else (
                '经营分析报告已生成' if active else '分析说明已生成')
            analysis = task.approved_text
        else:
            message = '报告暂未生成，分析进展已保存'
            analysis = '报告生成遇到问题，已保留本次分析进展和查询结果。'
        return QueryResult(task_id=task.state['task_id'], route='database_query',
                           status=task.final_status or 'failed', message=message, analysis=analysis,
                           report=task.approved_report if task.final_reviewed or task.delivery_kind in {'evidence_summary', 'agent_report'} else None,
                           content_review_status='skipped' if task.delivery_kind == 'agent_report' else None,
                           clarification=task.clarification, workflow_mode='investigation_react_v1',
                           execution_protocol_version=task.protocol_version,
                           datasource_id=task.state['datasource_id'], scenario=task.state.get('scenario'),
                           standalone_query=task.state.get('standalone_query'), plan=task.actions,
                           evidence=active, execution_log=task.logs,
                           selected_skills=list(task.skills), selected_semantics=list(task.metric_pool.values()),
                           required_questions=task.state.get('required_questions') or [task.state['query']],
                           requirements=[{'id': q['id'], 'question': q['question'], 'status': q['status']}
                                         for q in task.progress.get('core_questions') or []],
                           reviews=task.reviews, final_reviewed=task.final_reviewed,
                           retrieval_history=task.retrievals, stop_reason=task.stop_reason or None,
                           sql=latest.get('sql'), columns=latest.get('columns') or [], rows=latest.get('rows') or [],
                           result_ref=latest.get('result_ref'), total_rows=latest.get('total_rows'),
                           complete=bool(latest.get('complete', True)), preview_truncated=bool(latest.get('display_truncated')),
                           budget=current_budget.get().public() if current_budget.get() else {})
