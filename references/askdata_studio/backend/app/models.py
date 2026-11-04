from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConfirmedField(BaseModel):
    name: str
    aggregation: Literal["auto", "group", "sum", "avg", "count", "max", "min"] = "auto"
    tableId: str | None = None


class AnalysisTableInput(BaseModel):
    task_id: str
    title: str
    query: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceConfig(BaseModel):
    datasource_id: str | None = None
    confirmed_schema_tables: list[str] = Field(default_factory=list)
    confirmed_parameters: dict[str, Any] = Field(default_factory=dict)
    schema_fields: list[ConfirmedField] = Field(default_factory=list)
    analysis_table_ids: list[str] = Field(default_factory=list)
    analysis_tables: list[AnalysisTableInput] = Field(default_factory=list)
    # 兼容旧版前端的 fields 属性。
    fields: list[ConfirmedField] = Field(default_factory=list)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    session_id: str = "demo-session"
    workspace: WorkspaceConfig | None = None


class ClarificationRequest(BaseModel):
    option_id: str


class ClarificationOption(BaseModel):
    id: str
    label: str
    description: str
    recommended: bool = False
    metric_sources: dict[str, str] = Field(default_factory=dict)


class Clarification(BaseModel):
    required: bool = True
    allow_skip: bool = False
    parameter: str = "other"
    question: str
    reason: str
    options: list[ClarificationOption]


class Interpretation(BaseModel):
    metric: str
    dimension: str
    time_range: str
    table: str
    assumptions: list[str] = []


class VisualizationSpec(BaseModel):
    type: Literal["bar", "grouped_bar", "line", "area", "pie", "donut", "scatter"]
    title: str
    section_heading: str | None = Field(default=None, max_length=160, description="图表对应的正文小节标题，原样填写；程序将图表放在该标题下、分析文字前。未匹配时使用默认位置。")
    source_task_id: str = ""
    source_result_id: str | None = None
    source_version: str | None = None
    category_field: str
    series_field: str | None = Field(default=None, description='分组柱状图或多系列趋势图使用的实际组别列')
    value_field: str
    x_field: str | None = Field(default=None, description="散点图横轴的实际数值列；纵轴为value_field，点名为category_field")
    max_items: int = Field(default=12, ge=3, le=30)
    order: Literal["value_desc", "category_asc", "source"] = "value_desc"
    unit: str | None = None
    currency: str | None = None
    precision: int = Field(default=2, ge=0, le=8)


class ReportDataSource(BaseModel):
    """Authorized data frozen together with the report reviewed for delivery."""

    result_id: str
    version: str
    task_id: str = ""
    datasource_id: str = ""
    title: str = "查询结果"
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    complete: bool = False
    provided_rows: int = 0
    total_rows: int | None = None
    units: dict[str, str] = Field(default_factory=dict)
    currencies: dict[str, str] = Field(default_factory=dict)
    precisions: dict[str, int] = Field(default_factory=dict)


class AnalysisReport(BaseModel):
    title: str
    markdown: str
    visualizations: list[VisualizationSpec] = Field(default_factory=list)
    data_sources: list[ReportDataSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    continuation_context: dict[str, Any] = Field(default_factory=dict)


class QueryResult(BaseModel):
    task_id: str
    session_id: str | None = None
    status: Literal["waiting_clarification", "completed", "partial", "failed"]
    route: Literal["database_query", "data_qa", "direct_response"]
    message: str
    interpretation: Interpretation | None = None
    clarification: Clarification | None = None
    steps: list[str] = []
    sql: str | None = None
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    analysis: str | None = None
    saved: bool = False
    route_reason: str | None = None
    retrieval: dict[str, Any] | None = None
    execution_log: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    result_title: str | None = None
    analysis_sources: list[dict[str, Any]] = Field(default_factory=list)
    standalone_query: str | None = None
    schema_graph: dict[str, Any] | None = None
    workflow_mode: str | None = None
    execution_protocol_version: int | None = None
    report: AnalysisReport | None = None
    report_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    datasource_id: str | None = None
    datasource: dict[str, Any] | None = None
    scenario: dict[str, Any] | None = None
    selected_semantics: list[dict[str, Any]] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    step_decisions: list[dict[str, Any]] = Field(default_factory=list)
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    required_questions: list[str] = Field(default_factory=list)
    selected_skills: list[str] = Field(default_factory=list)
    retrieval_history: list[dict[str, Any]] = Field(default_factory=list)
    final_reviewed: bool = False
    content_review_status: str | None = None
    stop_reason: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    result_ref: str | None = None
    total_rows: int | None = None
    preview_truncated: bool = False
    complete: bool = True



class SchemaField(BaseModel):
    name: str
    label: str
    type: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    role: str = ""
    aggregation: str = "none"
    sql_type: str = ""
    unit: str = ""
    nullable: bool | None = None
    enum_values: list[Any] = Field(default_factory=list)
    source_type: str = ""
    data_profile: dict[str, Any] = Field(default_factory=dict)
    index_content: dict[str, str] = Field(default_factory=dict)


class SchemaTable(BaseModel):
    id: str
    name: str = ""
    label: str
    description: str
    fields: list[SchemaField]
    database: str = "askdata_mock"
    domain: str = ""
    business_terms: list[str] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    row_count: int = 0


class SaveMemoryRequest(BaseModel):
    task_id: str


class SaveFieldMemoryRequest(BaseModel):
    table_id: str
    name: str
    label: str
    field_type: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
