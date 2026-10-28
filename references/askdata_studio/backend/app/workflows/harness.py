"""Structured planner/executor/reviewer nodes with a deterministic controller.

Only public retrieval and explicitly selected application skills enter prompts.
The record survives a clarification within this server process; it is not a chat summary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from langgraph.graph import StateGraph, START, END

from .. import homenova
from ..errors import PipelineStageError
from ..models import QueryResult, Clarification
from ..runtime_budget import BudgetStop, current_budget
from ..task_decisions import StepCondition, StepDecision, STEP_PROTOCOL_INSTRUCTION, validate_step_decision
from ..result_references import model_result_context, resolve_result_ids


class StepSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^S[1-9][0-9]*$')
    goal: str = Field(min_length=1)
    kind: Literal['query', 'analysis'] = 'query'
    depends_on: list[str] = Field(default_factory=list)
    input_fields: dict[str, list[str]] = Field(default_factory=dict)
    expected_output: str = Field(min_length=1)
    metric_ids: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    entry_conditions: list[StepCondition] = Field(default_factory=list)
    review_after: bool = False
    review_reason: str = ''
    reuse_result_id: str | None = None


class Plan(BaseModel):
    conditions: dict[str, Any]
    steps: list[StepSpec] = Field(min_length=1, max_length=8)
    clarification: dict[str, Any] | None = None


class AnsweredItem(BaseModel):
    question: str
    evidence_ids: list[str] = Field(default_factory=list)
    strength: Literal['fact', 'contribution', 'association', 'cause', 'not_applicable'] = 'fact'
    explanation: str


class Review(BaseModel):
    premise_status: Literal['unknown', 'confirmed', 'refuted'] = 'unknown'
    action: Literal['continue', 'repair', 'replan', 'clarify', 'complete', 'partial', 'fail']
    answered: list[AnsweredItem] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)
    issue_type: Literal['none', 'execution', 'semantics', 'planning', 'ambiguity', 'unsupported', 'evidence_gap'] = 'none'
    affected_steps: list[str] = Field(default_factory=list)
    reason: str
    additional_skills: list[str] = Field(default_factory=list)
    clarification: dict[str, Any] | None = None
    approved_text: str = ''


@dataclass
class TaskRecord:
    state: dict[str, Any]
    required: list[str]
    conditions: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    retrievals: list[dict[str, Any]] = field(default_factory=list)
    clarification: dict[str, Any] | None = None
    feedback: str = ''
    draft: str = ''
    approved_text: str = ''
    final_status: str = ''
    stop_reason: str = ''
    next_node: str = 'planner'
    pending_review: dict[str, Any] | None = None
    final_reviewed: bool = False
    pending_decision: dict[str, Any] | None = None
    step_decisions: list[dict[str, Any]] = field(default_factory=list)
    not_applicable_requirements: set[str] = field(default_factory=set)
    delivery_signatures: set[str] = field(default_factory=set)
    draft_signatures: set[str] = field(default_factory=set)
    non_retryable_steps: set[str] = field(default_factory=set)


class InvestigationHarness:
    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow
        self.model = workflow.model_client
        self.records: dict[str, TaskRecord] = {}
        graph = StateGraph(dict)
        for name in ['planner', 'execute', 'controller', 'draft', 'delivery_review']:
            graph.add_node(name, getattr(self, '_' + name))
        graph.add_conditional_edges(START, lambda s: s['record'].next_node)
        for name in ['planner', 'execute', 'controller', 'draft', 'delivery_review']:
            graph.add_conditional_edges(name, lambda s: s['record'].next_node,
                {node: node for node in ['planner', 'execute', 'controller', 'draft', 'delivery_review']} | {'end': END})
        self.graph = graph.compile()

    def run(self, state: dict[str, Any], response: str | None = None) -> QueryResult:
        task_id = state['task_id']
        task = self.records.get(task_id)
        if task is None:
            task = TaskRecord(dict(state), list(state.get('required_questions') or [state['query']]),
                skills=list(state.get('business_skills') or []))
            self.records[task_id] = task
        if response is not None:
            if not task.clarification:
                raise ValueError('任务不在等待补充状态')
            parameter = task.clarification.get('parameter', 'business_condition')
            options = task.clarification.get('options') or []
            label = next((item['label'] for item in options if item['id'] == response), response)
            previous = task.conditions.get(parameter)
            if previous is not None and previous != label:
                for completed in list(task.steps):
                    self.invalidate(task, completed['id'])
            task.conditions[parameter] = label
            task.state.setdefault('workspace', {}).setdefault('confirmed_parameters', {})[parameter] = label
            task.feedback = f'用户补充（确认条件）：{parameter}={label}'
            task.clarification = None
            task.final_status = ''
            task.final_reviewed = False
            # No entry classification, completed steps and remaining budget survive.
            task.next_node = 'planner' if not task.steps else 'execute'
        try:
            if not task.retrievals:
                self._retrieve(task, task.state.get('standalone_query') or state['query'])
            self.graph.invoke({'record': task}, {'recursion_limit': 100})
        except BudgetStop as exc:
            task.stop_reason = str(exc)
            self._close_after_stop(task)
        except (PipelineStageError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            task.stop_reason = f'{getattr(exc, "stage", "harness")}: {exc}'
            task.logs.append({'stage': 'harness_error', 'success': False, 'error': task.stop_reason})
            self._close_after_stop(task)
        return self._result(task)

    def _selected_skills(self, task: TaskRecord) -> dict[str, str]:
        return {name: self.workflow.skills.get(name).instructions for name in task.skills}

    @staticmethod
    def _requirements(task: TaskRecord) -> dict[str, str]:
        return {f'Q{index}': question for index, question in enumerate(task.required, 1)}

    @staticmethod
    def _reference_context(task: TaskRecord, evidence: list[dict]) -> dict:
        return {'dependency_results': evidence, 'task_id': task.state['task_id'],
                'user_id': task.state['access_scope']['user_id'], 'datasource_id': task.state.get('datasource_id'),
                'snapshot': task.state.get('scenario') or {}}

    def _materials(self, task: TaskRecord) -> dict[str, Any]:
        state = task.state
        return {'task_question': state['query'], 'complete_question': state.get('standalone_query') or state['query'],
            'must_answer': task.required, 'conditions': task.conditions, 'datasource_id': state.get('datasource_id'),
            'scenario': state.get('scenario') or {}, 'selected_metrics': state.get('selected_semantics') or [],
            'schema_graph': state.get('schema_graph') or {},
            'capabilities': homenova.public_capabilities(state['datasource_id']) if homenova.is_homenova(state.get('datasource_id')) else {},
            'scenario_notes': state.get('scenario_text', ''),
            'business_skills': task.skills, 'plan': task.steps,
            'evidence': [item for item in task.evidence if item['check_status'] != 'superseded'],
            'previous_review': task.reviews[-1] if task.reviews else None, 'feedback': task.feedback,
            'requirements': self._requirements(task), 'not_applicable_requirement_ids': sorted(task.not_applicable_requirements),
            'step_decisions': task.step_decisions,
            'reusable_evidence_metadata': state.get('reusable_evidence') or []}

    def _call(self, node: str, system: str, payload: dict[str, Any], task: TaskRecord) -> dict[str, Any]:
        started = time.monotonic()
        budget = current_budget.get()
        if budget:
            budget.model_node = node
        methods = self._selected_skills(task)
        system += '\n\n本任务明确选中的业务Skill规则，必须遵守：\n' + '\n\n'.join(methods.values())
        if budget:
            for name, instructions in methods.items():
                budget.skill(name, instructions, node)
        try:
            result = self.model.chat_json(system, json.dumps(model_result_context(payload), ensure_ascii=False, default=str))
            task.logs.append({'stage': node, 'success': True, 'elapsed_seconds': round(time.monotonic()-started, 3)})
            return result
        except Exception as exc:
            task.logs.append({'stage': node, 'success': False, 'error': str(exc), 'elapsed_seconds': round(time.monotonic()-started, 3)})
            raise

    def _retrieve(self, task: TaskRecord, query: str, fields: list[str] | None = None) -> None:
        budget = current_budget.get()
        if budget:
            budget.tool('schema_retrieval')
        workspace = dict(task.state.get('workspace') or {})
        source = str(task.state.get('datasource_id') or '')
        if fields and homenova.is_homenova(source):
            tables = {table['name']: table for table in homenova.schema_payload(source)['tables']}
            workspace['schema_fields'] = [{'tableId': tables[ref.split('.', 1)[0]]['id'], 'name': ref.split('.', 1)[1]}
                for ref in sorted(set(fields)) if '.' in ref and ref.split('.', 1)[0] in tables]
        state = {**task.state, 'workspace': workspace, 'standalone_query': query,
                 'extraction': {'retrieval_terms': [query]}, 'result': {}}
        update = self.workflow._retrieve_schema(state, charge_budget=False)
        if update.get('result'):
            raise ValueError('当前授权数据源没有检索到可用语义')
        task.state.update(update)
        retrieval = update.get('retrieval') or {}
        task.retrievals.append({'query': query, **{key: retrieval.get(key) for key in
            ['embedding_source', 'rerank_source', 'bm25_count', 'dense_count', 'rrf_count', 'selected_count', 'selected_semantics']}})
        source = str(task.state.get('datasource_id') or '')
        if homenova.is_homenova(source):
            task.state['scenario_text'] = homenova.scenario_text(source)

    def _planner(self, state: dict) -> dict:
        task = state['record']
        budget = current_budget.get()
        if budget:
            budget.step = 'planner'
        payload = self._materials(task)
        schema = Plan.model_json_schema()
        system = '''你是经营调查Planner。只生成结构化计划，不提前查询或编造数字。
对照must_answer覆盖所有要求。根据公开能力与选中Skill提出少量有区分力的步骤，先验证比较前提。
conditions必须明确指标口径、范围、两期半开边界（相对时间使用场景日期），可默认最近等长窗口但披露假设。
调查时间只有4分钟、还需预留收尾。初始计划优先3至4个有区分力的步骤，不穷举所有方向。
单步goal须包含明确时间和范围，query步骤一次SQL；analysis步骤仅处理依赖结果。
用户未要求同比时，优先选择公开能力支持的相邻可比期，不擅自加入未确认覆盖的去年同期。没有交易覆盖不能当作零。
结果优先每期一行、指标为列。input_fields可为空数组传完整结构化聚合，不提前猜测多期透视列名迫使复杂SQL。
必要时在一个总体查询同时取得GMV及同集合的订单数/客单价，避免反复查询同一集合。
用户提及销量/件数时应检查成功支付商品件数，不能只查订单数代替；订单数不等于件数，客单价不等于商品成交单价。
不能把订单量减少直接等同流量/转化下降；需相应数据检验。不能计划计算精确失单/因果损失。
尚未检索的新方向可用语义描述，required_fields和metric_ids可空，不能猜测不存在的字段。
depends_on只能引用前序S编号；input_fields用依赖S编号声明必需列（未知列名可空数组，表示传完整结构化聚合）。
每步声明expected_output、metric_ids（仅已知注册ID）、required_fields（仅真实表.列）。需要新语义时执行器会补检索。
执行阶段没有独立过程Reviewer，所有review_after=false。每步requirement_ids引用requirements编号，覆盖全部必要要求。
后续Executor会在原有查询/分析调用中读取真实依赖结果，先判断本步适用性，适用时同次生成SQL/分析，不另加判断调用。
需判断前提的后续步骤声明entry_conditions：id、description、affected_requirement_ids、affected_step_ids。
条件只影响当前及后续相关分支，不能跳过独立目标或已完成的前提核实。GMV等数值比较条件comparison_required=true。
例如若完整可比结果未出现所述下降，相关下降归因不适用，保留两期事实并纠正前提；还有独立目标则继续。不改题找增长原因或其他口径下降。
缺比较期、结果不完整、口径不一致时尚不能判断，不是前提被推翻。尚需检验的解释要在计划中提出实际取证步骤，品类/渠道贡献不等同原因检验。
缺关键参数先检查上下文、确认条件和公开配置，确实缺才clarification，包含parameter/question/reason/options（可空数组让用户自由填写）。
保留已完成且有效的独立步骤及其ID，修正仅涉及受影响步骤；不要用有限结果缩小目标。重规划必须原样复制仍适用的conditions，不换键名或重新表述已有条件；只增加缺失条件。
已有证据满足完全相同goal、conditions、快照与完整性时可设置reuse_result_id，需保留原goal与conditions，否则重新查询。
只返回符合此JSON Schema的对象：'''+json.dumps(schema, ensure_ascii=False)
        raw = self._call('planner', system, payload, task)
        plan = Plan.model_validate(raw)
        requirements = set(self._requirements(task))
        plan_ids = [item.id for item in plan.steps]
        condition_ids: set[str] = set()
        for position, spec in enumerate(plan.steps):
            spec.review_after = False  # legacy fields are display compatibility, never dispatch a reviewer
            spec.review_reason = ''
            spec.requirement_ids = spec.requirement_ids or list(self._requirements(task))
            if not set(spec.requirement_ids) <= requirements:
                raise ValueError('计划引用未知任务要求')
            for condition in spec.entry_conditions:
                if condition.id in condition_ids or not set(condition.affected_requirement_ids) <= requirements:
                    raise ValueError('计划条件编号重复或任务范围不合法')
                if not set(condition.affected_step_ids) <= set(plan_ids[position:]):
                    raise ValueError('计划条件不能跳过上游或未知步骤')
                condition_ids.add(condition.id)
        source = str(task.state.get('datasource_id') or '')
        if homenova.is_homenova(source):
            tables = homenova.schema_payload(source)['tables']
            mapping = {table['id']: table['name'] for table in tables}
            known = {table['name']+'.'+field['name'] for table in tables for field in table['fields']}
            for spec in plan.steps:
                normalized = [mapping.get(ref.split('.', 1)[0], ref.split('.', 1)[0])+'.'+ref.split('.', 1)[1]
                              if '.' in ref else ref for ref in spec.required_fields]
                unknown = set(normalized)-known
                if unknown:
                    # Unsupported hints are recorded, never used as physical schema.
                    task.logs.append({'stage': 'planner_field_check', 'step_id': spec.id,
                        'success': False, 'unknown_fields': sorted(unknown)})
                spec.required_fields = [ref for ref in normalized if ref in known]
        seen: set[str] = set()
        for step in plan.steps:
            if step.id in seen or not set(step.depends_on) <= seen or not set(step.input_fields) <= set(step.depends_on):
                raise ValueError('计划步骤编号重复、前向依赖或未声明的输入依赖')
            seen.add(step.id)
        old = {item['id']: item for item in task.steps}
        new_conditions = {**plan.conditions, **task.state.get('workspace', {}).get('confirmed_parameters', {})}
        changed = bool(task.conditions) and task.conditions != new_conditions
        if changed:
            task.not_applicable_requirements.clear()
        task.conditions = new_conditions
        steps = []
        for spec in plan.steps:
            data = spec.model_dump()
            prior = old.get(spec.id)
            if prior and all(prior.get(key) == data[key] for key in data) and not changed:
                steps.append(prior)
            else:
                if prior:
                    self.invalidate(task, prior['id'])
                steps.append({**data, 'status': 'pending', 'result_ids': [], 'input_result_ids': [], 'error': None})
        removed = set(old) - seen
        for step_id in removed:
            self.invalidate(task, step_id)
        task.steps = steps
        if plan.clarification:
            self._wait(task, plan.clarification)
        else:
            task.next_node = 'execute'
        return state

    @staticmethod
    def invalidate(task: TaskRecord, step_id: str) -> None:
        affected = {step_id}
        for step in task.steps:
            if set(step.get('depends_on', [])) & affected:
                affected.add(step['id'])
        ids = {rid for step in task.steps if step['id'] in affected for rid in step.get('result_ids', [])}
        for result in task.evidence:
            if result['id'] in ids:
                result['check_status'] = 'superseded'
        for step in task.steps:
            if step['id'] in affected:
                task.non_retryable_steps.discard(step['id'])
                if step['status'] == 'not_applicable':
                    task.not_applicable_requirements.difference_update(step.get('requirement_ids', []))
                step.update(status='pending', result_ids=[], input_result_ids=[], error=None)

    def _dependencies(self, task: TaskRecord, step: dict) -> list[dict]:
        ids = {rid for item in task.steps if item['id'] in step['depends_on'] for rid in item['result_ids']}
        result = []
        for item in task.evidence:
            if item['id'] not in ids:
                continue
            if item['check_status'] not in {'basic_valid', 'reviewed'} or not item.get('complete'):
                raise ValueError('上游结果未通过基础检查、已失效或不完整')
            resolve_result_ids([item['id']], self._reference_context(task, [item]))
            wanted = step.get('input_fields', {}).get(item['step_id'], [])
            if not set(wanted) <= set(item.get('columns') or []):
                raise ValueError('上游结果缺少计划声明的输入字段')
            copied = dict(item)
            copied['rows'] = [{key: row.get(key) for key in wanted} for row in item['rows']] if wanted else item['rows']
            result.append(copied)
        return result

    def _execute(self, state: dict) -> dict:
        task = state['record']
        by_id = {step['id']: step for step in task.steps}
        step = next((step for step in task.steps if step['status'] == 'pending'
                     and all(by_id[dep]['status'] == 'completed' for dep in step['depends_on'])), None)
        if step is None:
            requires_new_path = False
            for item in task.steps:
                if item['status'] == 'pending':
                    if (any(by_id[dep]['status'] == 'not_applicable' for dep in item['depends_on'])
                            and set(item.get('requirement_ids', [])) <= task.not_applicable_requirements):
                        item['status'] = 'not_applicable'
                    else:
                        item['status'] = 'blocked'
                        requires_new_path |= any(by_id[dep]['status'] == 'not_applicable' for dep in item['depends_on'])
            if requires_new_path:
                budget = current_budget.get()
                if budget:
                    budget.replan()
                task.feedback = '上游分支不适用，但被阻塞步骤仍有独立必要目标；保留目标和有效结果，补充可执行依赖路径，不能结束整项任务。'
                task.next_node = 'planner'
                return state
            task.next_node = 'draft'
            return state
        budget = current_budget.get()
        if budget:
            budget.step = step['id']
            budget.check()
        started = time.monotonic()
        try:
            dependencies = self._dependencies(task, step)
            step['input_result_ids'] = [item['id'] for item in dependencies]
            required_output = list(dict.fromkeys(field for consumer in task.steps
                for field in consumer.get('input_fields', {}).get(step['id'], [])))
            context = {'step_id': step['id'], 'task_id': task.state['task_id'],
                       'user_id': task.state['access_scope']['user_id'], 'snapshot': task.state.get('scenario') or {},
                       'required_output_fields': required_output, 'task_question': task.state['query'], 'conditions': task.conditions,
                       'dependency_results': dependencies, 'expected_output': step['expected_output'],
                       'feedback': task.feedback, 'step_protocol': True,
                       'datasource_id': task.state.get('datasource_id'), 'requirements': self._requirements(task),
                       'step_conditions': step.get('entry_conditions', []),
                       'not_applicable_requirement_ids': sorted(task.not_applicable_requirements),
                       'plan_steps': [{key: item.get(key) for key in ['id', 'goal', 'status', 'requirement_ids']} for item in task.steps]}
            fingerprint = hashlib.sha256(json.dumps({'goal': step['goal'], 'conditions': task.conditions,
                'inputs': step['input_result_ids'], 'datasource': task.state.get('datasource_id')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            reused = next((item for item in task.evidence if item.get('fingerprint') == fingerprint
                           and item['check_status'] in {'basic_valid', 'reviewed'} and item.get('complete')
                           and set(required_output) <= set(item.get('columns', []))), None)
            if not reused and step.get('reuse_result_id'):
                source = next((item for item in task.state.get('reusable_evidence', [])
                    if item['id'] == step['reuse_result_id'] and item.get('goal') == step['goal']
                    and item.get('conditions') == task.conditions and item.get('datasource_id') == task.state.get('datasource_id')
                    and item.get('user_id') == task.state['access_scope']['user_id']
                    and item.get('check_status') == 'reviewed' and item.get('complete')
                    and not item.get('display_truncated') and set(required_output) <= set(item.get('columns', []))), None)
                if source:
                    resolve_result_ids([source['id']], {**self._reference_context(task, [source]), 'task_id': source['task_id']})
                    copied = {**source, 'source_result_id': source['id'], 'source_task_id': source['task_id']}
                    if source.get('result_ref'):
                        copied['result_ref'] = self.workflow.database_engine.clone_result_reference(
                            source['result_ref'], source['user_id'], source['task_id'], task.state['task_id'], source['datasource_id'])
                    self._save_evidence(task, step, copied, fingerprint)
                    reused = task.evidence[-1]
            if reused:
                step.update(status='completed', result_ids=[reused['id']])
                task.logs.append({'stage': 'reuse', 'step_id': step['id'], 'result_id': reused['id']})
            elif step['kind'] == 'query':
                current_metrics = {item['id'] for item in task.state.get('selected_semantics') or []}
                source = str(task.state.get('datasource_id') or '')
                needed_metrics = set(step['metric_ids'])
                if homenova.is_homenova(source):
                    for metric in homenova.metrics(source):
                        names = [metric['id'], metric['label'], *metric.get('aliases', [])]
                        if any(name and name.casefold() in (step['goal'] + ' ' + task.feedback).casefold() for name in names):
                            needed_metrics.add(metric['id'])
                fields = {item.get('sql_name') for item in (task.state.get('schema_graph') or {}).get('fields', [])}
                if not needed_metrics <= current_metrics or not set(step['required_fields']) <= fields or (not step['metric_ids'] and task.evidence):
                    dependency_fields = [ref for item in dependencies
                        for ref in (item.get('query_contract') or {}).get('dimensions', [])]
                    needed_fields = list(dict.fromkeys([*step['required_fields'], *dependency_fields]))
                    self._retrieve(task, step['goal'] + '；修正需求：' + task.feedback + '；所需语义：' + '、'.join([*sorted(needed_metrics), *needed_fields]), needed_fields)
                db = (task.state.get('database_names') or [task.state.get('datasource_id')])[0]
                decision = self.workflow.single_database_agent.prepare(step['goal'], db,
                    task.state['schema_graph'], task.state['schema_context'], task.state['retrieval'],
                    {**task.state.get('workspace', {}), 'confirmed_parameters': task.conditions},
                    task.state['access_scope'], original_query=task.state['query'], task_context=context)
                task.logs.append({'stage': 'step_execution', 'step_id': step['id'],
                    'validation_attempts': decision.get('validation_attempts', []),
                    'tool_trace': decision.get('tool_trace', []), 'elapsed_seconds': round(time.monotonic()-started, 3)})
                if decision.get('step_decision'):
                    front = validate_step_decision(decision['step_decision'], context)
                    self._record_decision(task, step, front)
                    if front.action != 'execute_step':
                        task.pending_decision = {'step_id': step['id'], **front.model_dump()}
                        task.next_node = 'controller'
                        return state
                if decision['action'] == 'clarify':
                    self._wait(task, decision['clarification'])
                    return state
                execution = decision['execution']
                if not execution.get('success'):
                    raise ValueError(execution.get('error') or '查询失败')
                rows = execution.get('rows') or []
                complete = bool(execution.get('complete', True))
                if execution.get('preview_truncated'):
                    ref = execution.get('result_ref')
                    source = self.workflow.database_engine.results.get(ref) or {}
                    if (source.get('user_id'), source.get('task_id'), source.get('datasource_id')) != (
                        task.state['access_scope']['user_id'], task.state['task_id'], db):
                        raise PermissionError('结果引用作用域不匹配')
                    rows = source.get('rows') or []
                trace = decision.get('tool_trace') or []
                contract = next((item.get('query_contract') for item in reversed(trace) if item.get('query_contract')), {})
                result = {'sql': execution.get('sql'), 'columns': execution.get('columns') or [], 'rows': rows,
                    'complete': complete, 'total_rows': execution.get('total_rows', len(rows)),
                    'preview_truncated': execution.get('preview_truncated', False), 'result_ref': execution.get('result_ref'),
                    'query_contract': contract}
                self._save_evidence(task, step, result, fingerprint)
                if not set(required_output) <= set(result['columns']):
                    self.invalidate(task, step['id'])
                    raise ValueError(f"输出列契约不匹配；下游需要{required_output}，实际列为{result['columns']}；请使用精确输出别名")
                if not rows or not complete:
                    raise ValueError('空结果需核实筛选和覆盖，不可当零；不完整结果需要完整聚合')
            else:
                payload = {'step': step, **context, 'business_skills': task.skills}
                output = self._call('step_analysis', '''仅处理本步真实依赖结果，不审核整个任务。
同次调用返回step_decision，适用时action=execute_step并返回columns/rows/findings/evidence_ids；不适用时只返回前置建议。
计算须可复核、区分贡献/关联/原因，不调用入口、不生成最终用户回答。
''' + STEP_PROTOCOL_INSTRUCTION + json.dumps(StepDecision.model_json_schema(), ensure_ascii=False), payload, task)
                changes = []
                front = validate_step_decision(output.get('step_decision'), context, normalizations=changes)
                self._record_decision(task, step, front)
                if front.action != 'execute_step':
                    task.pending_decision = {'step_id': step['id'], **front.model_dump()}
                    task.next_node = 'controller'
                    return state
                output['evidence_ids'] = resolve_result_ids(output.get('evidence_ids') or [], context, changes)
                if changes:
                    task.logs.append({'stage': 'result_reference_normalization', 'step_id': step['id'], 'changes': changes})
                if not isinstance(output.get('rows'), list) or not isinstance(output.get('columns'), list):
                    raise ValueError('分析结果缺少结构化数据')
                self._save_evidence(task, step, {**output, 'complete': True, 'total_rows': len(output['rows'])}, fingerprint)
            task.logs.append({'stage': 'step_validation', 'step_id': step['id'], 'success': True,
                              'checks': ['dependency_scope', 'required_fields', 'result_completeness', 'query_contract_or_analysis_refs']})
            task.next_node = 'execute'
        except BudgetStop as exc:
            if '修复额度' in str(exc):
                task.logs.append({'stage': 'step_repair_exhausted', 'step_id': step['id'], 'success': False,
                    'error': str(exc), **getattr(exc, 'context', {})})
                step.update(status='failed', error=str(exc))
                task.feedback = str(exc)
                task.next_node = 'execute'  # independent steps still run
            else:
                raise
        except (PipelineStageError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            task.logs.append({'stage': 'step_error', 'step_id': step['id'], 'error': str(exc),
                              'context': getattr(exc, 'context', {}), 'elapsed_seconds': round(time.monotonic()-started, 3)})
            task.feedback = f'步骤{step["id"]}错误：{exc}；保持目标，定向修复。'
            if getattr(exc, 'context', {}).get('no_progress'):
                task.non_retryable_steps.add(step['id'])
                step.update(status='failed', error=str(exc))
                task.next_node = 'execute'
                return state
            try:
                if budget:
                    budget.repair('步骤局部修复')
                step['error'] = str(exc)
                task.next_node = 'execute'
            except BudgetStop:
                step.update(status='failed', error=str(exc))
                task.next_node = 'execute'
        return state

    @staticmethod
    def _record_decision(task: TaskRecord, step: dict, decision: StepDecision) -> None:
        record = {'step_id': step['id'], **decision.model_dump()}
        task.step_decisions.append(record)
        task.logs.append({'stage': 'executor_decision', **record})

    def _save_evidence(self, task: TaskRecord, step: dict, data: dict, fingerprint: str) -> None:
        rid = f'{task.state["task_id"]}:R{len(task.evidence)+1}'
        task.evidence.append({**data, 'id': rid, 'step_id': step['id'], 'goal': step['goal'],
            'task_id': task.state['task_id'], 'datasource_id': task.state.get('datasource_id'),
            'user_id': task.state['access_scope']['user_id'],
            'snapshot': task.state.get('scenario'), 'conditions': dict(task.conditions),
            'input_result_ids': list(step['input_result_ids']), 'fingerprint': fingerprint,
            'check_status': 'basic_valid' if data.get('complete') and data.get('rows') else 'needs_review'})
        step.update(status='completed', result_ids=[rid], error=None)

    def _review_payload(self, task: TaskRecord, final: bool) -> dict[str, Any]:
        data = self._materials(task)
        data['draft'] = task.draft if final else None
        data['stop_reason'] = task.stop_reason
        data['budget'] = current_budget.get().public() if current_budget.get() else {}
        data['execution_errors'] = [{'step_id': item.get('step_id'), 'stage': item.get('stage'), 'error': item.get('error'),
            'validation_errors': [attempt.get('error') for attempt in (item.get('context') or {}).get('validation_attempts', []) if attempt.get('error')]}
            for item in task.logs if item.get('stage') in {'step_error', 'step_repair_exhausted'}][-5:]
        # Evidence is structured and complete. Large raw results are explicitly
        # marked as a preview here; the reviewer must request a smaller aggregate.
        for item in data['evidence']:
            if len(item.get('rows') or []) > 500:
                data['evidence'] = [{**entry, 'rows': entry.get('rows', [])[:500],
                    'review_input_truncated': len(entry.get('rows') or []) > 500} for entry in data['evidence']]
                break
        return data

    def _review_call(self, task: TaskRecord, final: bool) -> Review:
        if not final:
            raise RuntimeError('V1执行阶段不调用独立过程Reviewer')
        system = '''你是独立结果审核节点。核对must_answer的正确性、完整性、证据强度。
查询成功不等于业务正确；检查指标/事件时间/单位/联结粒度/完整性/两期可比与总分核对。
query_contract.validation.normalizations只是程序补齐元数据；pending_checks是尚待核实的计算/筛选，sql_summary来自真实SQL。basic_valid不认证业务正确；结合SQL与数据核对这些疑问，明显有误则repair，不能因为SQL已成功就忽略。可解释的等价写法不必强制返工或只给partial。
尚未查询不等于数据不支持；空结果需检查筛选/覆盖；有可行步骤继续，计划遗漏才replan。
SQL或口径错repair并列affected_steps；关键业务歧义clarify；公开能力核实不支持可partial。
前提下降不成立且口径完整时，premise_status必须设refuted（成立为confirmed，未核实为unknown），相关原因调查标not_applicable，可complete结束，不强找原因。
单步失败时继续独立必要步骤；不能拿无效结果计算。因果不可由相关性证明。
仅凭某一期的金额/渠道份额，不能判断流量、转化率或需求发生下降；两期渠道GMV也只能证明该渠道变化贡献，不能代替会话或转化证据。客单价上升不能排除SKU降价或结构变化；没有价格明细不能宣称已排除降价。
某品类贡献大应称主要变化来源，不能自动称原因。不得把尚未查询的比较期说成数据缺失。预算/修复是否耗尽只能引用程序budget和stop_reason，不得自行臆测。
格式/查询契约错误不代表数据不支持。若stop_reason非空或该步修复额度已耗尽，不再要求同一步无限修复；交付时核验可靠可答部分，给partial和明确未完成项。没有可靠证据则fail。
answered必须含question/evidence_ids/strength/explanation，引用真实结果ID；question逐字复制对应must_answer条目，不能改写；unanswered对齐必须回答项。
交付审核时还检查draft实际文字和关键数字；可直接修正文案到approved_text。
证据错误则回执行，不得用文案掩盖。只有final交付审核的complete/partial且approved_text非空才展示。
complete需要所有必须项已回答或明确不适用，且没有必要步骤待查、失败或阻塞。
review_input_truncated=true的片段不能支撑全集结论；应查询完整聚合。
不得建议经营写操作。返回符合此JSON Schema的对象：'''+json.dumps(Review.model_json_schema(), ensure_ascii=False)
        if final:
            system = ("这是交付前的批判性审核，不能因为草稿流畅或查询成功就放行。先逐句找无依据的推断并删除，再审核其余内容。\n"
                "若只有GMV、订单量、客单价证据，只能证明金额=单量×客单价的算术关系。客单价不是SKU单价；"
                "客单价上升绝不能用作排除降价、低价商品结构、需求变化的反证。没有SKU价格/数量明细必须删除这些判断。\n"
                "渠道GMV减少只支持该渠道的金额变化贡献，不能称流量、转化率、品牌吸引力已下降；这些只能列为未检验假设。\n"
                "可以完成有边界的变化调查，不强求唯一根因，但必须说明哪些解释仍未证实。关键数字注明结果ID。\n"
                "approved_text必须已删净无依据的排除/因果表述，不要把可能/暗示当成已获得的关联证据。\n\n" + system)
        review = Review.model_validate(self._call('delivery_review', system, self._review_payload(task, final), task))
        changes = []
        context = self._reference_context(task, task.evidence)
        for item in review.answered:
            item.evidence_ids = resolve_result_ids(item.evidence_ids, context, changes)
        if changes:
            task.logs.append({'stage': 'result_reference_normalization', 'node': 'delivery_review', 'changes': changes})
        if not set(review.affected_steps) <= {step['id'] for step in task.steps}:
            raise ValueError('审核修复引用未知步骤')
        task.reviews.append({'final': final, **review.model_dump()})
        return review

    def _review(self, state: dict) -> dict:
        task = state['record']
        review = self._review_call(task, False)
        task.pending_review = review.model_dump()
        task.next_node = 'controller'
        return state

    @staticmethod
    def _wait(task: TaskRecord, clarification: dict) -> None:
        payload = {**clarification, 'options': clarification.get('options') or []}
        task.clarification = Clarification.model_validate(payload).model_dump()
        task.final_status = 'waiting_clarification'
        task.next_node = 'end'

    def _controller(self, state: dict) -> dict:
        task = state['record']
        if task.pending_decision:
            proposal = task.pending_decision
            task.pending_decision = None
            action = proposal['action']
            task.feedback = proposal['reason']
            if action in {'skip_step', 'request_delivery'}:
                # The model's remaining list is informational, never authority
                # to erase independent user requirements or steps.
                affected = set(proposal['affected_step_ids'])
                related = set(affected)
                # Plans are validated in dependency order. Descendants of a
                # skipped step are part of that branch, not independent work
                # merely because the proposal names only the branch's root.
                for item in task.steps:
                    if set(item['depends_on']) & related:
                        related.add(item['id'])
                for requirement in proposal['affected_requirement_ids']:
                    independent = any(requirement in item.get('requirement_ids', [])
                                      and item['id'] not in related and item['status'] in {'pending', 'blocked'}
                                      for item in task.steps)
                    if not independent:
                        task.not_applicable_requirements.add(requirement)
                skipped = set(affected)
                for step in task.steps:
                    branch_only = (set(step['depends_on']) & skipped and step.get('requirement_ids')
                                   and set(step['requirement_ids']) <= set(proposal['affected_requirement_ids']))
                    if (step['id'] in affected or branch_only) and step['status'] in {'pending', 'blocked'}:
                        step['status'] = 'not_applicable'
                        skipped.add(step['id'])
                task.next_node = 'execute'
            elif action == 'request_clarification':
                self._wait(task, proposal['clarification'])
            elif action == 'request_replan':
                budget = current_budget.get()
                if budget:
                    budget.replan()
                task.next_node = 'planner'
            else:
                raise ValueError('Controller收到无效前置动作')
            task.logs.append({'stage': 'controller_decision', 'step_id': proposal['step_id'],
                              'suggested_action': action, 'next_node': task.next_node,
                              'remaining_requirement_ids': sorted(set(self._requirements(task)) - task.not_applicable_requirements)})
            return state
        review = Review.model_validate(task.pending_review)
        budget = current_budget.get()
        task.feedback = review.reason
        for name in review.additional_skills:
            if name in {'business_diagnosis', 'goal_analysis', 'inventory_risk_check'} and name not in task.skills:
                task.skills.append(name)
        action = review.action
        pending = any(item['status'] == 'pending' for item in task.steps)
        if action == 'continue' and not pending and review.unanswered:
            # The review can say "continue" while identifying an already
            # completed step that lacks evidence. Repair that step rather than
            # incorrectly treating the absence of queued work as unsupported data.
            action = 'repair' if review.affected_steps else 'replan'
        if action == 'clarify' and review.clarification:
            self._wait(task, review.clarification)
        elif action == 'repair' and review.affected_steps:
            repaired = False
            roots = self._repair_roots(task, review.affected_steps)
            for step_id in roots:
                try:
                    if budget:
                        budget.step = step_id
                        budget.repair('结果审核定向修复')
                    self.invalidate(task, step_id)
                    repaired = True
                except BudgetStop:
                    # An exhausted root cannot make blocked descendants runnable.
                    # Keep existing evidence and statuses; do not create work.
                    pass
            if not repaired:
                task.logs.append({'stage': 'local_repair_exhausted', 'affected_steps': roots})
            task.next_node = 'execute' if repaired or any(item['status']=='pending' for item in task.steps) else 'draft' 
        elif action == 'replan':
            if budget:
                budget.replan()
            task.next_node = 'planner'
        elif action == 'continue':
            if any(item['status'] == 'pending' for item in task.steps):
                task.next_node = 'execute'
            else:
                task.next_node = 'draft'
        else:
            # A model cannot silently skip pending necessary work. Only a
            # documented false premise can make remaining diagnostics N/A.
            false_premise = action == 'complete' and (review.premise_status == 'refuted' or any(item.strength == 'not_applicable' for item in review.answered))
            if false_premise:
                for item in task.steps:
                    if item['status'] in {'pending', 'blocked'}:
                        item['status'] = 'not_applicable'
            if action in {'complete', 'partial'} and any(item['status']=='pending' for item in task.steps) and not false_premise:
                task.next_node = 'execute'
            else:
                task.next_node = 'draft'
        return state

    def _draft(self, state: dict) -> dict:
        task = state['record']
        budget = current_budget.get()
        if budget:
            budget.phase = 'closing'
            budget.step = 'closing'
        if not task.evidence and any(item['status'] == 'failed' for item in task.steps):
            task.final_status = 'failed'
            task.stop_reason = task.stop_reason or '必要查询失败，没有可靠的业务结果'
            task.next_node = 'end'
            return state
        draft_signature = self._delivery_signature(task, include_draft=False) + task.feedback
        if draft_signature in task.draft_signatures:
            raise BudgetStop('证据与修正要求没有实质变化，停止重复生成相同草稿')
        task.draft_signatures.add(draft_signature)
        result = self.workflow.data_qa_agent.run(task.state['query'], {
            'short_term': '', 'recent_result': '', 'selected_tables': '',
            'scenario': task.state.get('scenario') or {},
            'harness_evidence': json.dumps(model_result_context(self._review_payload(task, False)), ensure_ascii=False, default=str),
            'business_methods': json.dumps(self._selected_skills(task), ensure_ascii=False),
            'instruction': '生成交付草稿，answer或report均可；关键数字注明结果ID；使用完整结构化证据，不可编造，不调用图表工具。说明未完成项、强度和限制。',
        }, task.state['access_scope'])
        task.draft = result.report.markdown if result.report else result.answer
        task.next_node = 'delivery_review'
        return state

    @staticmethod
    def _delivery_signature(task: TaskRecord, include_draft: bool = True) -> str:
        evidence = []
        for item in task.evidence:
            if item.get('check_status') == 'superseded':
                continue
            rows = sorted((json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
                           for row in item.get('rows', [])))
            evidence.append({key: item.get(key) for key in ['goal', 'datasource_id', 'conditions', 'snapshot', 'columns', 'complete', 'query_contract']}
                            | {'rows': rows})
        payload = {'requirements': task.required, 'conditions': task.conditions,
                   'not_applicable': sorted(task.not_applicable_requirements),
                   'evidence': sorted(evidence, key=lambda item: json.dumps(item, sort_keys=True, default=str))}
        if include_draft:
            # Ignore whitespace and identifier-only changes, not changed claims.
            draft = re.sub(r'(?<![A-Za-z0-9])[a-f0-9]{12}:R\d+(?![A-Za-z0-9])', '<result>', task.draft)
            payload['draft'] = re.sub(r'\s+', '', draft)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    @staticmethod
    def _can_investigate() -> bool:
        budget = current_budget.get()
        return not budget or (budget.elapsed() < min(budget.seconds, budget.investigation_seconds)
                              and budget.tools < budget.tool_limit)

    @staticmethod
    def _repair_roots(task: TaskRecord, candidates: list[str]) -> list[str]:
        """Only return roots whose repair can actually restore a requested path."""
        by_id = {step['id']: step for step in task.steps}
        budget = current_budget.get()

        def recover(ident: str, seen: set[str]) -> set[str] | None:
            if ident in seen or ident not in by_id:
                return None
            step = by_id[ident]
            if step['status'] == 'not_applicable' or ident in task.non_retryable_steps:
                return None
            unavailable = [dep for dep in step['depends_on'] if by_id[dep]['status'] != 'completed']
            if unavailable:
                roots: set[str] = set()
                for dep in unavailable:
                    path = recover(dep, seen | {ident})
                    if path is None:
                        return None
                    roots.update(path)
                return roots
            if budget and budget.repairs.get(ident, 0) >= budget.repair_limit:
                return None
            return {ident}

        roots = set()
        for ident in candidates:
            roots.update(recover(ident, set()) or set())
        # If both an ancestor and its descendant are named, invalidate once
        # at the ancestor. This keeps independent valid results untouched.
        for step in task.steps:
            if step['id'] not in roots:
                continue
            frontier = list(step['depends_on'])
            while frontier:
                dep = frontier.pop()
                if dep in roots:
                    roots.discard(step['id'])
                    break
                frontier.extend(by_id[dep]['depends_on'])
        return [step['id'] for step in task.steps if step['id'] in roots]

    def _review_work(self, task: TaskRecord, review: Review) -> bool:
        """Route remaining executable work before considering a terminal answer."""
        if not self._can_investigate():
            return False
        budget = current_budget.get()
        by_id = {step['id']: step for step in task.steps}
        ready = [step for step in task.steps if step['status'] == 'pending'
                 and all(by_id[dep]['status'] == 'completed' for dep in step['depends_on'])]
        action = review.action
        if ready:
            task.next_node = 'execute'
        else:
            answered = {item.question for item in review.answered if item.evidence_ids or item.strength == 'not_applicable'}
            not_applicable = {question for ident, question in self._requirements(task).items()
                              if ident in task.not_applicable_requirements}
            missing = (set(task.required) - answered - not_applicable) | (set(review.unanswered) - not_applicable)
            candidates = review.affected_steps or [s['id'] for s in task.steps if s['status'] == 'failed']
            repairable = self._repair_roots(task, candidates)
            if repairable and (action == 'repair' or missing or any(s['status'] == 'failed' for s in task.steps)):
                task.pending_review = {**review.model_dump(), 'action': 'repair', 'affected_steps': repairable}
                task.next_node = 'controller'
            elif action == 'clarify' and review.clarification:
                task.pending_review = review.model_dump()
                task.next_node = 'controller'
            elif (missing or action in {'replan', 'continue'}) and review.issue_type != 'unsupported' and not candidates and (not budget or budget.replans < budget.replan_limit):
                task.pending_review = {**review.model_dump(), 'action': 'replan'}
                task.next_node = 'controller'
            else:
                return False
        if budget:
            budget.phase = 'investigation'
        task.logs.append({'stage': 'delivery_return_to_work', 'next_node': task.next_node,
                          'reason': review.reason})
        return True

    @staticmethod
    def delivery_text_issues(text: str) -> list[str]:
        """A narrow, general arithmetic guard, not a business-answer template.

        Amount/order (AOV) cannot establish an amount/unit price or mix claim.
        Reject the assertion and return it to the reviewer; never rewrite an
        already approved answer with a regex or inject reference numbers.
        """
        sentences = [part.strip() for part in re.split(r'[。！？\n]', text) if part.strip()]
        issues = []
        for index, sentence in enumerate(sentences):
            context = (sentences[index-1] + '。' if index else '') + sentence
            if not re.search(r'客单价|\bAOV\b', context, re.I):
                continue
            if not re.search(r'降价|低价|低值|单价|商品结构|商品组合|price|discount|product mix', sentence, re.I):
                continue
            if not re.search(r'排除|证明|表明|说明|并非|不是|rule.?s? out|exclud|prov', sentence, re.I):
                continue
            if re.search(r'不能|无法|不足以|不应|不正确|错误|未验证|未证实|does not|cannot|not sufficient', sentence, re.I):
                continue
            issues.append('客单价与SKU单价/商品组合不是同一指标；以下排除或推断缺少有效依据：' + sentence[:240])
        return issues

    def _delivery_review(self, state: dict) -> dict:
        task = state['record']
        signature = self._delivery_signature(task)
        if signature in task.delivery_signatures:
            raise BudgetStop('草稿与实质证据未变化，禁止重复交付审核')
        task.delivery_signatures.add(signature)
        review = self._review_call(task, True)
        text_issues = self.delivery_text_issues(review.approved_text) if review.approved_text else []
        if text_issues:
            task.reviews[-1]['programmatic_text_check'] = {'passed': False, 'issues': text_issues}
            task.logs.append({'stage': 'delivery_text_check', 'success': False, 'issues': text_issues})
            budget = current_budget.get()
            if budget:
                budget.step = 'closing'
                budget.repair('交付文案证据约束修正')
            task.feedback = ('交付未通过以下基础证据检查，必须删除对应断言并重新审核草稿；'
                '可以交付有边界的算术变化调查，不能再用客单价推断或排除SKU价格/商品组合。'
                '若无相应数据，直接明确未验证。' + '；'.join(text_issues))
            task.next_node = 'draft'
            return state
        task.reviews[-1]['programmatic_text_check'] = {'passed': True, 'issues': []}
        if self._review_work(task, review):
            return state
        if review.action in {'complete', 'partial'} and review.approved_text.strip():
            valid_ids = {item['id'] for item in task.evidence if item['check_status'] in {'basic_valid', 'reviewed'}}
            for item in review.answered:
                if not set(item.evidence_ids) <= valid_ids:
                    raise ValueError('最终审核使用未通过检查的结果')
            unresolved_steps = any(item['status'] in {'pending', 'failed', 'blocked'} for item in task.steps)
            not_applicable = {question for ident, question in self._requirements(task).items() if ident in task.not_applicable_requirements}
            all_covered = set(task.required) <= ({item.question for item in review.answered} | not_applicable)
            completed = review.action == 'complete' and not (set(review.unanswered) - not_applicable) and all_covered and not unresolved_steps
            task.final_status = 'completed' if completed else 'partial'
            task.approved_text = review.approved_text
            task.final_reviewed = True
            if completed:
                if task.stop_reason:
                    task.logs.append({'stage': 'historical_stop_resolved', 'reason': task.stop_reason})
                task.stop_reason = ''
            for item in task.evidence:
                if item['id'] in {rid for answer in review.answered for rid in answer.evidence_ids}:
                    item['check_status'] = 'reviewed'
            task.next_node = 'end'
        else:
            task.stop_reason = task.stop_reason or review.reason
            task.final_status = 'partial' if task.evidence else 'failed'
            task.next_node = 'end'
        return state

    def _close_after_stop(self, task: TaskRecord) -> None:
        budget = current_budget.get()
        if budget:
            budget.phase = 'closing'
            budget.step = 'closing'
        task.final_status = 'partial' if any(item['check_status'] in {'basic_valid','reviewed'} for item in task.evidence) else 'failed'
        try:
            closing_repairs_exhausted = budget is not None and budget.repairs.get('closing', 0) >= budget.repair_limit and 'closing' in task.stop_reason and '修复额度' in task.stop_reason
            if task.evidence and not closing_repairs_exhausted and (not budget or budget.remaining() > 1):
                task.next_node = 'draft'
                while task.next_node in {'draft', 'delivery_review'}:
                    if task.next_node == 'draft':
                        self._draft({'record': task})
                    else:
                        self._delivery_review({'record': task})
        except (BudgetStop, PipelineStageError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            task.logs.append({'stage': 'closing_incomplete', 'success': False, 'error': str(exc)})
        task.next_node = 'end'
        # Never expose a draft as an approved business conclusion.
        if not task.final_reviewed:
            task.approved_text = ''

    def _result(self, task: TaskRecord) -> QueryResult:
        valid = [item for item in task.evidence if item['check_status'] in {'basic_valid', 'reviewed'}]
        latest = next((item for item in reversed(valid) if item.get('sql')), None)
        status = task.final_status or ('partial' if valid else 'failed')
        message = {'completed': '调查完成', 'partial': '部分完成', 'waiting_clarification': '等待补充', 'failed': '调查失败'}[status]
        if not task.final_reviewed and status != 'waiting_clarification':
            message += '（尚未完成交付审核）'
        text = task.approved_text or ('请补充关键条件后继续当前任务。' if status == 'waiting_clarification' else
            '尚未完成交付审核。' + ('已取得的结构化结果仅供核对；' if valid else '未取得可靠的业务查询结果；') + '未形成完整经营结论。停止原因：' + (task.stop_reason or '审核未通过'))
        public_evidence = [{**item, 'rows': item.get('rows', [])[:200],
                            'display_truncated': len(item.get('rows') or []) > 200} for item in task.evidence]
        answered = {item['question'] for item in task.reviews[-1].get('answered', [])} if task.final_reviewed and task.reviews else set()
        requirements = [{'id': ident, 'question': question,
                         'status': 'not_applicable' if ident in task.not_applicable_requirements
                         else 'answered' if question in answered else 'unanswered'}
                        for ident, question in self._requirements(task).items()]
        return QueryResult(task_id=task.state['task_id'], status=status, route=task.state['intent']['action'],
            message=message, analysis=text, clarification=task.clarification,
            sql=latest.get('sql') if latest else None, columns=latest.get('columns', []) if latest else [],
            rows=latest.get('rows', [])[:200] if latest else [], workflow_mode='investigation_harness_v1',
            standalone_query=task.state.get('standalone_query'), result_title='经营调查证据',
            plan=task.steps, evidence=public_evidence, reviews=task.reviews, final_reviewed=task.final_reviewed,
            step_decisions=task.step_decisions, requirements=requirements,
            stop_reason=task.stop_reason or None, required_questions=task.required,
            selected_skills=task.skills, execution_log=task.logs, retrieval_history=task.retrievals,
            budget=current_budget.get().public() if current_budget.get() else {},
            result_ref=latest.get('result_ref') if latest else None,
            total_rows=latest.get('total_rows') if latest else None,
            preview_truncated=len(latest.get('rows', []))>200 if latest else False)
