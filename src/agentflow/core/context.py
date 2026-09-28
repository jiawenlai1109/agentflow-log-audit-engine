"""运行上下文：RunContext（L2 黑板）+ SessionContext（L3 会话）+ run_id 生成。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
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

    def append_turn(self, entry: dict[str, Any]) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.conversation_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_turns(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.conversation_path.exists():
            return []
        lines = self.conversation_path.read_text(encoding="utf-8").strip().splitlines()
        if limit is not None:
            lines = lines[-limit:]
        turns: list[dict[str, Any]] = []
        for line in lines:
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return turns

    def load_summary(self) -> dict[str, Any] | None:
        if not self.summary_path.exists():
            return None
        try:
            return json.loads(self.summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def save_summary(self, summary: dict[str, Any]) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False), encoding="utf-8"
        )


@dataclass
class RunContext:
    """一次运行共享的黑板上下文（L2），对 Agent 只读；写入仅 Orchestrator。

    输入的唯一真源是 `bundle`。`data_path` 是主表派生视图（= bundle.primary.path），
    不再单独存一份——单文件本就只有一个成员的 Bundle，两份数据迟早会走样。
    """

    run_id: str
    question: str
    bundle: Any  # agentflow.core.bundle.Bundle
    outputs_dir: Path
    config: dict[str, Any]
    session: SessionContext | None = None
    transcript: Any = None  # TranscriptWriter
    budget: Any = None  # BudgetCounter
    schema_profile: dict[str, Any] | None = None
    task_list: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None  # v1.2：用户约束（一等公民，注入下游全部 Agent）
    pack: Any = None  # 场景包（ScenarioPack）：登录审计等领域规则包（工作规划 §6.2）
    clarify: dict[str, Any] | None = None  # v1.2：非阻塞澄清请求
    join_preflight: dict[str, Any] = field(default_factory=dict)  # M2-3：派发前 join 预检留痕
    replan_used: int = 0  # v1.2：重规划预算（每 run ≤ max_replan_rounds）
    wall_clock_timeout: bool = False  # v1.2：运行墙钟超时标记
    results: dict[int, dict[str, Any]] = field(default_factory=dict)
    figures: dict[int, dict[str, Any]] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    task_states: dict[int, str] = field(default_factory=dict)
    degraded_reason: str | None = None
    critic_passed: bool | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def data_path(self) -> str:
        """主表路径：兼容"单表运行"的全部既有语义（执行 env、独立校验、报告行数基准）。"""
        return str(self.bundle.primary.path)

    @property
    def readable_paths(self) -> set[Path]:
        """本次运行允许读取的文件集合（表 + 原件副本 + 文档）。"""
        return self.bundle.readable_paths()

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
