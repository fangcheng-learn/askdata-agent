from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field


class DateTimeResult(BaseModel):
    date: str = Field(description="当前日期，格式为YYYY-MM-DD")
    datetime: str = Field(description="包含时区的当前时间")
    timezone: str = Field(description="返回结果使用的时区")


class DateRangeResult(BaseModel):
    # MCP derives output schemas through TypeAdapter's default validation mode.
    # This output-only model must advertise its computed wire field as well.
    model_config = ConfigDict(json_schema_mode_override='serialization')

    expression: str = Field(description="输入的相对时间表达式")
    start_date: str = Field(description="包含起始当天的日期，格式为YYYY-MM-DD；SQL使用 >= start_date")
    end_date: str = Field(description="兼容字段：结束日期，格式为YYYY-MM-DD，包含当天；不可直接作为SQL < 的排他终点")
    timezone: str = Field(description="计算日期范围时使用的时区")

    @computed_field(description="不包含当天的排他终点，格式为YYYY-MM-DD；由end_date加一天计算，SQL及查询契约使用 < end_exclusive")
    @property
    def end_exclusive(self) -> str:
        return (date.fromisoformat(self.end_date) + timedelta(days=1)).isoformat()


class DatabaseQueryResult(BaseModel):
    database: str = Field(description="执行查询的数据库名称")
    sql: str = Field(description="实际执行或尝试执行的SQL")
    success: bool = Field(description="SQL是否执行成功")
    columns: list[str] = Field(default_factory=list, description="结果字段")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="查询结果，最多200行")
    row_count: int = Field(default=0, description="返回结果行数")
    error: str | None = Field(default=None, description="执行失败时的错误信息")

    total_rows: int | None = None
    preview_truncated: bool = False
    complete: bool = True
    result_ref: str | None = None


class ChartSpecResult(BaseModel):
    type: str = Field(description="前端图表类型：bar或pie")
    title: str = Field(description="图表标题")
    source_task_id: str = Field(description="提供图表数据的查询任务ID")
    category_field: str = Field(description="分类字段")
    value_field: str = Field(description="数值字段")
    max_items: int = Field(description="前端最多展示的数据项数")
