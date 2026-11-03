from typing import Any


class ModelOutputError(RuntimeError):
    """The provider responded, but its model content violates the JSON protocol.

    This is a bounded format-repair candidate, unlike unknown network/service
    outcomes, which must not silently dispatch another SQL generation request.
    """


class PipelineStageError(RuntimeError):
    """包含处理阶段信息的工作流异常。"""

    def __init__(self, stage: str, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.context = context or {}
