"""运行上下文：RunContext（L2 黑板）+ SessionContext（L3 会话）+ run_id 生成。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentflow.core.tools import ensure_within


def new_run_id() -> str:
    """全局唯一 run_id：日期_时间_8位随机（uuid4 截断）。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"run_{ts}_{uuid4().hex[:8]}"


@dataclass
class SessionContext:
    """会话记忆上下文（对应《上下文与记忆设计.md》L3）。"""

    session_id: str
    session_dir: Path

    @property
    def conversation_path(self) -> Path:
        return self.session_dir / "conversation.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.session_dir / "summary.json"


@dataclass
class RunContext:
    """一次运行共享的黑板上下文（L2），对 Agent 只读；写入仅 Orchestrator。"""

    run_id: str
    question: str
    data_path: str
    outputs_dir: Path
    config: dict[str, Any]
    session: SessionContext | None = None
    transcript: Any = None  # TranscriptWriter
    budget: Any = None  # BudgetCounter
    schema_profile: dict[str, Any] | None = None
    task_list: dict[str, Any] | None = None
    results: dict[int, dict[str, Any]] = field(default_factory=dict)
    figures: dict[int, dict[str, Any]] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    task_states: dict[int, str] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def run_root(self) -> Path:
        return self.outputs_dir

    @property
    def work_dir(self) -> Path:
        return self.outputs_dir / "work"

    @property
    def artifacts_dir(self) -> Path:
        return self.outputs_dir / "artifacts"

    def task_work_dir(self, task_id: int) -> Path:
        return self.work_dir / str(task_id)

    def ensure_within(self, path: str | Path) -> Path:
        return ensure_within(self.outputs_dir, path)
