"""Per-task active-time and shared retry limits; never charge time waiting for a user."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import math
import time
import uuid
from typing import Any


class BudgetStop(RuntimeError):
    pass


@dataclass
class RunBudget:
    task_id: str
    tool_limit: int = 20
    repair_limit: int = 2
    replan_limit: int = 1
    seconds: float = 480
    investigation_seconds: float = 420
    spent: float = 0
    tools: int = 0
    replans: int = 0
    repairs: dict[str, int] = field(default_factory=dict)
    model_calls: dict[str, int] = field(default_factory=dict)
    model_usage: list[dict[str, Any]] = field(default_factory=list)
    model_node: str = "entry"
    events: list[dict[str, Any]] = field(default_factory=list)
    phase: str = 'investigation'
    step: str = 'entry'
    started: float | None = None
    reference_now: str = ''
    # Legacy fields are readable for old checkpoints; they no longer gate calls.
    model_token_limit: int | None = None
    retrieval_token_limit: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    closing_token_reserve: int | None = None
    react_round_limit: int = 8
    react_rounds: int = 0
    react_decisions: int = 0
    react_round_policy: str = 'query_batches'
    sql_generation_limit: int = 3
    direct_sql_generations: dict[str, int] = field(default_factory=dict)
    model_tokens: dict[str, int] = field(default_factory=dict)
    model_estimated_tokens: dict[str, int] = field(default_factory=dict)
    token_accounting_version: int = 2
    model_reservations: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_input_estimator: dict[str, Any] = field(default_factory=dict)
    checkpoint_callback: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.react_round_limit) is not int or not 1 <= self.react_round_limit <= 8:
            raise ValueError('业务取证轮次上限必须为1至8的整数')
        if type(self.sql_generation_limit) is not int or not 1 <= self.sql_generation_limit <= 3:
            raise ValueError('每条SQL生成上限必须为1至3，包含首次生成')

    def checkpoint(self) -> None:
        if self.checkpoint_callback:
            self.checkpoint_callback()

    @staticmethod
    def estimate_tokens(payload: Any) -> int:
        # Keep a byte upper bound as diagnostic metadata, never as admission.
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')) + 64

    @staticmethod
    def estimate_input_tokens(payload: Any) -> int:
        from .token_counting import estimate_input_tokens
        return estimate_input_tokens(payload)

    def reserve_model(self, kind: str, payload: dict[str, Any], *, timeout_seconds: float = 60.0) -> str:
        """Track a pending input estimate; token amounts never authorize/refuse it."""
        self.check()
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('模型传输超时必须为有限正数')
        text = {k: payload[k] for k in ('messages', 'input', 'query', 'documents', 'response_format') if k in payload}
        upper_bound = self.estimate_tokens(text)
        from .token_counting import input_token_details
        details = input_token_details(text)
        self.last_input_estimator = details
        estimated = int(details['estimated_count'])
        if details.get('fallback'):
            self.record('token_estimator_fallback', reason=details.get('fallback_reason'),
                        estimator=details['estimator'])
        group = 'chat' if kind == 'chat' else 'retrieval'
        key = uuid.uuid4().hex
        self.model_reservations[key] = {'group': group, 'tokens': estimated,
            'input_upper_bound': upper_bound, 'input_estimate': estimated,
            'input_estimator': details['estimator'], 'input_estimator_fallback': details.get('fallback', False),
            'estimate_scope': 'input_only_output_unknown',
            'kind': kind, 'seconds_reserved': min(timeout_seconds, self.remaining())}
        self.checkpoint()
        return key

    @staticmethod
    def _actual_tokens(usage: dict[str, Any] | None) -> int | None:
        if not isinstance(usage, dict):
            return None
        def valid(value: Any) -> bool:
            return (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0)
        total = usage.get('total_tokens')
        if valid(total):
            return int(total)
        values = [usage.get('prompt_tokens', usage.get('input_tokens')),
                  usage.get('completion_tokens', usage.get('output_tokens', 0))]
        return int(sum(values)) if all(valid(value) for value in values) else None

    def settle_model(self, key: str, usage: dict[str, Any] | None) -> None:
        item = self.model_reservations.pop(key, None)
        if not item:
            return
        total = self._actual_tokens(usage)
        group = item['group']
        if total is None:
            # An input estimate is not evidence of billed output or actual use.
            estimated = int(item.get('input_estimate', item.get('tokens', 0)))
            self.model_estimated_tokens[group] = self.model_estimated_tokens.get(group, 0) + max(0, estimated)
            self.record('model_usage_unknown', kind=item['kind'], estimated_input_tokens=estimated,
                        actual_usage_known=False, output_tokens_known=False)
        else:
            self.model_tokens[group] = self.model_tokens.get(group, 0) + total
        self.checkpoint()

    def model_budget(self) -> dict[str, Any]:
        return {'mode': 'observe_only', 'enforced': False, 'token_limits_enforced': False,
            'used': dict(self.model_tokens), 'estimated_unknown': dict(self.model_estimated_tokens),
            'unknown_estimate_scope': 'input_only_output_unknown',
            'limit': None, 'retrieval_limit': None, 'max_input': None,
            'max_output': None, 'closing_reserve': None,
            'reserved': sum(x['tokens'] for x in self.model_reservations.values()),
            'pending_estimated_input': sum(x['tokens'] for x in self.model_reservations.values()),
            'remaining': None,
            'estimator': self.last_input_estimator.get('estimator', 'not_recorded'),
            'reservation_estimator': 'input_estimate_observation_only'}

    def export_state(self) -> dict[str, Any]:
        fields = ('task_id','tool_limit','repair_limit','replan_limit','seconds','investigation_seconds',
            'tools','replans','repairs','model_calls','model_usage','events','phase','step',
            'model_token_limit','retrieval_token_limit','max_input_tokens','max_output_tokens',
            'closing_token_reserve','model_tokens','model_estimated_tokens','token_accounting_version',
            'react_round_limit','react_rounds','react_decisions','react_round_policy','sql_generation_limit','direct_sql_generations',
            'model_reservations','pending_tools','reference_now','last_input_estimator')
        return {**{key: getattr(self,key) for key in fields}, 'spent': self.elapsed()}

    @classmethod
    def from_state(cls, task_id: str, payload: dict[str, Any]) -> 'RunBudget':
        allowed = set(cls.__dataclass_fields__) - {'task_id','started','checkpoint_callback'}
        data = {k:v for k,v in payload.items() if k in allowed}
        data.setdefault('spent', payload.get('active_seconds', 0))
        # Missing policy belongs to pre-v3 checkpoints, never grant a restored
        # query an extra allowance merely because the new default is three.
        data.setdefault('sql_generation_limit', 2)
        data.setdefault('react_round_limit', 5)
        if 'react_rounds' not in data:
            data['react_rounds'] = sum(item.get('node') == 'react' for item in payload.get('model_usage', [])
                                       if isinstance(item, dict))
        if 'react_round_policy' not in data:
            # Historical tasks retain their already spent, more conservative
            # decision count. Restoring never grants back old allowances.
            data['react_round_policy'] = 'legacy_decisions'
        data.setdefault('react_decisions', sum(item.get('node') == 'react'
                                              for item in payload.get('model_usage', []) if isinstance(item, dict)))
        if payload.get('token_accounting_version', 1) < 2:
            actual: dict[str, int] = {}
            for item in payload.get('model_usage', []):
                if not isinstance(item, dict):
                    continue
                total = cls._actual_tokens(item.get('usage'))
                if total is not None:
                    group = 'chat' if item.get('kind') == 'chat' else 'retrieval'
                    actual[group] = actual.get(group, 0) + total
            legacy_totals = payload.get('model_tokens') or {}
            data['model_estimated_tokens'] = {group: max(0, int(total) - actual.get(group, 0))
                                             for group, total in legacy_totals.items()
                                             if total > actual.get(group, 0)}
            data['model_tokens'] = actual
            data['token_accounting_version'] = 2
        budget = cls(task_id=task_id, **data)
        pending = list(budget.model_reservations.items())
        for key, item in pending:
            budget.spent = min(budget.seconds, budget.spent + item.get('seconds_reserved', 0))
            budget.settle_model(key, None)
        for item in list(budget.pending_tools.values()):
            budget.spent = min(budget.seconds, budget.spent + item.get('seconds_reserved', 0))
            budget.record('tool_duration_unknown', name=item.get('name'),
                          seconds_reserved=item.get('seconds_reserved', 0))
        budget.pending_tools.clear()
        return budget

    def react_decision(self, *, allow_closing: bool = False) -> None:
        self.check()
        if self.react_rounds >= self.react_round_limit and (not allow_closing or self.react_round_policy == 'legacy_decisions'):
            raise BudgetStop(f'业务取证次数上限（{self.react_round_limit}轮）')
        self.react_decisions += 1
        if self.react_round_policy == 'legacy_decisions':
            self.react_rounds += 1
        self.record('react_decision', index=self.react_decisions)

    def query_round(self) -> None:
        self.check()
        if self.react_round_policy == 'legacy_decisions':
            return
        if self.react_rounds >= self.react_round_limit:
            raise BudgetStop(f'业务取证次数上限（{self.react_round_limit}轮）')
        self.react_rounds += 1
        self.record('query_round', index=self.react_rounds, limit=self.react_round_limit)

    def begin_tool_call(self, name: str) -> str:
        self.check()
        if self.tools >= self.tool_limit:
            raise BudgetStop(f'工具调用预算耗尽（{self.tool_limit}次）')
        key = uuid.uuid4().hex
        self.pending_tools[key] = {'name': name, 'seconds_reserved': self.remaining()}
        self.tool(name)
        return key

    def finish_tool_call(self, key: str) -> None:
        self.pending_tools.pop(key, None)
        self.checkpoint()

    def start(self) -> None:
        if self.started is None:
            self.started = time.monotonic()

    def elapsed(self) -> float:
        return self.spent + (time.monotonic() - self.started if self.started is not None else 0)

    def pause(self) -> None:
        self.spent = self.elapsed()
        self.started = None

    def remaining(self) -> float:
        limit = self.seconds if self.phase == 'closing' else min(self.seconds, self.investigation_seconds)
        return max(0, limit - self.elapsed())

    def check(self) -> None:
        from .workbench_runtime import check_execution
        check_execution()
        if self.remaining() <= 0:
            raise BudgetStop(f'{self.phase}主动处理时间预算耗尽')

    def record(self, stage: str, **details: Any) -> None:
        self.events.append({'sequence': len(self.events) + 1, 'stage': stage,
                            'step_id': self.step, 'active_seconds': round(self.elapsed(), 3), **details})
        self.checkpoint()

    def skill(self, name: str, instructions: str, node: str) -> None:
        self.record('skill_injection', node=node, skill=name, characters=len(instructions),
                    content_sha256=hashlib.sha256(instructions.encode('utf-8')).hexdigest())

    def tool(self, name: str) -> None:
        self.check()
        if self.tools >= self.tool_limit:
            raise BudgetStop(f'工具调用预算耗尽（{self.tool_limit}次）')
        self.tools += 1
        self.record('tool_dispatch', event='tool', name=name, index=self.tools)

    def repair(self, reason: str) -> None:
        self.check()
        used = self.repairs.get(self.step, 0)
        if used >= self.repair_limit:
            raise BudgetStop(f'步骤{self.step}额外修复额度耗尽（{self.repair_limit}次）')
        self.repairs[self.step] = used + 1
        self.record('repair', event='repair', reason=reason)

    def replan(self) -> None:
        self.check()
        if self.replans >= self.replan_limit:
            raise BudgetStop('任务重规划额度耗尽')
        self.replans += 1
        self.record('replan', index=self.replans)

    def public(self) -> dict[str, Any]:
        return {'tools': self.tools, 'tool_limit': self.tool_limit, 'repairs': dict(self.repairs),
                'repair_limit': self.repair_limit, 'replans': self.replans, 'replan_limit': self.replan_limit,
                'react_rounds': self.react_rounds, 'react_round_limit': self.react_round_limit,
                'react_decisions': self.react_decisions, 'react_round_policy': self.react_round_policy,
                'direct_sql_generations': dict(self.direct_sql_generations),
                'sql_generation_limit': self.sql_generation_limit,
                'model_calls': dict(self.model_calls), 'model_usage': list(self.model_usage), 'events': list(self.events), 'active_seconds': round(self.elapsed(), 3),
                'seconds_limit': self.seconds, 'investigation_seconds': self.investigation_seconds,
                'remaining_seconds': round(max(0, self.seconds-self.elapsed()), 3), 'model_budget': self.model_budget(),
                'input_estimator': dict(self.last_input_estimator)}


current_budget: ContextVar[RunBudget | None] = ContextVar('askdata_budget', default=None)
