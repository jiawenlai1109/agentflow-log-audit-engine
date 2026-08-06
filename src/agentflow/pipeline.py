"""pipeline：端到端组装 run_analysis（CLI 入口在 Phase 4 接入）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentflow.agents import (
    CriticAgent,
    ExecutorAgent,
    ExplorerAgent,
    InspectorAgent,
    PlannerAgent,
    ReporterAgent,
    VisualizerAgent,
)
from agentflow.core.budget import BudgetCounter
from agentflow.core.config import load_config
from agentflow.core.context import SessionContext
from agentflow.core.llm import BaseLLM, MockLLM, OpenAILLM
from agentflow.core.orchestrator import Orchestrator
from agentflow.core.tools import build_default_registry


def build_agents(
    llm: BaseLLM,
    registry: Any,
    config: dict[str, Any],
    budget: Any,
) -> dict[str, Any]:
    common = {
        "llm": llm,
        "config": config,
        "budget": budget,
        "registry": registry,
    }
    return {
        "explorer": ExplorerAgent(**common),
        "planner": PlannerAgent(**common),
        "executor": ExecutorAgent(**common),
        "inspector": InspectorAgent(**common),
        "visualizer": VisualizerAgent(**common),
        "reporter": ReporterAgent(**common),
        "critic": CriticAgent(**common),
    }


def run_analysis(
    question: str,
    data_path: str,
    config_path: str | Path | None = None,
    mode: str = "mock",
    llm: BaseLLM | None = None,
    outputs_root: str | Path | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """端到端运行一次分析，返回 {run_id, outputs_dir, status, report, task_states}。"""
    config = load_config(config_path)
    registry = build_default_registry()
    if llm is None:
        llm = MockLLM() if mode == "mock" else OpenAILLM()

    project_root = Path(__file__).resolve().parents[2]
    outputs_root = (
        Path(outputs_root) if outputs_root else project_root / "outputs"
    )
    outputs_root.mkdir(parents=True, exist_ok=True)

    session = None
    if session_id:
        session_dir = outputs_root / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session = SessionContext(session_id=session_id, session_dir=session_dir)

    budget = BudgetCounter(int(config["execution"]["max_llm_calls"]))
    agents = build_agents(llm, registry, config, budget)
    orchestrator = Orchestrator(
        config=config, registry=registry, agents=agents, budget=budget
    )
    result = orchestrator.run(
        question=question,
        data_path=str(Path(data_path).resolve()),
        outputs_root=outputs_root,
        session=session,
    )
    if session:
        _persist_session(session, question, result)
    return result


def _persist_session(
    session: SessionContext, question: str, result: dict[str, Any]
) -> None:
    """L3 会话记忆持久化（简化版：追加轮次 + 更新 summary）。"""
    report = result.get("report") or {}
    entry = {
        "turn": time_turn(session),
        "timestamp": result.get("run_id"),
        "question": question,
        "run_id": result.get("run_id"),
        "report_path": report.get("report_path"),
        "summary": report.get("summary", ""),
    }
    with session.conversation_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    session.summary_path.write_text(
        json.dumps({"summary": report.get("summary", "")}, ensure_ascii=False),
        encoding="utf-8",
    )


def time_turn(session: SessionContext) -> int:
    if not session.conversation_path.exists():
        return 1
    return len(session.conversation_path.read_text(encoding="utf-8").strip().splitlines()) + 1
