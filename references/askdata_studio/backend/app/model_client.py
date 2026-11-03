from __future__ import annotations

import json
from http.client import HTTPException
import math
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Settings, settings
from .runtime_budget import current_budget, BudgetStop
from .errors import ModelOutputError
from .structured_output import current_output_schema, schema_response_format
from .model_trace import ModelTrace


class ModelRequestNotSent(RuntimeError):
    request_sent = False


class ModelClient:
    """Chat、Embedding 和 Rerank 接口客户端。"""

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        self._blocked_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    def chat(self, system: str, user: str, *, temperature: float | None = None, json_mode: bool = False) -> str:
        from .workbench_runtime import current_execution
        control = current_execution.get()
        if json_mode and "json" not in (system + user).casefold():
            system += "\n请只返回JSON对象。"
        payload = {
            "model": control.model if control and control.model and not (current_budget.get() and current_budget.get().model_node == "preprocess") else self.config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": getattr(self.config, 'llm_stream', False),
            "enable_thinking": self.config.llm_enable_thinking,
        }
        if payload['stream']:
            payload['stream_options'] = {'include_usage': True}
        if json_mode:
            payload["response_format"] = schema_response_format(
                enabled=getattr(self.config, 'structured_output_enabled', True))
        # Token accounting is observational. Do not turn a task's historical
        # max_output_tokens into a provider output cap; native provider limits
        # still apply and are reported as real interface failures when hit.
        result = self._post(self.config.chat_url, payload)
        try:
            return str(result["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("大模型响应缺少 message.content") from exc

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user, json_mode=True)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if not match:
                raise ModelOutputError("大模型未返回有效 JSON")
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise ModelOutputError("大模型未返回有效 JSON") from exc
        if not isinstance(result, dict):
            raise ModelOutputError("大模型JSON必须为对象")
        return result

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 10):
            payload = {
                "model": self.config.embedding_model,
                "input": texts[start : start + 10],
                "dimensions": self.config.embedding_dimensions,
                "encoding_format": "float",
            }
            result = self._post(self.config.embeddings_url, payload)
            items = sorted(result.get("data", []), key=lambda item: item.get("index", 0))
            vectors.extend(self._normalize(item["embedding"]) for item in items)
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding 返回数量与输入不一致")
        return vectors

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        payload = {
            "model": self.config.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "instruct": "Rank database schema fields by relevance to the user's analytics query.",
        }
        result = self._post(self.config.rerank_url, payload)
        output = [
            (int(item["index"]), float(item.get("relevance_score", item.get("score", 0))))
            for item in result.get("results", [])
        ]
        return sorted(output, key=lambda item: item[1], reverse=True)

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        from .security import public_demo
        from .security import trial_quota
        if public_demo.enabled():
            kind = "chat" if url == self.config.chat_url else "embedding" if url == self.config.embeddings_url else "rerank"
            payload = trial_quota.prepare(payload, kind, url)
        from .workbench_runtime import check_execution
        node = current_budget.get().model_node if current_budget.get() else 'entry'
        labels = {'preprocess': '正在理解问题', 'entry': '正在理解问题', 'react': '正在分析经营变化',
                  'query_coder': '正在准备数据查询', 'sql_repair': '正在核对查询',
                  'draft': '正在整理分析结论', 'delivery_review': '正在核对结论与证据'}
        stage = labels.get(node, '正在分析经营数据') if url == self.config.chat_url else '正在查找指标口径与数据'
        check_execution(stage)
        if not self.config.api_key:
            raise RuntimeError("LLM_API_KEY 尚未配置")
        if time.monotonic() < self._blocked_until.get(url, 0):
            raise ModelRequestNotSent("模型接口暂时处于30秒熔断窗口，请稍后重试")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_error: Exception | None = None
        retryable_failure = False
        endpoint_failure = False
        task_budget = current_budget.get()
        # A ReAct/SQL generation slot authorizes one request only. Unknown
        # network responses must not silently spend another generation.
        max_retries = (0 if task_budget and task_budget.model_node in {'react', 'sql_repair', 'query_coder'}
                       else self.config.max_retries)
        for attempt in range(max_retries + 1):
            check_execution()
            budget = current_budget.get()
            reservation = None
            request_timeout = self.config.timeout
            kind = "chat" if url == self.config.chat_url else "embedding" if url == self.config.embeddings_url else "rerank"
            trace = None
            if budget:
                budget.check()
            if getattr(self.config, 'model_trace_dir', ''):
                try:
                    trace = ModelTrace(self.config.model_trace_dir, payload,
                        {'task_id': budget.task_id if budget else None, 'kind': kind,
                         'node': budget.model_node if budget else None,
                         'step_id': budget.step if budget else None, 'attempt': attempt + 1,
                         'model': payload.get('model')}, self.config.api_key)
                except OSError as exc:
                    raise ModelRequestNotSent('模型输入记录未能写入，未发送本次请求') from exc
            if budget:
                try:
                    if attempt:
                        budget.repair("模型网络重试")
                    reservation = budget.reserve_model(kind, payload, timeout_seconds=request_timeout)
                except BaseException as exc:
                    if trace:
                        trace.finish(status='not_sent', request_attempted=False, response_body=None,
                            response_received=False, elapsed_seconds=0, error_type=type(exc).__name__)
                    raise
                request_timeout = budget.model_reservations[reservation]['seconds_reserved']
                budget.model_calls[kind] = budget.model_calls.get(kind, 0) + 1
            audit = None
            if budget:
                audit = {"kind": kind, "node": budget.model_node, "step_id": budget.step,
                    "model": payload.get("model"), "attempt": attempt + 1,
                    "request_characters": len(json.dumps({key: payload[key] for key in ("messages", "input", "query", "documents", "response_format") if key in payload}, ensure_ascii=False)),
                    "status": "started", "usage": None, 'timeout_seconds': request_timeout,
                    'input_blocks': self._input_block_sizes(payload),
                    'input_estimate_tokens': budget.model_reservations[reservation]['input_estimate'],
                    'input_reservation_upper_bound': budget.model_reservations[reservation]['input_upper_bound'],
                    'input_estimator': budget.model_reservations[reservation]['input_estimator'],
                    'input_estimator_fallback': budget.model_reservations[reservation]['input_estimator_fallback']}
                if payload.get('response_format'):
                    selected = current_output_schema.get() or {}
                    audit.update(response_format=payload['response_format']['type'],
                                 output_schema_name=selected.get('name'), output_schema_sha256=selected.get('sha256'))
                budget.model_usage.append(audit)
                if trace:
                    audit.update(trace_id=trace.trace_id, trace_complete=False)
                budget.checkpoint()
            started = time.monotonic()
            response_body = None
            response_received = False
            request_attempted = False
            trace_status = 'not_sent'
            trace_http_status = None
            trace_error_type = None
            trial_ticket = None
            try:
                if budget:
                    budget.check()
                    request_timeout = min(request_timeout, budget.remaining())
                    audit['timeout_seconds'] = request_timeout
                if public_demo.enabled():
                    trial_ticket = trial_quota.reserve(payload, kind)
                request_attempted = True
                trace_status = 'started'
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    if payload.get('stream'):
                        from .model_stream import ChatStream
                        stream = ChatStream()
                        try:
                            result = stream.read(response, deadline=started + request_timeout,
                                check=lambda: (check_execution(), budget.check() if budget else None))
                        finally:
                            response_body = b''.join(stream.raw).decode('utf-8', errors='replace')
                            response_received = stream.done
                            if audit is not None and stream.usage is not None:
                                audit['usage'] = stream.usage
                    else:
                        raw_response = response.read()
                        response_received = True
                        response_body = raw_response.decode('utf-8', errors='replace')
                        result = json.loads(raw_response.decode("utf-8"))
                    trace_status = 'success'
                    if trial_ticket:
                        trial_quota.settle(trial_ticket, result.get("usage"))
                    if audit is not None:
                        usage = result.get("usage")
                        # Public accounting stays numeric. Opt-in full bodies
                        # are stored separately in the private trace directory.
                        def numeric_usage(value):
                            if not isinstance(value, dict):
                                return None
                            return {str(key): numeric_usage(child) if isinstance(child, dict) else child
                                    for key, child in value.items()
                                    if isinstance(child, (int, float, dict)) and not isinstance(child, bool)}
                        audit.update(status="success", usage=numeric_usage(usage))
                    if budget:
                        budget.settle_model(reservation, audit.get('usage') if audit else None)
                        budget.check()
                    return result
            except urllib.error.HTTPError as exc:
                trace_status, trace_http_status = 'http_error', exc.code
                if audit is not None:
                    audit.update(status="http_error", http_status=exc.code)
                # 保留上游完整响应体，便于复现 403 等非重试性错误。
                detail = exc.read().decode("utf-8", errors="replace")
                response_body, response_received = detail, True
                last_error = RuntimeError(
                    f"模型接口返回 HTTP {exc.code} ({exc.reason}): {detail}"
                )
                retryable_failure = exc.code >= 500 or exc.code == 429
                endpoint_failure = retryable_failure
                if not retryable_failure:
                    break
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, HTTPException, json.JSONDecodeError) as exc:
                trace_status, trace_error_type = 'transport_error', type(exc).__name__
                if isinstance(getattr(exc, 'partial', None), bytes):
                    response_body = exc.partial.decode('utf-8', errors='replace')
                if audit is not None:
                    audit.update(status="transport_error", error_type=type(exc).__name__)
                if budget and budget.remaining() <= 0:
                    raise BudgetStop("模型调用超出剩余主动时间预算") from exc
                last_error = exc
                retryable_failure = True
                # One request exhausting its read deadline is not evidence
                # that this shared endpoint is unavailable for other tasks.
                # Preserve this request's failure/usage; do not silently retry.
                reason = getattr(exc, 'reason', exc)
                timed_out = isinstance(reason, (TimeoutError, socket.timeout))
                endpoint_failure = (not timed_out
                                    and not isinstance(exc, json.JSONDecodeError))
                if timed_out or payload.get('stream'):
                    # The provider may still have processed this request. This
                    # applies to every role, including draft/QA/review and calls
                    # without a task budget; finally records unknown usage.
                    break
            finally:
                trace_failure = None
                if trace:
                    pending = sys.exc_info()[1]
                    try:
                        trace.finish(status=trace_status if not pending else ('not_sent' if not request_attempted else 'interrupted'),
                            request_attempted=request_attempted, response_body=response_body,
                            response_received=response_received,
                            elapsed_seconds=round(time.monotonic() - started, 3),
                            http_status=trace_http_status, error_type=trace_error_type or (type(pending).__name__ if pending else None))
                        if audit is not None:
                            audit['trace_complete'] = True
                    except OSError as exc:
                        trace_failure = exc
                        if audit is not None:
                            audit.update(trace_complete=False, trace_error_type=type(exc).__name__)
                if audit is not None:
                    audit["elapsed_seconds"] = round(time.monotonic() - started, 3)
                if budget and reservation:
                    budget.settle_model(reservation, audit.get('usage') if audit else None)
                    budget.checkpoint()
                if trace_failure:
                    raise RuntimeError('模型调用记录写入失败；保留本次调用状态，不自动重放') from trace_failure
            if attempt < max_retries:
                time.sleep(min(0.4 * (2**attempt), budget.remaining()) if budget else 0.4 * (2**attempt))
        # 只有网络故障和可重试的上游错误进入熔断；账户/权限错误不会自行恢复。
        if retryable_failure and endpoint_failure:
            self._blocked_until[url] = time.monotonic() + 30
        raise RuntimeError(f"模型接口调用失败: {last_error}") from last_error

    @staticmethod
    def _input_block_sizes(payload: dict[str, Any]) -> dict[str, Any]:
        """Sizes only: never archive prompt text, credentials, or raw headers."""
        blocks: dict[str, Any] = {}
        def add(key: str, value: Any) -> None:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            blocks[key] = {'characters': len(text), 'utf8_token_upper_bound': len(text.encode('utf-8')),
                           'token_count_is_estimate': True}
        for message in payload.get('messages', []):
            content = message.get('content', '')
            role = str(message.get('role') or 'message')
            try:
                parsed = json.loads(content) if role == 'user' else None
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    add(f'{role}.{key}', value)
            else:
                add(role, content)
        for key in ('input', 'query', 'documents', 'response_format'):
            if key in payload:
                add(key, payload[key])
        return blocks

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [float(value) / norm for value in vector]
