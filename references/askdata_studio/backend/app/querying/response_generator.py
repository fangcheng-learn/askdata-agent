from __future__ import annotations

from ..agent_configuration_runtime import effective_prompt
from ..prompt_defaults import RESULT_DRAFT_SYSTEM, SIMPLE_REVIEW_SYSTEM

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
        system = effective_prompt('result_draft', RESULT_DRAFT_SYSTEM)
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
                effective_prompt('simple_review', SIMPLE_REVIEW_SYSTEM) + method_text,
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
