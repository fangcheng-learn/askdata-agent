from typing import Any


class PipelineStageError(RuntimeError):
    """包含处理阶段信息的工作流异常。"""

    def __init__(self, stage: str, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.context = context or {}
