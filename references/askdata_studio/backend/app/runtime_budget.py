"""Per-task active-time and shared retry limits; never charge time waiting for a user."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import time
from typing import Any


class BudgetStop(RuntimeError):
    pass


@dataclass
class RunBudget:
    task_id: str
    tool_limit: int = 20
    repair_limit: int = 2
    replan_limit: int = 1
    seconds: float = 300
    investigation_seconds: float = 240
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
        if self.remaining() <= 0:
            raise BudgetStop(f'{self.phase}主动处理时间预算耗尽')

    def record(self, stage: str, **details: Any) -> None:
        self.events.append({'sequence': len(self.events) + 1, 'stage': stage,
                            'step_id': self.step, 'active_seconds': round(self.elapsed(), 3), **details})

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
                'model_calls': dict(self.model_calls), 'model_usage': list(self.model_usage), 'events': list(self.events), 'active_seconds': round(self.elapsed(), 3),
                'seconds_limit': self.seconds, 'investigation_seconds': self.investigation_seconds,
                'remaining_seconds': round(max(0, self.seconds-self.elapsed()), 3)}


current_budget: ContextVar[RunBudget | None] = ContextVar('askdata_budget', default=None)
