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
