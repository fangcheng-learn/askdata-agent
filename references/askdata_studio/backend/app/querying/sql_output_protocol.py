"""One SQL model response schema and deterministic legacy clarification adapter.

The schema describes model output. SQL-derived structure belongs to the query
contract builder; no model-authored table/time/dimension copy is required in v4.
This module does not execute SQL, change permissions, or call another model.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import Clarification, ClarificationOption
from .query_contract import MetricContract
from .calculations import StateDurationRequest


class _Output(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LocalMeasurement(_Output):
    output_columns: list[str] = Field(default_factory=list)
    id: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    scope: str = Field(min_length=1, description='适用对象、期间与统计集合；不是永久登记定义')
    source_fields: list[str] = Field(default_factory=list)
    statistical_entity: str | None = None
    event_time: list[str] = Field(default_factory=list)
    unit: str | None = None
    formula: str | None = None
    population: str | None = None
    cohort: str | None = None


class BusinessClarification(Clarification):
    model_config = ConfigDict(extra='forbid')
    category: Literal['business_parameter', 'metric_definition', 'entity_identity']
    options: list[ClarificationOption] = Field(default_factory=list)


class StateDurationPostprocess(StateDurationRequest):
    # Bound by Controller to this query's actual saved result, never a model-
    # chosen task or a storage handle.
    result_id: Literal['query_result'] = 'query_result'


def normalize_postprocess(value: Any) -> dict | None:
    if value is None:
        return None
    return StateDurationPostprocess.model_validate(value).model_dump(mode='json')


class SqlMetricDeclaration(MetricContract):
    # Required in the new provider schema; old persisted contracts still load
    # with an empty list and retain their explicit unbound status.
    output_columns: list[str] = Field(min_length=1)


class SqlQueryOutput(_Output):
    query_id: str = Field(min_length=1)
    status: Literal['query']
    sql: str = Field(min_length=1)
    metric_declarations: list[SqlMetricDeclaration] = Field(min_length=1)
    local_measurements: list[LocalMeasurement] = Field(default_factory=list)
    technical_note: str = ''
    postprocess: StateDurationPostprocess | None = Field(default=None, description='可选：对本查询完整原始台账执行受控状态时长计算，由Controller执行，无需主Agent再发一次查询')


class ClarifyOutput(_Output):
    query_id: str = Field(min_length=1)
    status: Literal['clarify']
    clarification: BusinessClarification
    reason: str = ''


class MissingRequest(_Output):
    kind: Literal['field', 'metric', 'join', 'entity_mapping', 'cohort', 'other']
    request: str = Field(min_length=1)
    reason: str = ''


class MissingContextOutput(_Output):
    query_id: str = Field(min_length=1)
    status: Literal['missing_context']
    reason: str = Field(min_length=1)
    missing_requests: list[MissingRequest] = Field(default_factory=list)


class UnsupportedOutput(_Output):
    query_id: str = Field(min_length=1)
    status: Literal['unsupported']
    reason: str = Field(min_length=1)


class SqlBatchOutput(_Output):
    queries: list[SqlQueryOutput | ClarifyOutput | MissingContextOutput | UnsupportedOutput] = Field(min_length=1)


def sql_batch_output_schema(query_ids: list[str] | tuple[str, ...],
                            allow_preparation_actions: bool = False) -> dict[str, Any]:
    """Generate the schema from actual types, then bind server-owned IDs.

    Legacy single-query preparation has a distinct tool protocol. It must not
    be accidentally constrained to a batch response. Its existing prompt and
    validation path remain active until it is explicitly migrated by caller.
    """
    if allow_preparation_actions:
        raise ValueError('普通单项准备使用既有工具协议，不能套用协议4批次Schema')
    ids = list(dict.fromkeys(query_ids))
    if not ids or len(ids) != len(query_ids) or not all(isinstance(qid, str) and qid.strip() for qid in ids):
        raise ValueError('SQL批次query_id必须为非空且唯一的字符串')
    schema = SqlBatchOutput.model_json_schema()
    for definition in schema.get('$defs', {}).values():
        properties = definition.get('properties') or {}
        if 'query_id' in properties:
            properties['query_id']['enum'] = ids
    schema['description'] = ('只返回调用方已绑定查询。成功项仅声明实际指标含义；'
        'tables/dimensions/filters/time_range/grain等机械结构由程序从SQL构造，不能另写一份。'
        '只有业务参数、指标定义或实体身份真正不明确才clarify；技术字段映射使用missing_context。')
    return schema


def normalize_clarification(detail: Any) -> dict[str, Any]:
    """Accept explicit legacy option representations without inventing choices.

    String options and label-only objects describe the same user choice as a
    ClarificationOption. They get deterministic local IDs and missing optional
    metadata. Unknown or malformed choices fail here, before controller state
    is mutated, instead of raising deep inside the waiting-user branch.
    Category is routing metadata and is intentionally not a Clarification field.
    """
    if not isinstance(detail, dict):
        raise ValueError('clarification必须为对象')
    question = detail.get('question')
    if not isinstance(question, str) or not question.strip():
        raise ValueError('clarification.question必须为非空业务问题')
    options = detail.get('options')
    if options is None:
        options = []
    if not isinstance(options, list):
        raise ValueError('clarification.options必须是选项数组')
    normalized = []
    seen = set()
    for index, option in enumerate(options):
        if isinstance(option, str):
            item: dict[str, Any] = {'label': option}
        elif isinstance(option, dict):
            item = deepcopy(option)
        else:
            raise ValueError(f'clarification.options[{index}]必须为文字或选项对象')
        label = item.get('label')
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f'clarification.options[{index}].label必须为非空文字')
        label = label.strip()
        identity = item.get('id')
        if identity is None or identity == '':
            digest = hashlib.sha256(label.encode('utf-8')).hexdigest()[:10]
            identity = f'option_{index + 1}_{digest}'
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError('clarification选项编号必须非空且唯一')
        seen.add(identity)
        parsed = ClarificationOption.model_validate({
            'id': identity, 'label': label, 'description': item.get('description') or '',
            'recommended': item.get('recommended', False), 'metric_sources': item.get('metric_sources') or {},
        })
        normalized.append(parsed.model_dump())
    return Clarification.model_validate({
        'parameter': detail.get('parameter') or 'other', 'question': question.strip(),
        'reason': detail.get('reason') or '缺失的业务条件会影响本次查询', 'options': normalized,
    }).model_dump()


def metric_declarations_contract(item: dict[str, Any]) -> dict[str, Any]:
    """Convert only the typed business declaration, never infer business intent."""
    values = item.get('metric_declarations')
    if not isinstance(values, list) or not values:
        raise ValueError('metric_declarations必须为非空指标声明数组')
    if 'query_contract' in item:
        raise ValueError('不能同时返回metric_declarations与第二份query_contract')
    return {'metrics': [MetricContract.model_validate(value).model_dump() for value in values]}


def local_measurements(values: Any, *, protocol_version: int) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ValueError('local_measurements必须为数组')
    if protocol_version >= 4:
        parsed = [LocalMeasurement.model_validate(item).model_dump(exclude_none=True) for item in values]
    else:
        if any(not isinstance(item, dict) or not str(item.get('id') or '').strip()
               or not str(item.get('definition') or '').strip() for item in values):
            raise ValueError('local_measurements必须逐项明确id和definition')
        parsed = deepcopy(values)
    return [{**item, 'kind': 'task_local', 'validation_status': 'requires_evidence_and_review'} for item in parsed]
