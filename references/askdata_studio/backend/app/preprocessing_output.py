"""Typed entry understanding; entity names remain user claims until resolved."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


RouteName = Literal["data_qa", "database_query", "direct_response", "metadata_query"]
ResponseType = Literal["answer", "clarification"]


class _Output(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AnalysisScope(_Output):
    kind: Literal['all', 'store', 'category', 'sku', 'channel', 'unknown']
    name_or_id: str = Field(description='用户或适用上下文中的名称/编号，不猜数据库ID；全盘为空')
    source_text: str = Field(description='支撑该范围的用户原话或上下文原文')


class KnownFinding(_Output):
    quote: str
    source: Literal['current_user_message', 'recent_context']
    status: Literal['unverified'] = 'unverified'


class Understanding(_Output):
    goal: str
    scope: AnalysisScope
    period: str
    baseline: str
    known_findings: list[KnownFinding]
    questions: list[str]


class Retrieval(_Output):
    retrieval_terms: list[str]
    metrics: list[str]
    dimensions: list[str]
    filters: list[str]
    time_expressions: list[str]
    operations: list[str]


class PreparedOutput(_Output):
    action: RouteName
    analysis_task_ids: list[str] = Field(description="data_qa所用上下文turn_id/task_id，只能选已展示且有数据的历史结果；其他路由为空")
    analysis_result_ids: list[str] = Field(description="本轮回答实际需要的完整R结果ID，只从所选历史任务目录中选择；不需要或普通旧表填空")
    analysis_mode: Literal['none', 'fact', 'explanation', 'report'] = Field(description='已有数据追问的用途：fact仅数值/差额/比例，explanation解释，report整理或补充报告；其他路由none')
    confidence: float = Field(ge=0, le=1)
    needs_investigation: bool = Field(description='需要经营取证或仍需澄清时为true；缺指标/期间不等于目标完成，交主Agent在原任务暂停')
    task_understanding: Understanding
    required_questions: list[str]
    reason: str
    standalone_query: str
    rewritten: bool
    response: str
    response_type: ResponseType = Field(description='clarification表示任务尚需用户补充，必须进入可恢复的主Agent任务；不是已完成答案')
    retrieval: Retrieval
