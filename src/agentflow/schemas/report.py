"""Reporter → ReportResult（正常 / 降级）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator

from agentflow.schemas.result import ErrorClass


class FailureInfo(BaseModel):
    """降级报告的错误信息。

    LLM 常把这些字段输出成 null，直接透传会让整份降级报告写入失败（把可预期的降级
    变成 run_error），因此这里统一兜底为合法默认值。
    """

    error_class: ErrorClass = ErrorClass.UNKNOWN
    error: str = ""
    suggestion: str = ""

    @field_validator("error", "suggestion", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("error_class", mode="before")
    @classmethod
    def _unknown_fallback(cls, value: Any) -> Any:
        if value is None or value == "":
            return ErrorClass.UNKNOWN
        try:
            return ErrorClass(str(value))
        except ValueError:
            return ErrorClass.UNKNOWN


class ReportResult(BaseModel):
    report_path: str
    degraded: bool = False
    sections: list[str] = []
    time_base_note: str | None = None
    failure_info: FailureInfo | None = None
    summary: str = ""
