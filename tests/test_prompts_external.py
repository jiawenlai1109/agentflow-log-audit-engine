"""M4-A：prompts/ 外置后的三条承诺——逐字节搬迁、装载器不加工、缺文件不静默退默认。

这里最要紧的一条不是"文件能读出来"，而是**搬迁没有改动内容**：
下面钉住的 9 个 hash 是 2026-09-28 迁移*之前*由 `grading.fingerprint()` 实测得到的值。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentflow import agents as agent_module  # noqa: E402
from agentflow.agents.base import DATA_CONTENT_DEFENSE  # noqa: E402
from agentflow.core.grading import fingerprint  # noqa: E402
from agentflow.core.prompts import (  # noqa: E402
    DEFAULT_PROMPTS_DIR,
    KNOWN_PROMPTS,
    PromptError,
    load_prompt,
    prompt_hashes,
    prompt_version,
    split_frontmatter,
)

# 迁移前实测值（scripts 里跑 fingerprint 得到）。改任何一份 prompt 正文都会撞这里——
# 那时该做的是把新 hash 记进进度文档并说明为什么改，而不是把断言删掉。
PRE_MIGRATION_HASHES = {
    "critic": "d8435a6e38bb",
    "data_defense": "93bd3aaf495a",
    "executor": "0148a451cb91",
    "explorer": "3cec92f4e57d",
    "inspector": "a982bfa4c290",
    "planner": "ca032ec2ad28",
    "reporter": "e44a344aa675",
    "summarizer": "e4d8a26a1dfc",
    "visualizer": "c55800626f76",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def test_every_prompt_is_externalized_and_non_empty():
    assert DEFAULT_PROMPTS_DIR.exists()
    for name in KNOWN_PROMPTS:
        body = load_prompt(name)
        assert body.strip(), f"{name} 正文为空"
        assert prompt_version(name) != "unversioned", f"{name} 缺 version"


@pytest.mark.parametrize("name", sorted(PRE_MIGRATION_HASHES))
def test_migration_is_byte_for_byte(name):
    """外置只是搬家：正文 hash 必须与迁移前逐位相同。"""
    assert _sha(load_prompt(name)) == PRE_MIGRATION_HASHES[name]


@pytest.mark.parametrize(
    "role,class_name",
    [
        ("explorer", "ExplorerAgent"),
        ("planner", "PlannerAgent"),
        ("executor", "ExecutorAgent"),
        ("inspector", "InspectorAgent"),
        ("visualizer", "VisualizerAgent"),
        ("reporter", "ReporterAgent"),
        ("critic", "CriticAgent"),
    ],
)
def test_class_prompt_comes_from_the_file(role, class_name):
    """文件是唯一真源：类属性必须等于装载结果，而不是"另一份也长得一样的文本"。"""
    agent_class = getattr(agent_module, class_name)
    assert agent_class.system_prompt == load_prompt(role)


def test_defense_line_is_externalized_too():
    """防线文本会进 system prompt，所以它也得能被人改、也得进指纹——留在 .py 里时改它不动任何 hash。"""
    assert DATA_CONTENT_DEFENSE == load_prompt("data_defense")
    assert DATA_CONTENT_DEFENSE.startswith("\n\n"), "正文开头的空行是拼接用的，装载器不许 strip 掉"


def test_loader_returns_body_verbatim(tmp_path):
    path = tmp_path / "sample.md"
    path.write_text("---\nagent: sample\nversion: 9.9.9\n---\n\n  正文前两空格与首行空行\n尾行\n", encoding="utf-8")
    body = load_prompt("sample", prompts_dir=tmp_path)
    assert body == "\n  正文前两空格与首行空行\n尾行\n"


def test_missing_prompt_file_raises_instead_of_defaulting(tmp_path):
    """缺文件必须炸。静默退内置默认 = 跑着一份没人核对过的 prompt，而且看不出来。"""
    with pytest.raises(PromptError):
        load_prompt("nope", prompts_dir=tmp_path)


def test_file_without_frontmatter_is_used_as_whole_body(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("没有 frontmatter 的整段就是正文", encoding="utf-8")
    assert load_prompt("plain", prompts_dir=tmp_path) == "没有 frontmatter 的整段就是正文"


def test_split_frontmatter_ignores_a_dash_line_inside_body():
    """正文里出现 `---` 不能被当成 frontmatter 结束符（那是分隔线，不是元数据边界）。"""
    body, meta = split_frontmatter("---\nversion: 1\n---\n上文\n---\n下文\n")
    assert meta == {"version": 1}
    assert body == "上文\n---\n下文\n"


def test_fingerprint_covers_all_nine_prompts_and_versions():
    from scripts.run_eval import agent_prompt_map

    out = fingerprint(agents=agent_prompt_map())
    for name, digest in PRE_MIGRATION_HASHES.items():
        assert out[f"prompt:{name}"] == digest, f"{name} 的指纹与迁移前不一致"
    assert out["prompt_version:summarizer"] and out["prompt_version:data_defense"]
    # 七个角色的类属性 hash 与文件 hash 必须同源相等（这里同时钉住"没有第二份真相"）
    assert out["prompt:planner"] == prompt_hashes()["prompt:planner"]


AGENT_CLASSES = {
    "explorer": "ExplorerAgent",
    "planner": "PlannerAgent",
    "executor": "ExecutorAgent",
    "inspector": "InspectorAgent",
    "visualizer": "VisualizerAgent",
    "reporter": "ReporterAgent",
    "critic": "CriticAgent",
}


@pytest.mark.parametrize("name", sorted(PRE_MIGRATION_HASHES))
def test_fingerprint_is_the_hash_of_what_the_model_actually_sees(name):
    """`prompt:<name>` 必须同时等于"文件正文 hash"和"送进模型的文本 hash"。

    三者同源才谈得上归因：改了文件 → 指纹动 → 分数变化能配对到某个 diff。
    任何一处偷偷加了加工（strip、模板渲染、拼版本头），这里就会红。
    """
    body, _meta = split_frontmatter((DEFAULT_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8"))
    digest = _sha(body)
    assert digest == PRE_MIGRATION_HASHES[name]
    assert prompt_hashes()[f"prompt:{name}"] == digest
    if name in AGENT_CLASSES:
        assert _sha(getattr(agent_module, AGENT_CLASSES[name]).system_prompt) == digest
    elif name == "summarizer":
        from agentflow.core.memory import SUMMARIZER_SYSTEM

        assert _sha(SUMMARIZER_SYSTEM) == digest
