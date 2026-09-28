"""M4-B：Skill 装载链——三级渐进披露、不得扩权、关掉之后门禁要能动。

这个文件里最贵的一条是 `test_turning_off_the_chart_skill_moves_the_gate`：
它就是 M4 骨架的验收判据（"关掉某 skill 后门禁能量出指标差"）。
其余用例保证这条判据不是偶然——规则表真的被确定性消费，而不是贴在那儿当装饰。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentflow.core.config import load_config  # noqa: E402
from agentflow.core.grading import _p_chart_type, _p_skills_active, fingerprint  # noqa: E402
from agentflow.core.skill import (  # noqa: E402
    MAX_BODY_CHARS,
    SkillError,
    load_skill_set,
    parse_skill,
)
from agentflow.core.tools import build_default_registry  # noqa: E402

SKILLS_DIR = ROOT / "skills"
ROSTER = ["explorer", "planner", "executor", "inspector", "visualizer", "reporter", "critic"]
PROFIT_CSV = str(ROOT / "demo" / "data" / "retail_sales_with_profit.csv")


class FakeTranscript:
    def __init__(self):
        self.entries = []

    def write(self, record):
        self.entries.append(record)

    def events(self):
        return [entry.get("event") for entry in self.entries]


def _registry():
    return build_default_registry(load_config(None))


def _load(skills_dir=None, disabled=(), transcript=None, registry=None):
    return load_skill_set(
        skills_dir=skills_dir or SKILLS_DIR,
        registry=registry if registry is not None else _registry(),
        disabled=list(disabled),
        transcript=transcript,
        known_agents=ROSTER,
    )


def _write_skill(
    directory: Path,
    name: str,
    body: str = "方法正文。\n",
    applies_to=None,
    requires_tools=None,
) -> Path:
    """造一只临时 skill。frontmatter 手写而不是走 yaml.dump：
    装载器读到的必须是"人会手写出来的那种文件"。
    """
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", "name: " + name, "version: 1.0.0", "description: " + name + " 的 L1 描述"]
    if applies_to:
        lines.append("applies_to: [" + ", ".join(applies_to) + "]")
    if requires_tools:
        lines.append("requires_tools:")
        for agent, tools in requires_tools.items():
            lines.append("  " + agent + ": [" + ", ".join(tools) + "]")
    lines += ["---", body]
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------- 仓库里这两只


def test_repo_skills_load_and_are_assigned_to_specific_roles():
    skills = _load()
    installed = {skill.name: skill for skill in skills.installed}
    assert set(installed) == {"chart_selection", "cross_table_triage"}, "首批 2 只 skill 都要在场"
    assert installed["chart_selection"].applies_to == ("visualizer",)
    assert set(installed["cross_table_triage"].applies_to) == {"planner", "executor"}
    assert not skills.refused


def test_level_one_index_is_shared_while_level_two_body_is_not():
    """L1 索引人人都看得到（模型要知道有什么方法可用），L2 正文只给点名的角色。"""
    skills = _load()
    index_marker = "可用方法（skill 索引"
    for agent in ROSTER:
        assert index_marker in skills.prompt_block(agent), agent
    assert "选图不是审美问题" in skills.prompt_block("visualizer"), "L2 正文没进它点名的角色"
    assert "选图不是审美问题" not in skills.prompt_block("reporter")
    assert "先各自成数" in skills.prompt_block("planner")
    assert "先各自成数" not in skills.prompt_block("visualizer")


def test_level_three_reference_is_declared_and_readable():
    skill = _load().get("chart_selection")
    assert skill.references == {"rules": "references/rules.yaml"}
    rules = skill.reference(skill.reference_path("rules"))
    assert isinstance(rules["rules"], list) and rules["rules"], "规则表必须是可消费的列表"
    # 兜底条目必须存在：没有它，"规则走完什么都没选"会和"没有方法"混成同一件事
    assert any(not rule.get("when") for rule in rules["rules"])


def test_reference_cannot_escape_the_skill_directory():
    skill = _load().get("chart_selection")
    with pytest.raises(SkillError):
        skill.reference("../../pyproject.toml")
    with pytest.raises(SkillError):
        skill.reference("references/nope.yaml")


def test_reference_key_must_be_declared_in_frontmatter():
    """没声明过的键不能读：否则 SKILL.md 里的声明只是装饰。"""
    with pytest.raises(SkillError):
        _load().get("cross_table_triage").reference_path("rules")


# ---------------------------------------------------------------- 不得扩权


def test_skill_requiring_an_ungranted_tool_is_refused_and_audited(tmp_path):
    """skill 不得自带扩权：它要求的工具不在角色白名单里 ⇒ 整只拒装。"""
    transcript = FakeTranscript()
    _write_skill(
        tmp_path,
        "sneaky",
        applies_to=["planner"],
        requires_tools={"planner": ["execute_python"]},
    )
    registry = _registry()
    before = {agent: registry.whitelist_for(agent) for agent in ROSTER}
    skills = load_skill_set(
        skills_dir=tmp_path,
        registry=registry,
        disabled=[],
        transcript=transcript,
        known_agents=ROSTER,
    )
    assert skills.installed == [], "扩权 skill 不该装上"
    assert skills.refused[0]["reason"] == "requires_tools_unsatisfied"
    assert skills.refused[0]["missing"] == [{"agent": "planner", "missing": ["execute_python"]}]
    assert "skill_refused_no_escalation" in transcript.events()
    # 拒装之外还有一条：装载过程本身也不许改动白名单
    assert {agent: registry.whitelist_for(agent) for agent in ROSTER} == before


def test_multi_role_skill_is_refused_whole_not_half(tmp_path):
    """两只角色一只能一只能不能：整只拒装。只注入一半的方法正文比完全不注入更危险。"""
    _write_skill(
        tmp_path,
        "half",
        applies_to=["executor", "planner"],
        requires_tools={"executor": ["execute_python"], "planner": ["read_artifact"]},
    )
    skills = load_skill_set(skills_dir=tmp_path, registry=_registry(), known_agents=ROSTER)
    assert skills.installed == []
    assert skills.refused[0]["missing"] == [{"agent": "planner", "missing": ["read_artifact"]}]


def test_applies_to_an_unknown_role_is_refused(tmp_path):
    _write_skill(tmp_path, "ghost", applies_to=["wizard"])
    skills = load_skill_set(skills_dir=tmp_path, registry=_registry(), known_agents=ROSTER)
    assert skills.installed == []
    assert skills.refused[0]["missing"] == [{"agent": "wizard", "missing": ["not_a_known_agent"]}]


def test_satisfiable_requirements_install_and_record_injections():
    registry = _registry()
    skills = _load(registry=registry, transcript=FakeTranscript())
    assert skills.injections == [], "装载阶段还没有角色实例，注入留痕发生在构建时"
    from agentflow.agents.visualizer import VisualizerAgent
    from agentflow.core.llm import MockLLM

    agent = VisualizerAgent(llm=MockLLM(), config={}, registry=registry, skills=skills)
    entry = skills.injections[-1]
    assert entry["agent"] == "visualizer"
    assert {item["name"] for item in entry["bodies"]} == {"chart_selection"}
    assert entry["indexed"] == ["chart_selection", "cross_table_triage"]
    assert "选图不是审美问题" in agent.system_prompt, "注入必须真的进了这个实例的 prompt"


# ---------------------------------------------------------------- 关停与降级留因


def test_disabling_a_skill_leaves_a_reason_in_the_audit():
    transcript = FakeTranscript()
    skills = _load(disabled=["chart_selection"], transcript=transcript)
    assert {skill.name for skill in skills.installed} == {"cross_table_triage"}
    assert skills.disabled == [{"skill": "chart_selection", "reason": "explicitly_disabled"}]
    assert "skill_disabled" in transcript.events()
    assert "选图不是审美问题" not in skills.prompt_block("visualizer")


def _visualizer(skills_dir=None, disabled=()):
    from agentflow.agents.visualizer import VisualizerAgent
    from agentflow.core.llm import MockLLM

    registry = _registry()
    return VisualizerAgent(
        llm=MockLLM(),
        config={},
        registry=registry,
        skills=load_skill_set(
            skills_dir=skills_dir or SKILLS_DIR,
            registry=registry,
            disabled=list(disabled),
            known_agents=ROSTER,
        ),
    )


class _Ctx:
    def __init__(self, constraints=None, date_column="订单日期"):
        self.constraints = constraints or {}
        self.schema_profile = {"suggested_date_column": date_column}
        self.transcript = None


def _decide(agent, task, ctx):
    return agent._decide_chart_type(task, {}, ctx)


def test_rules_drive_every_branch_of_the_old_hardcoded_logic():
    """规则表必须覆盖原来硬编码的每一支——否则这次搬迁就顺手改了行为。"""
    agent = _visualizer()
    date_task = {"chart_type": "", "required_columns": ["订单日期", "销售额"]}
    cases = [
        ({"display": ["画柱状图"]}, date_task, "bar"),
        ({"display": ["要折线"]}, date_task, "line"),
        ({"display": ["饼图吧"]}, date_task, "pie"),
        ({}, {"chart_type": "hist", "required_columns": []}, "hist"),
        ({}, {"chart_type": "none", "required_columns": ["订单日期"]}, "none"),
        ({}, date_task, "line"),
        ({}, {"chart_type": "", "required_columns": ["产品类别"]}, "bar"),
    ]
    for constraints, task, expected in cases:
        assert _decide(agent, task, _Ctx(constraints)) == expected, (constraints, task)
    assert agent.no_chart_reason == "", "有方法时不该留下降级原因"


def test_rule_order_now_beats_constraint_order():
    """一处**有意**的行为差异：同时给两个冲突偏好时由规则表顺序决定（原实现是逐条约束内试）。
    记下来是因为它真的变了——搬运动不许顺手改语义，改了就要有名字。
    """
    agent = _visualizer()
    task = {"chart_type": "", "required_columns": []}
    assert _decide(agent, task, _Ctx({"display": ["要饼图", "要柱状"]})) == "bar"


def test_disabled_skill_means_no_chart_and_a_stated_reason():
    agent = _visualizer(disabled=["chart_selection"])
    assert _decide(agent, {"chart_type": "", "required_columns": ["订单日期"]}, _Ctx()) == "none"
    assert "未装载选图方法" in agent.no_chart_reason, "不画图必须说清为什么不画"


def _rules_only_skill(tmp_path: Path, rules_yaml: str) -> Path:
    directory = tmp_path / "chart_selection"
    (directory / "references").mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: chart_selection\nversion: 9.9.9\n"
        "description: 只换规则表的同名 skill\napplies_to: [visualizer]\n"
        "references:\n  rules: references/rules.yaml\n---\n正文\n",
        encoding="utf-8",
    )
    (directory / "references" / "rules.yaml").write_text(rules_yaml, encoding="utf-8")
    return tmp_path


def test_no_catch_all_rule_is_reported_not_silently_defaulting(tmp_path):
    directory = _rules_only_skill(
        tmp_path,
        "rules:\n  - id: only_line\n    when: {constraint_contains: [折线]}\n    then: {chart_type: line}\n",
    )
    agent = _visualizer(directory)
    assert _decide(agent, {"chart_type": "", "required_columns": []}, _Ctx()) == "none"
    assert "无兜底" in agent.no_chart_reason


def test_unknown_rule_condition_raises_instead_of_being_skipped(tmp_path):
    """静默跳过的规则等于一条没在守门的规则——这里选择报错。"""
    directory = _rules_only_skill(
        tmp_path,
        "rules:\n  - id: typo\n    when: {required_has_date_col: true}\n    then: {chart_type: line}\n",
    )
    agent = _visualizer(directory)
    with pytest.raises(ValueError, match="未知条件"):
        _decide(agent, {"chart_type": "", "required_columns": []}, _Ctx())


def test_rule_table_is_a_closed_vocabulary_not_an_expression_language():
    """`when` 的键是白名单制的封闭集合：给 yaml 开表达式语法等于把代码执行权交给配置文件。"""
    from agentflow.agents.visualizer import VisualizerAgent

    assert set(VisualizerAgent.RULE_KEYS) == {
        "constraint_contains",
        "task_chart_type_present",
        "required_has_date_column",
    }


# ---------------------------------------------------------------- 文本层面


def test_skill_without_description_is_rejected(tmp_path):
    directory = tmp_path / "anon"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nname: anon\n---\n正文\n", encoding="utf-8")
    with pytest.raises(SkillError):
        parse_skill(directory)


def test_oversized_body_is_rejected(tmp_path):
    """正文常驻 prompt，长度得有人签字；要更长就拆到 references/ 按需读。"""
    directory = tmp_path / "long"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: long\nversion: 1\ndescription: d\napplies_to: [planner]\n---\n"
        + "字" * (MAX_BODY_CHARS + 1),
        encoding="utf-8",
    )
    with pytest.raises(SkillError, match="上限"):
        parse_skill(directory)


def test_skills_dir_without_any_skill_is_not_an_error(tmp_path):
    (tmp_path / "not_a_skill").mkdir()
    (tmp_path / "not_a_skill" / "README.md").write_text("这里没有 SKILL.md", encoding="utf-8")
    assert load_skill_set(skills_dir=tmp_path, registry=_registry()).installed == []
    assert load_skill_set(skills_dir=tmp_path / "nope", registry=_registry()).installed == []


def test_skill_hash_enters_fingerprint_and_follows_edits(tmp_path):
    skill_dir = _write_skill(tmp_path, "small", applies_to=["planner"])
    path = skill_dir / "SKILL.md"
    first = fingerprint(skills=_load(tmp_path))
    assert first["skill:small"] == hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    path.write_text(path.read_text(encoding="utf-8").replace("方法正文", "换了正文"), encoding="utf-8")
    second = fingerprint(skills=_load(tmp_path))
    assert second["skill:small"] != first["skill:small"], "改了方法文本必须能在指纹里看出来"


def test_run_fingerprint_lists_the_same_two_skills_the_run_loaded():
    """`run_config.json` 的 skill 栏必须与真实运行时装的是同一份——两处走同一个装载器。"""
    from agentflow.pipeline import AGENT_ROSTER
    from scripts.run_eval import agent_prompt_map

    config = load_config(None)
    registry = build_default_registry(config)
    skills = _load(disabled=(config.get("skills") or {}).get("disabled") or [])
    out = fingerprint(agents=agent_prompt_map(), config=config, skills=skills)
    assert sorted(key for key in out if key.startswith("skill:")) == [
        "skill:chart_selection",
        "skill:cross_table_triage",
    ]
    assert set(skill.name for skill in skills.installed) == {"chart_selection", "cross_table_triage"}
    # 关掉一只 ⇒ 指纹里那一栏消失（分数变化时能立刻配对到"方法少了"这个 diff）
    off = fingerprint(agents=agent_prompt_map(), config=config, skills=_load(disabled=["chart_selection"]))
    assert "skill:chart_selection" not in off and "skill:cross_table_triage" in off


# ---------------------------------------------------------------- 谓词与真流程


def _evidence(skills=None, charts=None):
    return {
        "evaluation": {
            "skills": skills,
            "chart_types": charts or {},
            "chart_success": 1 if (charts or {}).get("2") not in (None, "none") else None,
        },
        "report": "",
        "transcript": [],
        "plan": {},
    }


def test_skills_active_predicate_separates_installed_from_injected():
    good = {
        "installed": [{"name": "chart_selection"}, {"name": "cross_table_triage"}],
        "refused": [],
        "injected": [{"agent": "visualizer", "bodies": [{"name": "chart_selection"}]}],
    }
    assert _p_skills_active(_evidence(good), {"installed": ["chart_selection"]}, "mock")[0]
    # 装上了但没注入 ⇒ 方法根本没生效，这时"installed 绿"是假绿
    half = {"installed": good["installed"], "refused": [], "injected": []}
    passed, detail = _p_skills_active(_evidence(half), {"injected": {"visualizer": ["chart_selection"]}}, "mock")
    assert not passed and "未注入" in detail
    # 本该被拒装却装上了 ⇒ 扩权被静默接受
    passed, detail = _p_skills_active(_evidence(half), {"refused": ["chart_selection"]}, "mock")
    assert not passed and "扩权" in detail


def test_chart_type_predicate_reads_the_per_task_field():
    evidence = _evidence(charts={"1": "none", "2": "line"})
    assert _p_chart_type(evidence, {"task": 2, "expect": "line"}, "mock")[0]
    passed, detail = _p_chart_type(evidence, {"task": 2, "expect": "bar"}, "mock")
    assert not passed and "chart_type" in detail
    assert _p_chart_type(evidence, {"expect_any": "line"}, "mock")[0]


def _run(tmp_path, question, disabled=None, data=None, pack=None):
    from agentflow.pipeline import run_analysis

    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.yaml"
    if disabled:
        config.write_text(
            "skills:\n  disabled: [" + ", ".join(disabled) + "]\n", encoding="utf-8"
        )
    result = run_analysis(
        question,
        data or [PROFIT_CSV],
        config_path=str(config) if disabled else None,
        outputs_root=tmp_path / "out",
        pack=pack,
    )
    directory = Path(result["outputs_dir"])
    evaluation = json.loads((directory / "evaluation.json").read_text(encoding="utf-8"))
    transcript = [
        json.loads(line)
        for line in (directory / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return result, evaluation, transcript


def test_real_run_traces_skills_in_transcript_and_evaluation(tmp_path):
    """三级披露的每一级都要在事实层留痕：注入事件（L1/L2）与规则表读取事件（L3）。"""
    _result, evaluation, transcript = _run(tmp_path, "最近7天每日销售额的走势如何？")
    skills = evaluation["skills"]
    assert {item["name"] for item in skills["installed"]} == {"chart_selection", "cross_table_triage"}
    assert any(entry["agent"] == "planner" for entry in skills["injected"])
    events = [entry.get("event") for entry in transcript]
    assert "skills_loaded" in events and "skill_injected" in events
    reads = [entry for entry in transcript if entry.get("event") == "skill_reference_read"]
    assert reads and reads[0]["skill"] == "chart_selection" and len(reads[0]["sha256"]) == 12


def test_turning_off_the_chart_skill_moves_the_gate(tmp_path):
    """M4 骨架验收判据：关掉一只 skill 之后，门禁必须能量出指标差。

    同时验三件事：① 图真的没了（chart_success 从 1 变 None）；
    ② `chart_type` 那条 gate 谓词转红——不是"少了一张图而没人知道"；
    ③ `skills_active` 也转红，且关停的痕迹在 evaluation.json 里查得到。
    """
    _baseline, base_eval, _ = _run(tmp_path / "on", "最近7天每日销售额的走势如何？")
    assert base_eval["chart_types"]["2"] == "line" and base_eval["chart_success"]

    _result, off_eval, transcript = _run(
        tmp_path / "off", "最近7天每日销售额的走势如何？", disabled=["chart_selection"]
    )
    assert off_eval["chart_success"] is None, "关掉选图方法后不该再有图"
    assert off_eval["chart_types"]["2"] == "none"
    assert "未装载选图方法" in (off_eval["chart_notes"].get("2") or ""), "降级必留因"
    evidence = _evidence(off_eval["skills"], off_eval["chart_types"])
    assert not _p_chart_type(evidence, {"task": 2, "expect": "line"}, "mock")[0]
    passed, detail = _p_skills_active(
        evidence,
        {"installed": ["chart_selection"], "injected": {"visualizer": ["chart_selection"]}},
        "mock",
    )
    assert not passed and "未装载" in detail and "未注入" in detail
    assert [entry for entry in transcript if entry.get("event") == "skill_disabled"], "关停也要留痕"
