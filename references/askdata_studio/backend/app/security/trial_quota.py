"""Durable trial limits. Every provider attempt requires a committed reservation.

Amounts are integer millionths of CNY. Unknown outcomes retain the full reserve;
failure to read/write Postgres never permits a request. The ledger is private and
contains no prompts, query results, IP addresses or credentials.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from uuid import uuid4

from . import public_demo

active_run: ContextVar[str | None] = ContextVar("trial_run", default=None)
DAILY_LIMIT = 10_000_000
MAX_OUTPUT = 16_384
# Conservative upper rates (CNY / million tokens), above the published regional
# maximums for these exact models, checked 2026-11-03. No promotional discounts.
RATES = {"qwen3.7-plus": (12, 48), "qwen3.7-flash": (6, 24),
         "text-embedding-v4": (2, 0), "qwen3-rerank": (2, 0)}


class TrialLimit(ValueError):
    pass


@contextmanager
def transaction():
    import psycopg
    dsn = os.environ.get("INSIGHTPILOT_QUOTA_DATABASE_URL", "")
    if not dsn:
        raise TrialLimit("试用额度服务尚未配置，暂时无法开始分析")
    try:
        with psycopg.connect(dsn, connect_timeout=8, sslmode="require",
                             options="-c statement_timeout=8000", prepare_threshold=None) as conn:
            yield conn
    except TrialLimit:
        raise
    except Exception:
        # Connection exceptions can contain credential-bearing connection strings.
        raise TrialLimit("试用额度服务暂不可用，请稍后重试") from None


def _day(conn):
    return conn.execute("SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date").fetchone()[0]


def begin_run(run_id: str, owner: str, *, count_analysis: bool = True) -> None:
    with transaction() as conn:
        gate = conn.execute("SELECT run_id, lease_until > now() FROM insightpilot_trial.gate WHERE id=1 FOR UPDATE").fetchone()
        if not gate:
            raise TrialLimit("试用额度服务尚未初始化")
        if gate[0] and gate[1]:
            raise TrialLimit("当前有访客正在分析，请稍后再试（不会扣减本次次数）")
        day = _day(conn)
        conn.execute("INSERT INTO insightpilot_trial.daily(day) VALUES (%s) ON CONFLICT DO NOTHING", (day,))
        used, halted = conn.execute("SELECT reserved_microyuan,halted FROM insightpilot_trial.daily WHERE day=%s FOR UPDATE", (day,)).fetchone()
        if halted or used >= DAILY_LIMIT:
            raise TrialLimit("今日全站试用额度已用完，请明天再来")
        conn.execute("INSERT INTO insightpilot_trial.visitors(day,visitor_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (day, owner))
        count = conn.execute("SELECT starts FROM insightpilot_trial.visitors WHERE day=%s AND visitor_id=%s FOR UPDATE", (day, owner)).fetchone()[0]
        if count_analysis and count >= 2:
            raise TrialLimit("你今天的 2 次试用已用完，请明天再来")
        if count_analysis:
            conn.execute("UPDATE insightpilot_trial.visitors SET starts=starts+1 WHERE day=%s AND visitor_id=%s", (day, owner))
        conn.execute("UPDATE insightpilot_trial.gate SET run_id=%s, lease_until=now()+interval '15 minutes' WHERE id=1", (run_id,))


def finish_run(run_id: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE insightpilot_trial.gate SET run_id=NULL, lease_until=NULL WHERE id=1 AND run_id=%s", (run_id,))


def prepare(payload: dict, kind: str, url: str) -> dict:
    expected = {"chat": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                "embedding": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
                "rerank": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"}
    allowed = {"chat": {"qwen3.7-plus", "qwen3.7-flash"},
               "embedding": {"text-embedding-v4"}, "rerank": {"qwen3-rerank"}}
    if url != expected.get(kind) or payload.get("model") not in allowed.get(kind, set()):
        raise TrialLimit("该模型或接口尚未配置试用计费规则")
    result = dict(payload)
    if kind == "chat":
        result.update(max_tokens=MAX_OUTPUT, enable_thinking=False)
    return result


def upper_cost(payload: dict, kind: str) -> tuple[int, int, int]:
    # UTF-8 bytes bound text-token counts; the margin covers chat templates/schema.
    byte_count = len(json.dumps(payload, ensure_ascii=False).encode())
    if kind == "rerank":
        # Providers bill the query once per document (including chunked documents).
        query_bytes = len(str(payload.get("query", "")).encode())
        docs = payload.get("documents", [])
        byte_count += query_bytes * sum(1 + len(str(d).encode()) // 1000 for d in docs)
    tokens = byte_count + 4096
    if tokens > 262_144:
        raise TrialLimit("本次上下文超过公开试用范围，请缩小问题范围")
    input_rate, output_rate = RATES[payload["model"]]
    return tokens * input_rate + (MAX_OUTPUT * output_rate if kind == "chat" else 0), input_rate, output_rate


def reserve(payload: dict, kind: str) -> tuple[str, int, int]:
    run_id = active_run.get()
    if not run_id:
        raise TrialLimit("请从试用工作台发起分析")
    amount, in_rate, out_rate = upper_cost(payload, kind)
    reservation = uuid4().hex
    with transaction() as conn:
        gate = conn.execute("SELECT run_id, lease_until > now() FROM insightpilot_trial.gate WHERE id=1 FOR UPDATE").fetchone()
        if not gate or gate[0] != run_id or not gate[1]:
            raise TrialLimit("本次试用已结束，请重新发起分析")
        day = _day(conn)
        conn.execute("INSERT INTO insightpilot_trial.daily(day) VALUES (%s) ON CONFLICT DO NOTHING", (day,))
        updated = conn.execute("UPDATE insightpilot_trial.daily SET reserved_microyuan=reserved_microyuan+%s WHERE day=%s AND NOT halted AND reserved_microyuan+%s<=%s RETURNING day", (amount, day, amount, DAILY_LIMIT)).fetchone()
        if not updated:
            raise TrialLimit("今日剩余额度不足以继续分析，已有结果已保留")
        conn.execute("INSERT INTO insightpilot_trial.reservations(id,day,run_id,amount) VALUES (%s,%s,%s,%s)", (reservation, day, run_id, amount))
        conn.execute("UPDATE insightpilot_trial.gate SET lease_until=now()+interval '15 minutes' WHERE id=1 AND run_id=%s", (run_id,))
    return reservation, in_rate, out_rate


def settle(ticket: tuple[str, int, int], usage: dict | None) -> None:
    if not isinstance(usage, dict):
        return  # Unknown usage keeps the entire pre-committed reservation.
    incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
    outgoing = usage.get("completion_tokens", usage.get("output_tokens", 0 if ticket[2] == 0 else None))
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (incoming, outgoing)):
        return
    actual = incoming * ticket[1] + outgoing * ticket[2]
    with transaction() as conn:
        row = conn.execute("SELECT day,amount,settled FROM insightpilot_trial.reservations WHERE id=%s FOR UPDATE", (ticket[0],)).fetchone()
        if not row or row[2]:
            return
        day, reserved, _ = row
        if actual > reserved:
            # Unexpected provider accounting: record all cost and close today's gate.
            conn.execute("UPDATE insightpilot_trial.daily SET halted=true,reserved_microyuan=reserved_microyuan+%s WHERE day=%s", (actual-reserved, day))
        else:
            conn.execute("UPDATE insightpilot_trial.daily SET reserved_microyuan=reserved_microyuan-%s WHERE day=%s", (reserved-actual, day))
        conn.execute("UPDATE insightpilot_trial.reservations SET amount=%s,settled=true WHERE id=%s", (actual, ticket[0]))
