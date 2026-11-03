from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ..database import SCHEMA, physical_table_name
from ..querying.duckdb_engine import DuckDbEngine
from ..security import AccessController, AccessScope
from .. import homenova
from ..runtime_budget import current_budget
from .schemas import DateTimeResult, DateRangeResult
from .tools.time_tools import DateExpression, _date_range
from .tools import (
    build_bar_chart,
    build_database_query_tool,
    build_pie_chart,
    current_datetime,
    resolve_date_range,
)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def create_local_mcp_server(
    engine: DuckDbEngine | None = None,
    access_scope: AccessScope | None = None,
) -> MCPServer:
    """创建与FastAPI运行在同一进程中的MCP服务。"""
    database_engine = engine or DuckDbEngine()
    scope = access_scope or AccessController().resolve(None)
    home_database = next(iter(scope.allowed_databases)) if len(scope.allowed_databases) == 1 else ""
    home_database = home_database if homenova.is_homenova(home_database) else ""
    server = MCPServer(
        name="askdata-local-tools",
        title="AskData本地工具服务",
        description="提供本地数据库只读查询和基础时间计算工具。",
        instructions="调用数据库工具前先根据Schema图生成一条只读DuckDB SQL。" +
                     (homenova.scenario_text(home_database) if home_database else ""),
    )

    if home_database:
        def scenario_datetime(timezone_name: Literal["Europe/Berlin", "UTC"] = "Europe/Berlin") -> DateTimeResult:
            """返回真实机器时间；同任务复用首次解析的参考时刻。"""
            budget = current_budget.get()
            reference = budget.reference_now if budget else ''
            now = (datetime.fromisoformat(reference).astimezone(ZoneInfo(timezone_name))
                   if reference else datetime.now(ZoneInfo(timezone_name)))
            return DateTimeResult(date=now.date().isoformat(), datetime=now.isoformat(timespec="seconds"), timezone=timezone_name)

        def scenario_date_range(expression: DateExpression,
                                timezone_name: Literal["Europe/Berlin", "UTC"] = "Europe/Berlin") -> DateRangeResult:
            """按真实当前日期解析相对日期，不以快照日期替代今天。"""
            today = datetime.fromisoformat(scenario_datetime(timezone_name).datetime).date()
            start, end = _date_range(expression, today)
            return DateRangeResult(expression=expression, start_date=start.isoformat(), end_date=end.isoformat(), timezone=timezone_name)

        time_handler = scenario_datetime
        range_handler = scenario_date_range
    else:
        time_handler = current_datetime
        range_handler = resolve_date_range

    server.tool(
        name="current_datetime",
        title="获取当前日期时间",
        description="返回机器真实当前日期时间（Europe/Berlin或UTC），快照场景日期不代替今天。" if home_database else "获取Asia/Shanghai或UTC时区的当前日期与时间。",
        annotations=READ_ONLY,
    )(time_handler)
    server.tool(
        name="resolve_date_range",
        title="解析相对日期范围",
        description="将今天、昨天、本周、上周、本月、上月、今年或近N天／近N周转换为明确起止日期。近N日含今天，返回包含末日与排他终点；未说明最近多久先询问。",
        annotations=READ_ONLY,
    )(range_handler)
    server.tool(
        name="build_bar_chart",
        title="生成柱状图配置",
        description=(
            "基于一张已有查询结果生成前端柱状图配置。"
            "适合比较不同类别的数值，不读取数据库，也不生成模拟数据。"
        ),
        annotations=READ_ONLY,
    )(build_bar_chart)
    server.tool(
        name="build_pie_chart",
        title="生成饼图配置",
        description=(
            "基于一张已有查询结果生成前端饼图配置。"
            "只适合类别较少的占比分析，不读取数据库，也不生成模拟数据。"
        ),
        annotations=READ_ONLY,
    )(build_pie_chart)

    databases = sorted({str(table.get("database") or "askdata_mock") for table in SCHEMA})
    for database in databases:
        if not scope.allows_database(database):
            continue
        tables = [
            physical_table_name(table)
            for table in SCHEMA
            if table.get("database") == database
            and scope.allows_table(database, table["id"])
        ]
        handler = build_database_query_tool(database, database_engine, scope)
        server.tool(
            name=f"query_{database}",
            title=f"查询数据库 {database}",
            description=(
                f"在数据库{database}中执行一条只读DuckDB SQL。"
                f"可用表：{', '.join(tables)}。rows为最多200行预览，total_rows为完整行数，preview_truncated表示预览截断，complete表示服务端结果完整；result_ref可由授权任务读取。总体请用完整聚合。"
            ),
            annotations=READ_ONLY,
        )(handler)
    return server
