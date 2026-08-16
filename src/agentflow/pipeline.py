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
from agentflow.core.memory import (
    SUMMARIZER_SYSTEM,
    clean_turn,
    detect_conflicts,
    extract_key_numbers,
    merge_summary_mock,
)
from agentflow.core.orchestrator import Orchestrator
from agentflow.schemas.summary import SessionSummary
from agentflow.core.tools import build_default_registry


def build_agents(
    llm: BaseLLM,
    registry: Any,
    config: dict[str, Any],
    budget: Any,
) -> dict[str, Any]:
    agents_cfg = config.get("agents", {})
    return {
        name: cls(
            llm=_agent_llm(llm, agents_cfg.get(name, {})),
            config=config,
            budget=budget,
            registry=registry,
        )
        for name, cls in (
            ("explorer", ExplorerAgent),
            ("planner", PlannerAgent),
            ("executor", ExecutorAgent),
            ("inspector", InspectorAgent),
            ("visualizer", VisualizerAgent),
            ("reporter", ReporterAgent),
            ("critic", CriticAgent),
        )
    }


def _agent_llm(llm: BaseLLM, agent_cfg: dict[str, Any]) -> BaseLLM:
    """按 Agent 配置覆盖模型（真实模式下为每个 Agent 克隆一个带指定模型的客户端）。"""
    model = agent_cfg.get("model")
    if model and isinstance(llm, OpenAILLM):
        return OpenAILLM(
            api_key=llm.api_key,
            base_url=llm.base_url,
            model=model,
        )
    return llm


def run_analysis(
    question: str,
    data_path: str,
    config_path: str | Path | None = None,
    mode: str = "mock",
    llm: BaseLLM | None = None,
    outputs_root: str | Path | None = None,
    session_id: str | None = None,
    on_event: Any | None = None,
) -> dict[str, Any]:
    """端到端运行一次分析，返回 {run_id, outputs_dir, status, report, task_states}。"""
    config = load_config(config_path)
    registry = build_default_registry()
    if llm is None:
        llm_cfg = config.get("llm", {})
        llm = (
            MockLLM()
            if mode == "mock"
            else OpenAILLM(
                base_url=llm_cfg.get("base_url") or None,
                model=llm_cfg.get("model") or None,
            )
        )

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
        config=config,
        registry=registry,
        agents=agents,
        budget=budget,
        on_event=on_event,
    )
    result = orchestrator.run(
        question=question,
        data_path=str(Path(data_path).resolve()),
        outputs_root=outputs_root,
        session=session,
    )
    if session:
        _persist_session(session, question, result, llm=llm)
    return result


def _persist_session(
    session: SessionContext,
    question: str,
    result: dict[str, Any],
    llm: BaseLLM | None = None,
) -> None:
    """L3 会话记忆持久化：TurnCleaner 结构化轮次 + Summarizer 滚动合并 + 冲突检测。"""
    report = result.get("report") or {}
    key_numbers: dict[str, float] = {}
    mentioned_columns: list[str] = []
    evaluation_path = Path(result["outputs_dir"]) / "evaluation.json"
    if evaluation_path.exists():
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        key_numbers = extract_key_numbers(evaluation.get("results", {}))
    plan_path = Path(result["outputs_dir"]) / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        mentioned_columns = sorted(
            {
                column
                for task in plan.get("tasks", [])
                for column in task.get("required_columns", [])
            }
        )

    previous_turns = session.read_turns()
    conflicts = detect_conflicts(previous_turns, key_numbers)
    cleaned = clean_turn(question, report.get("summary", ""), key_numbers, mentioned_columns)
    entry = {
        "turn": (previous_turns[-1]["turn"] + 1) if previous_turns else 1,
        "question": question,
        "run_id": result.get("run_id"),
        "report_path": report.get("report_path"),
        **cleaned,
        "conflicts": conflicts,
    }
    session.append_turn(entry)
    old_summary = session.load_summary()
    summary = _summarize(old_summary, entry, llm)
    summary.update({"conflicts": conflicts, "last_run_id": result.get("run_id")})
    session.save_summary(summary)


def _summarize(
    old_summary: dict[str, Any] | None,
    turn: dict[str, Any],
    llm: BaseLLM | None,
) -> dict[str, Any]:
    """Summarizer：real 模式用 LLM 增量合并为结构化摘要；mock 模式确定性合并。"""
    if llm is None or isinstance(llm, MockLLM):
        return merge_summary_mock(old_summary, turn)
    prompt = (
        "旧会话摘要：\n"
        f"{old_summary}\n\n"
        "新一轮对话：\n"
        f"问题：{turn.get('question', '')}\n"
        f"结论摘要：{turn.get('answer_summary', '')}\n"
        f"关键数字：{turn.get('key_numbers', {})}\n"
        "请合并输出新的会话摘要 JSON。"
    )
    try:
        result = llm.complete_structured(
            system=SUMMARIZER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            schema=SessionSummary,
            temperature=0.2,
            max_tokens=800,
        )
        return result.model_dump(mode="json")
    except Exception:  # noqa: BLE001 - 摘要失败退化为确定性合并
        return merge_summary_mock(old_summary, turn)
