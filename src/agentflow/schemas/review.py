"""Critic → Review（报告评审结论）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ReviewIssue(BaseModel):
    severity: Literal["high", "medium", "low"] = "medium"
    section: str = ""
    message: str


class Review(BaseModel):
    verdict: Literal["PASS", "FAIL"]
    rounds: int = 1
    issues: list[ReviewIssue] = []
    # 评审器**自己**没跑成（LLM 调不通、预算耗尽）。这与"报告有毛病"是两类事：
    # 塞进 issues 会让一次运维级失败长得像一次内容判定（#13），而两者的处置方式相反——
    # 内容判定该驱动重写，运维失败重写修不好、只会把剩余预算烧在无效重试上。
    infrastructure_error: str | None = None
