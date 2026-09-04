"""非阻塞澄清请求（v1.2：高代价歧义显式反馈用户，不阻塞当前 run）。"""

from __future__ import annotations

from pydantic import BaseModel


class ClarifyRequest(BaseModel):
    kind: str = "clarify_request"
    reason: str = ""
    missing_columns: list[str] = []
    question: str = ""
    options: list[str] = []
    suggestion: str = ""
