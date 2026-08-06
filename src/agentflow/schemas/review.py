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
