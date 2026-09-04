"""消息信封：所有 Agent 之间传递的统一结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AgentMessage:
    """Agent 间传递的统一消息信封。

    对应《输出格式设计.md》第 1 节，所有 Agent 输入输出均封装为该结构，
    并逐条写入 transcript.jsonl。
    """

    run_id: str
    sender: str
    receiver: str
    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "kind": self.kind,
            "content": self.content,
            "metadata": self.metadata,
            "artifacts": self.artifacts,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentMessage":
        return cls(
            run_id=data["run_id"],
            sender=data["sender"],
            receiver=data["receiver"],
            kind=data["kind"],
            content=data["content"],
            metadata=data.get("metadata", {}),
            artifacts=data.get("artifacts", []),
            created_at=data.get("created_at", ""),
        )


@dataclass
class HistoryEntry:
    role: str
    content: str
    kind: str = ""


class MessageHistory:
    """L1 有界消息历史（上下文与记忆设计 §2）。

    规则：append 追加；bounded(limit) 保留最新消息、先丢最旧的 tool/assistant 轮次；
    to_llm_messages() 输出 OpenAI 格式。多轮自愈/重试的历史长度由此封顶。
    """

    def __init__(self, limit: int = 8) -> None:
        self.limit = max(2, int(limit))
        self.entries: list[HistoryEntry] = []

    def append(self, role: str, content: str, kind: str = "") -> None:
        self.entries.append(HistoryEntry(role=role, content=content, kind=kind))

    def bounded(self) -> list[HistoryEntry]:
        """按上限截断：保留最新的 limit 条；system 类消息不因截断丢失（由调用方在首条固定）。"""
        if len(self.entries) <= self.limit:
            return list(self.entries)
        tail = self.entries[-self.limit :]
        # 尽量保证截断边界落在 user 消息上（成对丢弃 assistant+user 轮次）
        while tail and tail[0].role == "assistant" and len(tail) > 2:
            tail = tail[1:]
        return tail

    def to_llm_messages(self) -> list[dict[str, str]]:
        return [{"role": entry.role, "content": entry.content} for entry in self.bounded()]
