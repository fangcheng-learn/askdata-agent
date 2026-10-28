from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SqlExecution:
    sql: str
    success: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    total_rows: int | None = None
    preview_truncated: bool = False
    complete: bool = True
    result_ref: str | None = None
