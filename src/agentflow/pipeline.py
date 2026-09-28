"""pipeline：端到端组装 run_analysis（CLI 入口在 Phase 4 接入）。"""

from __future__ import annotations

import hashlib
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
        clone = OpenAILLM(
            api_key=llm.api_key,
            base_url=llm.base_url,
            model=model,
            max_retries=llm.max_retries,
        )
        clone.budget = llm.budget
        return clone
    return llm


def _bundle_fingerprint(sources: list[Path]) -> str:
    """源文件路径 + mtime 的指纹：同一批文件复用同一个 Bundle，改动自动换 id。"""
    digest = hashlib.sha256()
    for path in sources:
        digest.update(f"{path.resolve()}|{path.stat().st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()[:12]


def as_bundle(sources: Any, outputs_root: Path) -> Any:
    """把"Bundle / 单个路径 / 路径列表"归一成一个 Bundle。

    不是多写一套兼容分支：单文件本就是只有一个成员的 Bundle，这里只是把它构造出来。
    构造结果落在 `<outputs_root>/bundles/bd_<指纹>/`，同批源文件复用同一份，
    这样重复跑同一数据不会反复归一化，而报告里的数字仍能指回一份固定快照。
    """
    from agentflow.core.bundle import Bundle
    from agentflow.core.ingest import build_bundle

    if isinstance(sources, Bundle):
        return sources
    if isinstance(sources, (str, Path)):
        sources = [sources]
    paths = [Path(item).resolve() for item in sources]
    if not paths:
        raise ValueError("没有输入文件")
    root = outputs_root / "bundles" / f"bd_{_bundle_fingerprint(paths)}"
    if (root / "manifest.json").exists():
        try:
            cached = Bundle.load(root)
        except Exception:  # noqa: BLE001 - 缓存坏了就重建，别让一次坏 manifest 卡住所有跑批
            cached = None
        if cached is not None and len(cached.tables) + len(cached.documents) == len(paths):
            return cached
    return build_bundle(paths, root, strict=True)


def run_analysis(
    question: str,
    sources: Any,
    config_path: str | Path | None = None,
    mode: str = "mock",
    llm: BaseLLM | None = None,
    outputs_root: str | Path | None = None,
    session_id: str | None = None,
    on_event: Any | None = None,
    pack: str | None = None,
) -> dict[str, Any]:
    """端到端运行一次分析，返回 {run_id, outputs_dir, status, report, task_states}。

    sources：Bundle 或文件路径（单个或列表）——异构输入在 `as_bundle` 里归一化。
    pack：场景包名称（如 login_audit），装载 packs/<name>/ 并切换为领域规则包模式。
    """
    config = load_config(config_path)
    registry = build_default_registry(config)
    pack_obj = None
    if pack:
        from agentflow.core.pack import load_pack

        pack_obj = load_pack(pack)
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
    # 相对路径必须先解析为绝对：产物内部的 ensure_allowed/ensure_within 会以
    # outputs_dir 为根拼接路径，相对 outputs_dir 会造成双重拼接（Critic 读不到报告）
    outputs_root = outputs_root.resolve()
    outputs_root.mkdir(parents=True, exist_ok=True)

    session = None
    if session_id:
        session_dir = outputs_root / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session = SessionContext(session_id=session_id, session_dir=session_dir)

    budget = BudgetCounter(int(config["execution"]["max_llm_calls"]))
    llm.budget = budget  # v1.2：预算计数点下沉到 LLM 层（每次真实 API 调用计 1）
    agents = build_agents(llm, registry, config, budget)
    orchestrator = Orchestrator(
        config=config,
        registry=registry,
        agents=agents,
        budget=budget,
        on_event=on_event,
    )
    bundle = as_bundle(sources, outputs_root)
    result = orchestrator.run(
        question=question,
        bundle=bundle,
        outputs_root=outputs_root,
        session=session,
        pack=pack_obj,
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
