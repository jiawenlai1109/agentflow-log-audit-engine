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


class MessageCreateRequest(BaseModel):
    """会话续轮请求。

    mode 必须与 AnalyzeRequest 一样收紧成白名单：pipeline 用
    `MockLLM() if mode == "mock" else OpenAILLM(...)` 选客户端，
    任何未校验的字符串都会落到真实（计费）分支上。
    """

    question: str = Field(min_length=1)
    mode: str = Field(default="mock", pattern="^(mock|real)$")


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
    """会话对客户端只暴露标识与标题；dataset_path 是服务器绝对路径，不外泄。"""

    session_id: str
    title: str | None
    turn_count: int


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
