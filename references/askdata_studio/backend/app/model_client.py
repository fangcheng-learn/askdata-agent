from __future__ import annotations

import json
from http.client import HTTPException
import math
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Settings, settings
from .runtime_budget import current_budget, BudgetStop


class ModelClient:
    """Chat、Embedding 和 Rerank 接口客户端。"""

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        self._blocked_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    def chat(self, system: str, user: str, *, temperature: float | None = None, json_mode: bool = False) -> str:
        if json_mode and "json" not in (system + user).casefold():
            system += "\n请只返回JSON对象。"
        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": False,
            "enable_thinking": self.config.llm_enable_thinking,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        result = self._post(self.config.chat_url, payload)
        try:
            return str(result["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("大模型响应缺少 message.content") from exc

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user, json_mode=True)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if not match:
                raise RuntimeError("大模型未返回有效 JSON")
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise RuntimeError("大模型未返回有效 JSON") from exc

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
        if not self.config.api_key:
            raise RuntimeError("LLM_API_KEY 尚未配置")
        if time.monotonic() < self._blocked_until.get(url, 0):
            raise RuntimeError("模型接口暂时处于30秒熔断窗口，请稍后重试")
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
        for attempt in range(self.config.max_retries + 1):
            budget = current_budget.get()
            if budget:
                budget.check()
                if attempt:
                    budget.repair("模型网络重试")
                kind = "chat" if url == self.config.chat_url else "embedding" if url == self.config.embeddings_url else "rerank"
                budget.model_calls[kind] = budget.model_calls.get(kind, 0) + 1
            audit = None
            if budget:
                audit = {"kind": kind, "node": budget.model_node, "step_id": budget.step,
                    "model": payload.get("model"), "attempt": attempt + 1,
                    "request_characters": len(json.dumps({key: payload[key] for key in ("messages", "input", "query", "documents") if key in payload}, ensure_ascii=False)),
                    "status": "started", "usage": None}
                budget.model_usage.append(audit)
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=min(self.config.timeout, max(0.05, budget.remaining())) if budget else self.config.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                    if audit is not None:
                        usage = result.get("usage")
                        # Persist only numeric accounting fields, never prompts,
                        # headers, keys, request bodies or server response text.
                        def numeric_usage(value):
                            if not isinstance(value, dict):
                                return None
                            return {str(key): numeric_usage(child) if isinstance(child, dict) else child
                                    for key, child in value.items()
                                    if isinstance(child, (int, float, dict)) and not isinstance(child, bool)}
                        audit.update(status="success", usage=numeric_usage(usage))
                    if budget:
                        budget.check()
                    return result
            except urllib.error.HTTPError as exc:
                if audit is not None:
                    audit.update(status="http_error", http_status=exc.code)
                # 保留上游完整响应体，便于复现 403 等非重试性错误。
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(
                    f"模型接口返回 HTTP {exc.code} ({exc.reason}): {detail}"
                )
                retryable_failure = exc.code >= 500 or exc.code == 429
                if not retryable_failure:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, HTTPException, json.JSONDecodeError) as exc:
                if audit is not None:
                    audit.update(status="transport_error", error_type=type(exc).__name__)
                if budget and budget.remaining() <= 0:
                    raise BudgetStop("模型调用超出剩余主动时间预算") from exc
                last_error = exc
                retryable_failure = True
            finally:
                if audit is not None:
                    audit["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if attempt < self.config.max_retries:
                time.sleep(min(0.4 * (2**attempt), budget.remaining()) if budget else 0.4 * (2**attempt))
        # 只有网络故障和可重试的上游错误进入熔断；账户/权限错误不会自行恢复。
        if retryable_failure:
            self._blocked_until[url] = time.monotonic() + 30
        raise RuntimeError(f"模型接口调用失败: {last_error}") from last_error

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [float(value) / norm for value in vector]
