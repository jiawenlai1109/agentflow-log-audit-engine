"""M3-3：报告分档的确定性检查（core/report_lint.py）与它两侧的消费方。

写这批用例时先问的是"这把尺子能不能咬"：每条正向断言都配一条对应的负向断言
（把报告改坏 ⇒ 必须判红），否则"绿"只说明我没检查。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.core.grading import _p_report_layers, load_evidence
from agentflow.core.pack import load_pack
from agentflow.core.report_lint import (
    declared_layers,
    lint_report,
    scan_numbers,
    sections,
)
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ATTACK = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"
TRIAGE = PROJECT_ROOT / "demo" / "data" / "triage"

LAYERS = {"fact": ["分诊队列"], "action": ["处置建议"], "inference": ["研判摘要"]}
FINDINGS = [
    {"rule_id": "T1", "subject": "203.0.113.7->admin", "value": 9},
    {"rule_id": "T3", "subject": "10.0.0.7", "value": 7},
]

GOOD_REPORT = """# 分诊报告

> 生成时间：2026-09-28 18:00:00
> 命中统计：T1=1，T3=1

## 一、分诊队列（事实层）

| 优先级 | 严重级 | 规则 | 主体 | 指标 | 数值 |
| :--- | :--- | :--- | :--- | :--- | ---: |
| 1 | critical | 生产域大量失败（T3） | 10.0.0.7 | 失败次数 | 7 |
| 2 | high | 认证爆破（T1） | 203.0.113.7->admin | 失败次数 | 9 |

## 二、处置建议

- **T3 10.0.0.7**：立即隔离该主机网络出口。
- **T1 203.0.113.7->admin**：建议封禁该源 IP 并重置口令。

## 三、研判摘要（推断层）

【态势研判】共命中 2 条发现（T1×1、T3×1），最高严重级为 critical（T3 10.0.0.7 失败 7 次）。

## 四、分诊说明

