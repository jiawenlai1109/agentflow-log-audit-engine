"""Reporter → ReportResult（正常 / 降级）。"""

from __future__ import annotations

from pydantic import BaseModel

from agentflow.schemas.result import ErrorClass


class FailureInfo(BaseModel):
    error_class: ErrorClass
    error: str
    suggestion: str = ""


class ReportResult(BaseModel):
    report_path: str
    degraded: bool = False
    sections: list[str] = []
    time_base_note: str | None = None
    failure_info: FailureInfo | None = None
    summary: str = ""
