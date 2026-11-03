from __future__ import annotations

import asyncio
import json
from uuid import uuid4
from typing import Any

from ..runtime_budget import current_budget, BudgetStop

from mcp import Client
from mcp.server import MCPServer


class LocalMcpClient:
    """通过MCP进程内传输发现并调用工具。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server

    def list_tools(self) -> list[dict[str, Any]]:
        budget = current_budget.get()
        if budget:
            budget.check()
        try:
            from ..workbench_runtime import current_execution
            control = current_execution.get()
            found = asyncio.run(self._list_tools())
            return [tool for tool in found if not control or control.tools is None or tool['name'] in control.tools]
        except TimeoutError as exc:
            if budget:
                raise BudgetStop("MCP工具发现超出剩余主动时间预算") from exc
            raise RuntimeError("MCP工具发现超时") from exc

    async def _list_tools(self) -> list[dict[str, Any]]:
        async with Client(self.server) as client:
            budget = current_budget.get()
            result = await asyncio.wait_for(client.list_tools(), timeout=budget.remaining()) if budget else await client.list_tools()
            return [
                {
                    "name": tool.name,
                    "title": tool.title,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "output_schema": tool.output_schema,
                    "annotations": (
                        tool.annotations.model_dump(by_alias=True, exclude_none=True)
                        if tool.annotations
                        else {}
                    ),
                }
                for tool in result.tools
            ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from ..workbench_runtime import current_execution, check_execution
        check_execution('正在查询并核对经营数据')
        control = current_execution.get()
        if control and control.tools is not None and name not in control.tools:
            raise PermissionError('本次分析未启用该 MCP 工具：' + name)
        budget = current_budget.get()
        reservation = budget.begin_tool_call(name) if budget else None
        from ..workbench_runtime import emit_execution_event
        call_id = 'tool-'+uuid4().hex
        emit_execution_event({'type':'TOOL_CALL_START','toolCallId':call_id,'toolCallName':name,'parentMessageId':call_id+'-message'})
        emit_execution_event({'type':'TOOL_CALL_ARGS','toolCallId':call_id,'delta':json.dumps(arguments,ensure_ascii=False,default=str)})
        emit_execution_event({'type':'TOOL_CALL_END','toolCallId':call_id})
        try:
            result = asyncio.run(self._call_tool(name, arguments))
            emit_execution_event({'type':'TOOL_CALL_RESULT','toolCallId':call_id,'messageId':call_id+'-result',
                'content':json.dumps(result,ensure_ascii=False,default=str)})
            from ..business_progress import query_table
            table=query_table(result,call_id)
            if table:
                table['tool_call_id']=call_id
                emit_execution_event({'type':'CUSTOM','name':'artifact','value':table})
            return result
        except Exception as exc:
            emit_execution_event({'type':'TOOL_CALL_RESULT','toolCallId':call_id,'messageId':call_id+'-result',
                'content':json.dumps({'error':str(exc)},ensure_ascii=False)})
            if not isinstance(exc, TimeoutError):
                raise
            if budget:
                raise BudgetStop("MCP工具调用超出剩余主动时间预算") from exc
            raise RuntimeError("MCP工具调用超时") from exc
        finally:
            if budget and reservation:
                budget.finish_tool_call(reservation)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with Client(self.server) as client:
            budget = current_budget.get()
            result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=budget.remaining()) if budget else await client.call_tool(name, arguments)
        # 退出MCP会话后再抛出错误，避免异步任务组包装业务异常。
        if result.is_error:
            messages = [
                str(getattr(block, "text", ""))
                for block in result.content
                if getattr(block, "text", "")
            ]
            raise RuntimeError("; ".join(messages) or f"MCP工具调用失败：{name}")
        if result.structured_content is None:
            raise RuntimeError(f"MCP工具未返回结构化结果：{name}")
        return dict(result.structured_content)
