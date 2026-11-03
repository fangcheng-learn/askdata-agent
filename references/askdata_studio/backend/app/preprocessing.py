from __future__ import annotations

from .agent_configuration_runtime import effective_prompt
from .prompt_defaults import PREPROCESS_SYSTEM

import logging
import re
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .errors import PipelineStageError
from .model_client import ModelClient
from .runtime_budget import BudgetStop, current_budget
from .structured_output import using_output_schema
from .preprocessing_output import PreparedOutput, RouteName, ResponseType


logger = logging.getLogger(__name__)


@dataclass
class RetrievalIntent:
    """Schema 检索参数。"""

    retrieval_terms: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    time_expressions: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreparedRequest:
    """请求预处理结果。"""

    action: RouteName
    confidence: float
    reason: str
    standalone_query: str = ""
    rewritten: bool = False
    retrieval: RetrievalIntent = field(default_factory=RetrievalIntent)
    response: str = ""
    response_type: ResponseType = "answer"
    source: str = "model"
    needs_planner: bool = False
    business_skills: list[str] = field(default_factory=list)
    required_questions: list[str] = field(default_factory=list)
    task_understanding: dict[str, Any] = field(default_factory=dict)
    analysis_task_ids: list[str] = field(default_factory=list)
    analysis_result_ids: list[str] = field(default_factory=list)
    analysis_mode: str = "explanation"


