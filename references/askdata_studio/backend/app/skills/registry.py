from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..agent_configuration_runtime import effective_skill


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    output_actions: tuple[str, ...]
    # Keep the built-in body separate from a startup override. Long-lived
    # components must be able to restore defaults without a process restart.
    default_instructions: str = ""

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("instructions", None)
        payload.pop("default_instructions", None)
        payload["allowed_tools"] = list(self.allowed_tools)
        payload["output_actions"] = list(self.output_actions)
        return payload


class SkillRegistry:
    """从项目目录加载并校验应用级Skill。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parent
        self._skills = self._load()

    def get(self, name: str) -> SkillDefinition:
        from ..workbench_runtime import current_execution
        control = current_execution.get()
        if control and control.skills is not None and name not in control.skills and name not in {'database_query', 'data_qa'}:
            raise ValueError('本次分析未启用该 Skill：' + name)
        if name not in self._skills:
            raise KeyError(f"Skill不存在：{name}")
        skill = self._skills[name]
        default = (self.root / name / "SKILL.md").read_text(encoding="utf-8").strip()
        return SkillDefinition(**{**asdict(skill), "instructions": effective_skill(name, default),
                                  "default_instructions": default})

    def list(self) -> list[dict[str, Any]]:
        from ..workbench_runtime import current_execution
        control = current_execution.get()
        return [self._skills[name].public() for name in sorted(self._skills)
                if not control or control.skills is None or name in control.skills or name in {'database_query', 'data_qa'}]

    def _load(self) -> dict[str, SkillDefinition]:
        output: dict[str, SkillDefinition] = {}
        for config_path in sorted(self.root.glob("*/config.json")):
            directory = config_path.parent
            instruction_path = directory / "SKILL.md"
            if not instruction_path.exists():
                raise ValueError(f"Skill缺少SKILL.md：{directory.name}")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            name = str(payload.get("name") or directory.name).strip()
            if not name or name in output:
                raise ValueError(f"Skill名称无效或重复：{name}")
            output[name] = SkillDefinition(
                name=name,
                description=str(payload.get("description") or "").strip(),
                instructions="",
                allowed_tools=tuple(str(item) for item in payload.get("allowed_tools") or []),
                max_tool_calls=max(0, int(payload.get("max_tool_calls", 0))),
                output_actions=tuple(str(item) for item in payload.get("output_actions") or []),
            )
        return output
