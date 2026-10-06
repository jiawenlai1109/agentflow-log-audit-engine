"""Inspector → Verdict（PASS / WARN / FAIL 分级）。"""

from __future__ import annotations

from typing import Any, Literal

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
    verification: Literal["ok", "skipped"] | None = None
    # 独立复算侧的 (主体, 指标, 数值)：PASS 也要落盘。
    # 只有 `finding_match_check:PASS` 这个令牌的话，"报告 == 账本"能证，
    # "账本 == 复算值"证不了——校验器被改成照抄生产实现时那条线照样绿。
    recompute: list[dict[str, Any]] = []