- 阈值取自实测背景噪声。
"""


@pytest.fixture(scope="module")
def pack_sigma():
    return load_pack("sigma_triage")


def _issues(text: str, findings=FINDINGS, layers=LAYERS, thresholds=()) -> list[dict]:
    return lint_report(text, findings, layers, thresholds)


# ------------------------------------------------------------ 正向：结构完整就应当一声不响


def test_lint_accepts_a_well_layered_report():
    assert _issues(GOOD_REPORT) == []


def test_header_and_evidence_numbers_count_as_sources():
    """头部引用块与事实层一样是出处（"命中统计：T1=1，T3=1"就在头部）。"""
    text = GOOD_REPORT.replace("共命中 2 条发现", "共命中 2 条发现（较昨日 1200 条腰斩）")
    issues = _issues(text)
    assert len(issues) == 1 and "数字 1200" in issues[0]["message"], issues


def test_dotted_hosts_and_timestamps_are_not_treated_as_numbers():
    """IP/主机名与时间戳不构成"结论数字"——不先挖掉，每一条证据行都会变成假红。"""
    text = GOOD_REPORT.replace(
        "【态势研判】共命中 2 条发现",
        "【态势研判】10.0.0.7 与 203.0.113.7 在 2026-09-05 21:00:00 前后异常，共命中 2 条发现",
    )
    assert _issues(text) == []


def test_large_rule_thresholds_are_legitimate_sources():
    """研判里复述规则阈值（"≥8000 次"）是引用包内标准，不是造数——所以阈值要进允许池。"""
    text = GOOD_REPORT.replace("共命中 2 条发现", "共命中 2 条发现，均越过阈值 8000")
    assert _issues(text, thresholds=(8000, 3, 2)) == []
    assert any("数字 8000" in i["message"] for i in _issues(text))  # 不给阈值池就判红


def test_small_integers_are_deliberately_not_claims():
    """把口径的**边界**钉在案上：小整数不算结论，所以"3 台"这类造假本检查抓不到。

    记下来是防止将来把它当成完备防线——语义层造假归 LLM 评审，理由见 report_lint 的 `_is_claim`。
    """
    text = GOOD_REPORT.replace("最高严重级为 critical", "涉及 3 台主机，最高严重级为 critical")
    assert _issues(text) == []


# ------------------------------------------------------------ 负向：每种破口都要咬得住


def test_missing_action_entry_is_caught():
    text = GOOD_REPORT.replace("- **T1 203.0.113.7->admin**：建议封禁该源 IP 并重置口令。\n", "")
    issues = _issues(text)
    assert len(issues) == 1 and "处置建议" in issues[0]["message"] and "203.0.113.7->admin" in issues[0]["message"]
    assert issues[0]["severity"] == "high"


def test_missing_fact_row_is_caught():
    text = GOOD_REPORT.replace(
        "| 1 | critical | 生产域大量失败（T3） | 10.0.0.7 | 失败次数 | 7 |\n", ""
    )
    issues = [issue for issue in _issues(text) if "事实层" in issue["section"] or "未出现在事实层" in issue["message"]]
    assert issues and "10.0.0.7" in issues[0]["message"]


def test_invented_number_in_inference_layer_is_caught():
    text = GOOD_REPORT.replace("最高严重级为 critical", "涉及 312 台主机，最高严重级为 critical")
    issues = _issues(text)
    assert len(issues) == 1
    assert "推断层出现事实层没有的数字 312" in issues[0]["message"]
    assert issues[0]["severity"] == "high"


def test_missing_layer_heading_is_caught():
    """档名对不上模板 ⇒ 报告根本没有那一档。不报出来的话，检查会静默变成空检查。"""
    text = GOOD_REPORT.replace("## 三、研判摘要（推断层）", "## 三、小结")
    issues = _issues(text)
    assert any("缺少inference档" in issue["message"] for issue in issues), issues


def test_sections_splitter_keeps_header_as_its_own_block():
    parsed = sections(GOOD_REPORT)
    assert parsed[0][0] == "头部"
    assert "命中统计" in parsed[0][1]
    assert any(heading.startswith("## 三、") for heading, _ in parsed)


def test_scan_numbers_strips_meta_tokens():
    """时间戳、点分四段与小整数都不进池子；留下的才是"构成结论的数字"。"""
    assert scan_numbers("2026-09-05 21:00:00 与 10.0.0.7，命中 2 条") == set()
    assert scan_numbers("2026-09-05 21:00:00 与 10.0.0.7，合计 1200.5 条") == {1200.5}


# ------------------------------------------------------------ 包侧：承诺必须与模板一致


def test_packs_declare_layers_that_exist_in_templates():
    for name in ("sigma_triage", "login_audit"):
        pack = load_pack(name)
        layers = declared_layers(pack)
        assert layers and set(layers) == {"fact", "action", "inference"}, name
        for names in layers.values():
            for layer in names:
                assert layer in pack.report_template, (name, layer)


def test_pack_with_layer_missing_from_template_fails_to_load(tmp_path, monkeypatch):
    """声明了模板里没有的档 ⇒ 装载即报错。写成 ValueError 而不是 warning：这种包永远出不了合格报告。"""
    from agentflow.core import pack as pack_module

    broken = tmp_path / "broken_pack"
    broken.mkdir()
    source = PROJECT_ROOT / "packs" / "login_audit"
    text = source.joinpath("rules.yaml").read_text(encoding="utf-8")
    text = text.replace("  fact: [发现清单]", "  fact: [根本不存在的档]")
    (broken / "rules.yaml").write_text(text, encoding="utf-8")
    (broken / "report_template.md").write_text(
        source.joinpath("report_template.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(pack_module, "PACKS_DIR", tmp_path)
    with pytest.raises(ValueError, match="report_layers"):
        pack_module.load_pack("broken_pack")


# ------------------------------------------------------------ 两侧口径一致（Critic 与评分器）


@pytest.mark.parametrize(
    "pack_name,question,sources",
    [
        ("login_audit", "对2026-09-05的登录日志做安全审计", [ATTACK]),
        ("sigma_triage", "生产域主机的异常告警有哪些", [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"]),
    ],
)
def test_real_runs_pass_the_layer_gate(pack_name, question, sources, tmp_path):
    result = run_analysis(
        question, [str(path) for path in sources], outputs_root=tmp_path, pack=pack_name
    )
    assert result["status"] == "success", result.get("degraded_reason")
    evidence = load_evidence(result["outputs_dir"])
    passed, detail = _p_report_layers(evidence, {"pack": pack_name}, "gate")
    assert passed, detail

    # Critic 侧走的是同一份实现：报告没问题时，运行时评审不该因为分档破口把整轮判 FAIL
    # （evaluation.json 只落 `critic_pass` 结论，问题清单不落盘 ⇒ 这里断言结论而非清单）
    assert (evidence["evaluation"] or {}).get("critic_pass") is True, json.dumps(
        {
            "pack": pack_name,
            "report": evidence["report"][:400],
            "lint": _issues(evidence["report"], [], declared_layers(load_pack(pack_name))),
        },
        ensure_ascii=False,
    )


def test_gate_bites_when_the_report_loses_a_layer(tmp_path):
    """把渲染好的报告改坏（删掉处置建议整节）⇒ 评分器必须判红，否则这条闸门是摆设。"""
    result = run_analysis(
        "生产域主机的异常告警有哪些",
        [str(TRIAGE / "auth.csv"), str(TRIAGE / "assets.csv"), str(TRIAGE / "edr.csv")],
        outputs_root=tmp_path,
        pack="sigma_triage",
    )
    report_path = Path(result["report"]["report_path"])
    text = report_path.read_text(encoding="utf-8")
    head, _, tail = text.partition("## 三、处置建议")
    kept = head + tail.partition("## 四、")[2]
    report_path.write_text(kept, encoding="utf-8")
    evidence = load_evidence(result["outputs_dir"])
    passed, detail = _p_report_layers(evidence, {"pack": "sigma_triage"}, "gate")
    assert not passed and "处置建议" in detail


def test_critic_side_surfaces_the_same_breaks(tmp_path):
    """Critic 侧（运行时的 `_check_report`）必须报出同一个破口：两侧共用一份实现，不许一边红一边绿。"""
    from agentflow.core.tools import _check_report

    broken = GOOD_REPORT.replace("- **T1 203.0.113.7->admin**：建议封禁该源 IP 并重置口令。\n", "")
    report = tmp_path / "report.md"
    report.write_text(broken, encoding="utf-8")
    ctx = SimpleNamespace(outputs_dir=tmp_path, data_path=None, pack=load_pack("sigma_triage"))
    results = {"1": {"summary": {"findings": FINDINGS, "aggregate": {}}}}
    issues = _check_report(ctx, str(report), "q", results, sections=["分诊队列", "处置建议", "研判摘要"])
    assert any("处置建议" in issue["message"] and "203.0.113.7->admin" in issue["message"] for issue in issues), issues
    assert any(issue["severity"] == "high" for issue in issues)


def test_rule_rollup_does_not_label_by_task_position(pack_sigma):
    """缺陷复现路径：抽掉一条规则（不生成任务）⇒ 任务号与规则号错位，标签不许跟着错位。

    旧实现按 `pack.rules[task_id - 1]` 对号：T3 缺席时任务 2 的"规则T4命中数=1"
    会被标成"规则T3：…"，数字对、指代错——和"24 条认证日志"是同一类缺陷。
    """
    from agentflow.agents.reporter import ReporterAgent

    results = {
        "1": {
            "status": "success",
            "summary": {
                "aggregate": {"规则T1命中数": 1},
                "findings": [{"rule_id": "T1", "subject": "203.0.113.7->admin", "value": 9}],
            },
        },
        "2": {
            "status": "success",
            "summary": {
                "aggregate": {"规则T4命中数": 1},
                "findings": [{"rule_id": "T4", "subject": "10.0.0.15", "value": 3}],
            },
        },
    }
    findings = [f for row in results.values() for f in row["summary"]["findings"]]
    stats, silent, aggregates = ReporterAgent._rule_rollup(pack_sigma, results, findings)
    # 这一行以前写的是 `T3=0`——它和下一条断言（T3 必须被点名为未出结论）本来就互相打脸，
    # 那就是缺陷 #49 的形状：测试把模板的假 0 当成了预期值。
    assert stats == "T1=1，T3=未完成，T4=1"
    assert silent == ["T3"], "T3 没生成任务 ⇒ 必须被点名为未出结论，而不是当作无风险"
    # 逐字核对：旧实现按位置贴标签，这里会写成"规则T3：规则T4命中数=1"
    assert aggregates == "规则T1命中数=1；规则T4命中数=1"


# ---------------------------------------------------------------- 缺陷 #49：没跑成的规则不许印成 0
#
# 现场（2026-10-07 real E23）：报告头同时写着两句话——
#   > 命中统计：T1=1，T3=0，T4=1
#   > ⚠️ 未能完成的规则：T3（这些规则的结论缺失，不得当作"无风险"）
# T3 的任务白卷失败了，`0` 是模板替它编的结论。SOC 值班读第一行就够了，而第一行是假的。
# 这一族缺陷的通用形状：**"没做"被印成"做了且没问题"**。两侧都钉：
# 生产侧（`_rule_rollup`）不许再产出 0，校验侧（`lint_report`）独立拦自相矛盾——
# 只修一头，另一头改了就会悄悄退回去。


def _silent_header(stats_line: str, declared: str) -> str:
    return GOOD_REPORT.replace(
        "> 命中统计：T1=1，T3=1",
        f"> 命中统计：{stats_line}\n> ⚠️ 未能完成的规则：{declared}（这些规则的结论缺失，不得当作\u201c无风险\u201d）",
    )


def test_declaring_a_rule_unfinished_while_counting_it_zero_is_an_issue():
    issues = _issues(_silent_header("T1=1，T3=0，T4=1", "T3"))
    hit = [item for item in issues if "T3" in item["message"] and "无风险" in item["message"]]
    assert hit, issues
    assert hit[0]["severity"] == "high", hit[0]


def test_printing_undone_instead_of_zero_is_not_an_issue():
    """修好之后的样子：同一份自相矛盾不再存在，校验侧应当一声不响。"""
    assert _issues(_silent_header("T1=1，T3=未完成，T4=1", "T3")) == []


def test_zero_for_a_rule_that_finished_is_still_legal():
    """别把这条修成"0 一律不许出现"：真查过、真没命中，0 就是结论。"""
    assert _issues(GOOD_REPORT.replace("T1=1，T3=1", "T1=1，T3=0")) == []


def test_rollup_prints_undone_for_a_rule_that_never_spoke(pack_sigma):
    """生产侧的确定性单测：没有 aggregate、没有 finding 的规则，不许在命中统计里拿 0。"""
    from agentflow.agents.reporter import ReporterAgent

    results = {
        "1": {"summary": {"aggregate": {"规则T1命中数": 1}, "findings": [{"rule_id": "T1", "subject": "10.0.0.7", "value": 5}]}},
        # T3 的那条任务白卷失败：results 里根本没有它的 summary
        "3": {"summary": {"aggregate": {"规则T4命中数": 1}, "findings": [{"rule_id": "T4", "subject": "10.0.0.15", "value": 3}]}},
    }
    findings = [f for entry in results.values() for f in entry["summary"]["findings"]]
    stats_text, silent, _agg = ReporterAgent._rule_rollup(pack_sigma, results, findings)
    assert "T3=未完成" in stats_text, stats_text
    assert "T3=0" not in stats_text, stats_text
    assert silent == ["T3"], silent
    # 修完的产出必须过得了校验侧——两侧同向才算接上线
    assert _issues(_silent_header(stats_text, "、".join(silent))) == []