class RequestPreprocessor:
    """一次调用完成上下文聚合、意图判断、检索词提取或直接回复。"""

    def __init__(self, model_client: ModelClient) -> None:
        self.model_client = model_client

    def prepare(self, query: str, recent_context: str) -> PreparedRequest:
        budget = current_budget.get()
        if budget:
            budget.model_node = "preprocess"
        system = effective_prompt('preprocess', PREPROCESS_SYSTEM)
        user = f"当前问题：{query}\n近期轻量上下文：{recent_context or '无'}"
        try:
            with using_output_schema('entry_understanding', self._output_schema(recent_context)):
                payload = self.model_client.chat_json(system, user)
        except BudgetStop:
            raise
        except RuntimeError as exc:
            # 预处理失败时终止下游查询，避免错误路由访问数据库。
            logger.warning(
                "route_fallback_used stage=request_preprocessing action=direct_response error=%s",
                exc,
            )
            return PreparedRequest(
                action="direct_response",
                confidence=0.0,
                reason=f"预处理模型不可用，已启用保守兜底：{exc}",
                response="大模型服务暂时不可用，请稍后重试。",
                source="model_unavailable_fallback",
            )
        try:
            return self._parse(payload, query, recent_context)
        except (ValueError, KeyError, TypeError) as exc:
            raise PipelineStageError("request_preprocessing", str(exc)) from exc

    @staticmethod
    def _output_schema(recent_context: str) -> dict:
        schema = PreparedOutput.model_json_schema()
        for key in ('analysis_task_ids', 'analysis_result_ids'):
            schema['properties'][key]['maxItems'] = 0
        # route_context is serialized server JSON after the time/source
        # preamble. Values inside user text remain JSON-escaped, never parsed
        # as new reference candidates.
        start = recent_context.find('{"recent_turns"')
        if start < 0:
            return schema
        try:
            catalog, _ = json.JSONDecoder().raw_decode(recent_context[start:])
        except (ValueError, TypeError):
            return schema
        headers = [*(catalog.get('recent_turns') or []), *(catalog.get('selected_table_headers') or [])]
        task_ids = list(dict.fromkeys(str(h.get('turn_id') or h.get('task_id')) for h in headers
                                     if h.get('turn_id') or h.get('task_id')))
        result_ids = list(dict.fromkeys(str(e['result_id']) for h in headers
                          for e in h.get('available_evidence') or [] if e.get('result_id')))
        for key, choices in [('analysis_task_ids', task_ids), ('analysis_result_ids', result_ids)]:
            if choices:
                schema['properties'][key].pop('maxItems', None)
                schema['properties'][key]['items']['enum'] = choices
        return schema

    def _parse(self, payload: dict[str, Any], original_query: str, recent_context: str = "") -> PreparedRequest:
        action = str(payload["action"])
        if action not in {"data_qa", "database_query", "direct_response", "metadata_query"}:
            raise ValueError(f"不支持的路由结果：{action}")

        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.8))))
        reason = str(payload.get("reason") or "模型完成请求预处理")

        common = {
            "analysis_mode": payload.get("analysis_mode") if payload.get("analysis_mode") in {"none", "fact", "explanation", "report"} else "explanation",
            'analysis_task_ids': list(dict.fromkeys(self._strings(payload.get('analysis_task_ids'))))[:5],
            'analysis_result_ids': list(dict.fromkeys(self._strings(payload.get('analysis_result_ids')))),
            "needs_planner": bool(payload.get("needs_investigation", payload.get("needs_planner", False))),
            "business_skills": [name for name in self._strings(payload.get("business_skills"))
                                if name in {"business_diagnosis", "goal_analysis", "inventory_risk_check"}],
            "required_questions": list(dict.fromkeys(self._strings(payload.get("required_questions")) or [original_query])),
            "task_understanding": payload.get('task_understanding') if isinstance(payload.get('task_understanding'), dict) else {},
        }
        if payload.get('response_type') == 'clarification':
            # A declared question to the user is an unfinished task, even if
            # the router also emitted needs_investigation=false. Let the
            # existing main agent decide/ask the necessary clarification and
            # persist the same task/budget. Do not infer a missing metric or
            # period from wording, and never mark a free-text question complete.
            common['needs_planner'] = True
            reason += '；入口声明仍需澄清，交主Agent保存原任务并确认必要条件'
        if (action == 'direct_response' and payload.get('response_type', 'answer') == 'answer'
                and (common['analysis_task_ids'] or common['analysis_result_ids']
                     or payload.get('analysis_mode') in {'fact', 'explanation', 'report'})):
            # A declared dependency on real results belongs to the evidence
            # reader, never to the router's free-form answer channel.
            action = 'data_qa'
            reason += '；已声明依赖结果，交由DataQa读取实际证据'
        if common['needs_planner']:
            # Routing is not authority to replace a user's question with a
            # causal narrative. Parsed scope/time can inherit context, while
            # the investigation's goal remains the verbatim user request.
            understanding = dict(common['task_understanding'])
            understanding.update(goal=original_query, questions=[original_query],
                                 user_statement={'quote': original_query, 'source': 'current_user_message',
                                                 'status': 'unverified'})
            grounded = []
            for finding in understanding.get('known_findings') or []:
                quote = (finding.get('quote') if isinstance(finding, dict) else finding)
                if not isinstance(quote, str) or not quote.strip():
                    continue
                quote = quote.strip()
                source = ('current_user_message' if quote in original_query else
                          'recent_context' if quote in recent_context else None)
                if source and not any(item['quote'] == quote and item['source'] == source for item in grounded):
                    grounded.append({'quote': quote, 'source': source, 'status': 'unverified'})
            understanding['known_findings'] = grounded
            common['task_understanding'] = understanding
            common['required_questions'] = [original_query]
        if action == 'metadata_query':
            return PreparedRequest(action=action, confidence=confidence, reason=reason, standalone_query=original_query)
        if action == "data_qa":
            return PreparedRequest(action="data_qa", confidence=confidence, reason=reason,
                standalone_query=original_query if common["needs_planner"] else "", **common)

        if action == "direct_response":
            response = str(payload.get("response") or "").strip()
            if not response:
                raise ValueError("direct_response缺少response")
            response_type = str(payload.get("response_type") or "answer")
            if response_type not in {"answer", "clarification"}:
                raise ValueError(f"不支持的直接回复类型：{response_type}")
            return PreparedRequest(
                **common,
                action="direct_response",
                confidence=confidence,
                reason=reason,
                response=response,
                response_type=response_type,
            )

        standalone = str(payload.get("standalone_query") or original_query).strip()[:800]
        if common['needs_planner']:
            standalone = original_query
        raw = payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {}
        retrieval = RetrievalIntent(
            retrieval_terms=self._strings(raw.get("retrieval_terms")),
            metrics=self._strings(raw.get("metrics")),
            dimensions=self._strings(raw.get("dimensions")),
            filters=self._strings(raw.get("filters")),
            time_expressions=self._strings(raw.get("time_expressions")),
            operations=self._strings(raw.get("operations")),
        )
        return PreparedRequest(
            **common,
            action="database_query",
            confidence=confidence,
            reason=reason,
            standalone_query=standalone,
            rewritten=False if common['needs_planner'] else bool(payload.get("rewritten", standalone != original_query.strip())),
            retrieval=retrieval,
        )

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
