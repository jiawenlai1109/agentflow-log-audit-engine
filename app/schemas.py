"""后端请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class AnalyzeRequest(BaseModel):
    question: str = Field(min_length=1)
    dataset_id: int
    mode: str = Field(default="mock", pattern="^(mock|real)$")
    session_id: str | None = None


class SessionCreateRequest(BaseModel):
    title: str = "新会话"
    dataset_id: int | None = None


class JobOut(BaseModel):
    job_id: str
    status: str
    progress: int
    run_id: str | None = None
    error: str | None = None
    question: str


class DatasetOut(BaseModel):
    id: int
    filename: str
    size: int
    row_count: int
    columns: list[str]


class SessionOut(BaseModel):
    session_id: str
    title: str | None
    turn_count: int
    dataset_path: str | None


class MessageOut(BaseModel):
    turn: int
    question: str
    run_id: str | None
    answer_summary: str
    key_numbers: dict[str, float]


class EvaluationSummary(BaseModel):
    total: int
    status_count: dict[str, int]
    degraded_reasons: dict[str, int]
    chart: dict[str, int]
    critic: dict[str, int]
    avg_llm_calls: float
    avg_duration: float
    runs: list[dict[str, Any]] = []
