"""会话滚动摘要（结构化 schema，对应《上下文与记忆设计》4.4）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KeyFinding(BaseModel):
    conclusion: str
    value: float | None = None
    turn: int | None = None
    run_id: str | None = None


class SessionSummary(BaseModel):
    goals: list[str] = Field(default_factory=list)
    data_refs: list[str] = Field(default_factory=list)
    key_findings: list[KeyFinding] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    last_focus: str = ""
