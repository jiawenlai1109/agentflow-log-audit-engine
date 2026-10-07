"""Executor → TaskExecutionResult（含错误分类）。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel


class ErrorClass(str, Enum):
    MISSING_COLUMN = "MISSING_COLUMN"
    EMPTY_RESULT = "EMPTY_RESULT"
    CODE_ERROR = "CODE_ERROR"
    TIMEOUT = "TIMEOUT"
    LLM_ERROR = "LLM_ERROR"
    # C-14②：任务声明的表在 Bundle 里一张都解析不到——拒绝，不退回主表
    DATASET_SCOPE = "DATASET_SCOPE"
    UNKNOWN = "UNKNOWN"


class TaskExecutionResult(BaseModel):
    task_id: int
    status: Literal["success", "failed"]
    error: str | None = None
    error_class: ErrorClass | None = None
    missing_columns: list[str] = []
    summary: dict | None = None
    intermediate_file: str | None = None
    duration_seconds: float = 0.0
    attempts: int = 1
    suggestion: str | None = None
