"""Inspector → Verdict（PASS / WARN / FAIL 分级）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CheckResult(BaseModel):
    rule: str
    level: Literal["PASS", "WARN", "FAIL"]
    message: str


class Verdict(BaseModel):
    task_id: int
    status: Literal["PASS", "WARN", "FAIL"]
    checks: list[CheckResult] = []
    suggestion: str | None = None
