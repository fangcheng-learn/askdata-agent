"""Bounded step applicability proposals, validated before tools are dispatched.

This is not a business-rule interpreter. The executor interprets its planned
conditions; code enforces scope, evidence integrity and explicit numeric claims.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from .result_references import resolve_result_ids


class StepCondition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    affected_requirement_ids: list[str] = Field(min_length=1)
    affected_step_ids: list[str] = Field(min_length=1)
    comparison_required: bool = False


class NumericReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    result_id: str
    field: str
    row_match: dict[str, Any] = Field(default_factory=dict)


class NumericComparison(BaseModel):
    model_config = ConfigDict(extra='forbid')
    left: NumericReference
    operator: Literal['lt', 'le', 'eq', 'ne', 'ge', 'gt']
    right: NumericReference
    holds: bool


class StepDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['execute_step', 'skip_step', 'request_clarification', 'request_replan', 'request_delivery']
    premise_status: Literal['unknown', 'confirmed', 'refuted'] = 'unknown'
    condition_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    affected_requirement_ids: list[str] = Field(default_factory=list)
    affected_step_ids: list[str] = Field(default_factory=list)
    remaining_requirement_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    clarification: dict[str, Any] | None = None
    comparisons: list[NumericComparison] = Field(default_factory=list)


def _numeric(reference: NumericReference, evidence: dict[str, dict]) -> Decimal:
    result = evidence.get(reference.result_id)
    if not result or reference.field not in result.get('columns', []):
        raise ValueError('比较引用了未知结果或字段')
    rows = [row for row in result.get('rows', [])
            if all(key in row and row[key] == value for key, value in reference.row_match.items())]
    if len(rows) != 1:
        raise ValueError('比较的行定位必须唯一且真实存在')
    value = rows[0].get(reference.field)
    if isinstance(value, bool) or value is None:
        raise ValueError('比较值必须是有限数值')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('比较值必须是有限数值') from None
    if not number.is_finite():
        raise ValueError('比较值必须是有限数值')
    return number


def validate_step_decision(value: Any, context: dict[str, Any], *,
                           normalizations: list[dict[str, Any]] | None = None) -> StepDecision:
    decision = StepDecision.model_validate(value).model_copy(deep=True)
    requirements = set(context.get('requirements', {}))
    steps = {item['id'] for item in context.get('plan_steps', [])}
    dependencies = {item['id']: item for item in context.get('dependency_results', [])}
    decision.evidence_ids = resolve_result_ids(decision.evidence_ids, context, normalizations)
    for comparison in decision.comparisons:
        for reference in [comparison.left, comparison.right]:
            reference.result_id = resolve_result_ids([reference.result_id], context, normalizations)[0]
    if not set(decision.affected_requirement_ids + decision.remaining_requirement_ids) <= requirements:
        raise ValueError('前置决策引用了未知任务要求')
    if not set(decision.affected_step_ids) <= steps:
        raise ValueError('前置决策引用了未知步骤')
    condition = next((item for item in context.get('step_conditions', [])
                      if item['id'] == decision.condition_id), None)
    if decision.condition_id and condition is None:
        raise ValueError('前置决策引用了Planner未声明的条件')
    if decision.action in {'skip_step', 'request_delivery'}:
        if not condition or not decision.evidence_ids or decision.premise_status != 'refuted':
            raise ValueError('跳过或提前交付需要已推翻的计划条件及真实证据')
        if not decision.affected_step_ids or not decision.affected_requirement_ids:
            raise ValueError('跳过或提前交付必须声明影响范围')
        if context.get('step_id') not in decision.affected_step_ids:
            raise ValueError('不执行当前步的建议必须包含当前步，防止无进展循环')
        current = next((item for item in context.get('plan_steps', []) if item['id'] == context.get('step_id')), {})
        if not set(current.get('requirement_ids') or []) <= set(decision.affected_requirement_ids):
            raise ValueError('本步仍含独立必要目标，不能整个跳过')
        if not set(decision.affected_step_ids) <= set(condition['affected_step_ids']):
            raise ValueError('前置决策扩大了条件的步骤影响范围')
        if not set(decision.affected_requirement_ids) <= set(condition['affected_requirement_ids']):
            raise ValueError('前置决策扩大了条件的任务影响范围')
        if condition.get('comparison_required') and not decision.comparisons:
            raise ValueError('该前提需要真实数值比较，不能仅凭文字跳过')
        for result_id in decision.evidence_ids:
            result = dependencies[result_id]
            if not result.get('complete') or not result.get('rows') or result.get('check_status') not in {'basic_valid', 'reviewed'}:
                raise ValueError('不完整、空或失效证据不能推翻调查前提')
            if result.get('datasource_id') != context.get('datasource_id'):
                raise ValueError('前置决策证据快照不匹配')
            if (result.get('query_contract') or {}).get('validation', {}).get('pending_checks'):
                raise ValueError('证据仍有待核对的计算或范围，不能据此推翻前提并跳过调查；可继续核实或请求重规划')
        if decision.premise_status == 'refuted' and not decision.reason.strip():
            raise ValueError('前提被推翻必须给出依据')
    if decision.action == 'request_clarification':
        clarification = decision.clarification or {}
        if not all(clarification.get(key) for key in ['parameter', 'question', 'reason']):
            raise ValueError('澄清动作缺少必要问题和条件')
    for comparison in decision.comparisons:
        if not {comparison.left.result_id, comparison.right.result_id} <= set(decision.evidence_ids):
            raise ValueError('算术比较必须引用已声明的依赖证据')
        left, right = _numeric(comparison.left, dependencies), _numeric(comparison.right, dependencies)
        actual = {'lt': left < right, 'le': left <= right, 'eq': left == right,
                  'ne': left != right, 'ge': left >= right, 'gt': left > right}[comparison.operator]
        if actual != comparison.holds:
            raise ValueError('前置决策的数值比较与真实结果不一致')
    return decision


STEP_PROTOCOL_INSTRUCTION = '''
本次为Harness单步执行。你已收到任务原文、当前步骤、Planner条件、要求编号及真实dependency_results。
dependency_results.id是唯一对外证据编号，引用时从实际输入中选择，不填写存储UUID，不凭记忆猜测其他结果。
在原有这一次调用中先判断本步适用性，不重新审核整项任务，不生成最终报告。
本步适用时在同次调用返回执行产物，并附step_decision（action=execute_step）；不要先返回判断再另调用模型执行。
若命中Planner已声明的不适用条件，只返回step_decision，不生成或调用本步SQL。
step_decision只按下面Schema输出，引用真实结果和计划条件，明确受影响要求/步骤及剩余要求。
未查询、缺比较期、结果不完整或口径不一致不代表前提已推翻；应请求澄清或重规划，不编造能力不足。
依赖query_contract.validation.pending_checks为待核对项，basic_valid只代表基础结构检查；这些疑问未解决前，不能据此建议跳过或提前交付。允许本步继续取证，最终Reviewer核对SQL、数据及疑问，不另增审核调用。
对于可直接核对的两期数值大小判断，comparisons中提供真实结果行定位和比较断言，由代码复核。
跳过或交付只是建议；Controller会核验范围并继续独立目标。不得自行宣称预算耗尽或全部完成。
已命中条件时立即返回简短动作依据，不扩展调查或输出长篇推理。clarification允许options为空的自由文本补充。
'''
