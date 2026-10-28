from __future__ import annotations

from typing import Any
import json

from ..runtime_budget import current_budget, BudgetStop

from ..config import Settings, settings
from ..errors import PipelineStageError
from ..model_client import ModelClient
from .models import SqlExecution


class ResponseGenerator:
    """生成问答内容和查询结果说明。"""

    def __init__(
        self,
        model_client: ModelClient,
        config: Settings | None = None,
    ) -> None:
        self.model_client = model_client
        self.table_row_limit = max(1, (config or settings).context_table_row_limit)

    def finalize(
        self,
        query: str,
        execution: SqlExecution,
        schema_context: str,
        analysis_context: str,
        business_methods: dict[str, str] | None = None,
        query_validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        system = """你是查询结果整理器。检查结果能否回答问题，并生成简短标题和一到两句说明。
只能使用结果中真实存在的数值。只返回JSON：
{"valid":true,"reason":"...","title":"...","analysis":"..."}。"""
        method_text = "\n当前选中的业务Skill规则：\n" + "\n".join((business_methods or {}).values())
        system += method_text
        user = (
            f"问题：{query}\nSQL：{execution.sql}\n列：{execution.columns}\n"
            f"结果数据：{execution.rows[: self.table_row_limit]}\nSchema：{schema_context}\n"
            f"用户保存的分析表格：{analysis_context or '无'}"
            f"\nSQL实际行为与待核对项：{json.dumps(query_validation or {}, ensure_ascii=False)}"
        )
        budget = current_budget.get()
        if budget:
            budget.model_node = "result_draft"
            for name, instructions in (business_methods or {}).items():
                budget.skill(name, instructions, budget.model_node)
        try:
            payload = self.model_client.chat_json(system, user)
            budget = current_budget.get()
            if budget:
                budget.phase = "closing"
                budget.model_node = "simple_delivery_review"
                for name, instructions in (business_methods or {}).items():
                    budget.skill(name, instructions, budget.model_node)
            review = self.model_client.chat_json(
                "你是查询结果整理器的独立交付审核节点。检查草稿与真实证据的指标、事件时间、单位、联结粒度和关键数字。"
                "只输出JSON {valid:boolean,reason:string,title:string,analysis:string}。直接提供可展示的审核定稿。"
                "数据不全不能按预览求全集；不得夸大因果。query_validation中的pending_checks须结合SQL与数据核对，"
                "规范化不代表业务正确；可解释的等价表达无需强行返工，未解决的实质错误不能放行。尚未回答完整时valid=false并说明限制。" + method_text,
                json.dumps({"question": query, "sql": execution.sql, "schema": schema_context,
                    "rows": execution.rows[:self.table_row_limit], "total_rows": execution.total_rows,
                    "complete": execution.complete, "review_input_truncated": len(execution.rows)>self.table_row_limit or execution.preview_truncated,
                    "result_ref": execution.result_ref, "query_validation": query_validation or {},
                    "draft": payload}, ensure_ascii=False))
            payload = review
            return {
                "valid": bool(payload.get("valid", True)),
                "reason": str(payload.get("reason") or "结果检查通过"),
                "title": str(payload.get("title") or "查询结果"),
                "final_reviewed": bool(payload.get("valid", False)),
                "analysis": str(payload.get("analysis") or f"查询返回{len(execution.rows)}行。"),
            }
        except BudgetStop:
            raise
        except RuntimeError as exc:
            raise PipelineStageError("result_analysis", str(exc)) from exc
