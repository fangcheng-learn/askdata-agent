"""Executable response schemas for investigation decisions and final delivery."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..models import Clarification, VisualizationSpec
from ..querying.analysis_output import ReviewFinding, TextEdit
from ..querying.calculations import (SumRequest, ColumnStatsRequest, ChangeRequest,
    RatioRequest, PeriodChangeRequest, ProductDecompositionRequest, StateDurationRequest)
from .progress_protocol import ProgressUpdate


class _Output(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LoadSkill(_Output):
    type: Literal['load_skill']
    names: list[str] = Field(min_length=1)


class QueryFilter(_Output):
    field: str = Field(min_length=1, description='真实字段或明确业务维度；值单独放入values，不把表达式写入字段名')
    operator: Literal['=', 'in', '!=', 'not in', '>', '>=', '<', '<='] = 'in'
    values: list[str | int | float | bool] = Field(min_length=1)


class QueryPeriod(_Output):
    start: str
    end: str
    end_inclusive: bool


class QueryConditions(_Output):
    months: list[str] = Field(default_factory=list, min_length=1, description='明确日历月，例如2026-06；省略时继承已确认期间')
    time_range: QueryPeriod | None = None
    filters: list[QueryFilter] = Field(default_factory=list,
        description='筛选条件逐项声明，例如field=category_code, operator=in, values=[POWER]；不筛选则省略或空数组')


class BusinessQuery(_Output):
    goal: str = Field(min_length=1)
    execution_mode: Literal['registered_metrics', 'exploratory'] = Field(default='exploratory',
        description='实现方式提示，最终由查询模块选择。仅声明登记指标、分组及筛选时优先固定计算；需要新原始证据或验证机制时选exploratory并声明required_output_fields或自定义度量，不能靠模式标志改变登记指标含义。')
    metric_requirements: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list,
        description='实际分组维度，不发明物理字段。跨月逐月比较须包含month，可同时按品类、SKU或渠道分组；conditions.months只筛选期间，不能代替月份分组。')
    required_output_fields: list[str] = Field(default_factory=list)
    input_result_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    comparison_purpose: str | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)


class QueryBatch(_Output):
    type: Literal['query_batch']
    queries: list[BusinessQuery] = Field(min_length=1)


class RepairQuery(_Output):
    type: Literal['repair_query']
    query_id: str = Field(min_length=1)
    issue: str = Field(min_length=1, description='已经取得的结果具体哪里不符合目标或与哪些证据冲突；不能只说重试')


class ResolveTime(_Output):
    type: Literal['resolve_time']
    expression: str = ''
    timezone_name: str | None = None


class ReadResult(_Output):
    type: Literal['read_result']
    result_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=1000)
    columns: list[str] = Field(default_factory=list)


class Clarify(_Output):
    type: Literal['clarify']
    required: bool = Field(default=True, description='入口必要条件为true；已取证后可选业务背景为false，可跳过但保留未知')
    parameter: Literal['time_range', 'baseline', 'scope', 'target', 'other'] = 'other'
    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Deliver(_Output):
    type: Literal['deliver']
    reason: str


_calculations = [create_model(model.__name__ + 'Action', __base__=model,
    type=(Literal['calculate'], ...), goal=(str, ''), repair_action_id=(str | None, None))
    for model in (SumRequest, ColumnStatsRequest, ChangeRequest, RatioRequest,
                  PeriodChangeRequest, ProductDecompositionRequest, StateDurationRequest)]
_Action = LoadSkill | QueryBatch | RepairQuery | ResolveTime | ReadResult | Clarify | Deliver
for _calculation in _calculations:
    _Action = _Action | _calculation


class InvestigationDecision(_Output):
    action_reason: str = Field(description='公开的简短动作目的与当前证据缺口，不含内部思维链')
    progress: ProgressUpdate | None = None
    action: _Action


class CompletionCheck(_Output):
    goal_status: Literal['answered', 'partial', 'unanswered', 'not_applicable']
    metric_scope_matches: bool
    capability_claims_supported: bool
    remaining_core_gaps: list[str]
    completion_basis: str


class QuestionAssessment(_Output):
    question_id: str
    status: Literal['answered', 'not_applicable', 'unresolved']
    evidence_ids: list[str]
    answer_level: Literal['fact', 'decomposition', 'contribution', 'association', 'mechanism']
    reason: str


class Answered(_Output):
    question: str
    evidence_ids: list[str]
    strength: Literal['fact', 'decomposition', 'contribution', 'association', 'cause', 'not_applicable']
    explanation: str = ''


class DeliveryReview(_Output):
    audit_findings: list[ReviewFinding] = Field(description='先审查五类实际证据，再决定action；每类一项')
    action: Literal['complete', 'partial', 'repair', 'clarify', 'fail']
    reason: str
    approved_text: str
    text_edits: list[TextEdit] = Field(default_factory=list,
        description='审核只检查，不修改报告，始终为空数组；修改要求交报告Agent')
    approve_draft_unchanged: bool = False
    answered: list[Answered]
    unanswered: list[str]
    can_repair: bool
    repair_scope: Literal['report', 'evidence', 'mixed', 'none'] = Field(default='none',
        description='仅正文数字换算、引用、措辞或图表表达错误用report；SQL/数据/必要取证错误用evidence；两者都有用mixed；不修复用none')
    repair_requests: list[dict[str, Any] | str]
    affected_evidence_ids: list[str] = Field(default_factory=list)
    clarification: Clarification | None = None
    completion_check: CompletionCheck
    question_assessments: list[QuestionAssessment]
    explanation_coverage_ack: bool
    visualizations_approved: bool = False


class InvestigationReport(_Output):
    action: Literal['report'] = 'report'
    answer: str = Field(default='', description='给运营人员的当前核心结论，简短说明业务含义，不列执行状态')
    title: str = Field(description='本次业务主题，不以证据摘要或未完成标签代替标题')
    markdown: str = Field(description='整合后的运营报告：结论优先，分析解释并配合图表，提出有依据的行动优先级，未完成事项放篇末；不按查询顺序堆表')
    visualizations: list[VisualizationSpec] = Field(default_factory=list, description='由报告Agent选择的图表，绑定真实来源和字段；不由模型提供图表数据')
    evidence_gaps: list[str] = Field(default_factory=list, description='需要后续调查或用户确认的核心缺口；内部审核字段，不代替报告正文')


class ReportEdit(_Output):
    before: str = Field(min_length=1, description='原稿中唯一匹配的完整文字片段；不得跨越其他改动')
    after: str = Field(description='替换后的准确文字；删除不可靠片段可为空串')


class ReportRevision(_Output):
    action: Literal['revise_report'] = 'revise_report'
    markdown_edits: list[ReportEdit]
    title: str | None = Field(default=None, description='仅标题需要修订时填写，否则null')
    visualizations: list[VisualizationSpec] | None = Field(default=None, description='仅图表需要修订时填写完整列表，否则null保留；空数组删除图表')
    evidence_gaps: list[str] | None = Field(default=None, description='必要未决项需要变化时填写，否则null保留')


def output_schema(node: str, *, skill_names: list[str] | None = None, result_columns: dict[str, list[str]] | None = None, query_ids: list[str] | None = None, review_context_refs: list[str] | None = None) -> dict | None:
    model = {'react': InvestigationDecision, 'delivery_review': DeliveryReview,
             'draft': InvestigationReport, 'report_revision': ReportRevision}.get(node)
    if model is None:
        return None
    schema = model.model_json_schema()
    if node in {'draft', 'report_revision'}:
        chart = schema['$defs']['VisualizationSpec']
        chart.setdefault('required', []).append('source_result_id')
        chart['properties']['source_result_id'] = {'type': 'string', 'minLength': 1}
        if result_columns:
            chart['properties']['source_result_id']['enum'] = sorted(result_columns)
    if node == 'react':
        schema['$defs']['BusinessQuery'].setdefault('required', []).append('execution_mode')
        # New model output has closed keys. Keep BusinessQuery's local dict
        # reader compatible with historical requests; normalize this wire
        # format before query identity, scope inheritance and compilation.
        conditions = QueryConditions.model_json_schema()
        schema['$defs'].update(conditions.pop('$defs', {}))
        conditions['properties']['months']['items']['pattern'] = r'^\d{4}-(0[1-9]|1[0-2])$'
        conditions['properties']['time_range'] = {'$ref': '#/$defs/QueryPeriod'}
        schema['$defs']['BusinessQuery']['properties']['conditions'] = conditions
    # Progress accepts old record extensions locally for migration. New model
    # output uses its defined properties, rather than inventing narrative keys.
    for definition in schema.get('$defs', {}).values():
        if definition.get('type') == 'object' and definition.get('properties'):
            definition['additionalProperties'] = False
    if node == 'delivery_review':
        schema['$defs']['ReviewFinding']['properties']['resolved_in_final']['description'] = '本次审核不修改正文；发现未修复错误时必须false，修订后重新审核'
        schema['properties']['approved_text']['enum'] = ['']
        schema['properties']['text_edits']['maxItems'] = 0
        schema.setdefault('required', []).extend(['text_edits', 'repair_scope'])
        if result_columns is not None:
            for definition in schema.get('$defs', {}).values():
                prop = definition.get('properties', {}).get('evidence_ids')
                if prop:
                    if result_columns:
                        prop['items']['enum'] = sorted(result_columns)
                    else:
                        prop['maxItems'] = 0
        if review_context_refs is not None:
            prop = schema['$defs']['ReviewFinding']['properties']['context_refs']
            if review_context_refs:
                prop['items']['enum'] = sorted(review_context_refs)
            else:
                prop['maxItems'] = 0
    if node == 'react' and skill_names is not None:
        if skill_names:
            schema['$defs']['LoadSkill']['properties']['names']['items']['enum'] = sorted(set(skill_names))
        else:
            schema['properties']['action']['anyOf'] = [branch for branch in
                schema['properties']['action']['anyOf'] if branch.get('$ref') != '#/$defs/LoadSkill']
    if node == 'react' and query_ids is not None:
        if query_ids:
            schema['$defs']['RepairQuery']['properties']['query_id']['enum'] = sorted(set(query_ids))
        else:
            schema['properties']['action']['anyOf'] = [branch for branch in schema['properties']['action']['anyOf']
                if branch.get('$ref') != '#/$defs/RepairQuery']
    if node == 'react' and result_columns is not None:
        if result_columns:
            read = schema['$defs']['ReadResult']['properties']
            read['result_id']['enum'] = sorted(result_columns)
            columns = sorted({c for cols in result_columns.values() for c in cols})
            if columns:
                read['columns']['items']['enum'] = columns
            else:
                read['columns']['maxItems'] = 0
        else:
            schema['properties']['action']['anyOf'] = [branch for branch in schema['properties']['action']['anyOf']
                if branch.get('$ref') != '#/$defs/ReadResult']
    return schema
