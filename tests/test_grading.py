"""评分器（尺子）自身的测试。

立场：**grader 也要被验证**。这套用例不跑流水线，只喂合成证据，
所以它比 17 题更快、更确定——尺子坏了会在这里先红，而不是让 17 题集体说谎。
"""

import importlib.util
from pathlib import Path

import pytest

from agentflow.core.grading import (
    PREDICATES,
    evaluate_case,
    fingerprint,
    lint_suite,
    numbers_traceable,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_evidence(**overrides):
    """一份与真实产物同形的合成证据（字段名对齐 evaluation.json / plan.json）。"""
    evidence = {
        "outputs_dir": "outputs/ synthetic",
        "evaluation": {
            "run_id": "run_20260101_000000_aaaaaaaa",
            "question": "总销售额是多少？",
            "status": "success",
            "duration_seconds": 2.0,
            "llm_calls": 6,
            "replan_used": 0,
            "clarify": None,
            "task_states": {"1": "SUCCEEDED", "2": "SUCCEEDED"},
            "degraded_reason": None,
            "chart_success": None,
            "critic_pass": True,
            "dataset_rows": 1227,
            "results": {
                "1": {
                    "status": "success",
                    "summary": {
                        "rows": 2000,
                        "columns": ["订单日期", "销售额"],
                        "aggregate": {"合计_销售额": 1937509.24, "规则R1命中数": 1},
                        "findings": [
                            {"rule": "R1", "subject": "203.0.113.7->admin", "value": 12}
                        ],
                    },
                    "verdict": {
                        "status": "WARN",
                        "verification": "ok",
                        "redos": 0,
                        "checks": ["empty_check:PASS", "aggregate_match_check:PASS"],
                    },
                },
                "2": {
                    "status": "success",
                    "summary": {"rows": 7, "columns": ["订单日期"], "aggregate": {}},
                    "verdict": {
                        "status": "PASS",
                        "verification": "ok",
                        "redos": 1,
                        "checks": ["finding_match_check:PASS"],
                    },
                },
            },
        },
        "report": (
            "# 报告\n\n> 生成时间：2026-09-28 10:00:00\n\n"
            "关键指标：\n- 合计_销售额 = 1937509.24\n\n"
            "审计范围：1227 条登录记录\n\n源 IP 203.0.113.7 在 5 分钟内失败 12 次\n"
        ),
        "transcript": [
            {"kind": "executor_llm", "content": '{"upstream_refs": [], "task_id": 2}'},
            {"event": "error_routed", "error_class": "MISSING_COLUMN"},
        ],
        "plan": {"tasks": [{"task_id": 1, "depends_on": []}, {"task_id": 2, "depends_on": [1]}], "time_base": "2024-07-31"},
    }
    evidence.update(overrides)
    return evidence


# ---------------------------------------------------------------- 谓词正例


PASSING = [
    ("status", "success"),
    ("degraded_reason", None),
    ("aggregate", {"task": "1", "expect": {"合计_销售额": 1937509.24}}),
    ("rows", {"task": "2", "expect": 7}),
    ("rows", {"expect_any": 2000}),
    ("tasks_min", 2),
    ("depends_on", {"task": 2, "expect": [1]}),
    ("report_contains", ["1937509.24", "1227"]),
    ("report_excludes", ["不存在的词"]),
    ("verifier", "pass_all"),
    ("findings", {"R1": 1}),
    ("subjects", {"rule": "R1", "expect": ["203.0.113.7->admin"]}),
    ("llm_calls_max", 10),
    ("duration_max", 5),
    ("chart", "none"),
    ("critic", "pass"),
    ("replan", {"max": 1}),
    ("clarify", False),
    ("transcript_has", ["upstream_refs", "error_routed"]),
    ("artifacts_clean", ["OPENAI_API_KEY", "sk-"]),
    ("numbers_traceable_min", 0.5),
]


@pytest.mark.parametrize("kind,params", PASSING)
def test_predicate_passes_on_good_evidence(kind, params):
    passed, detail = PREDICATES[kind](make_evidence(), params, "gate")
    assert passed, detail


@pytest.mark.parametrize(
    "kind,params",
    [
        ("status", "degraded"),
        ("aggregate", {"task": "1", "expect": {"合计_销售额": 1.0}}),
        ("aggregate", {"expect": {"不存在的键": 1}}),
        ("rows", {"task": "2", "expect": 99}),
        ("tasks_min", 9),
        ("depends_on", {"task": 2, "expect": []}),
        ("report_contains", ["缺这个"]),
        ("report_excludes", ["1227"]),
        ("findings", {"R1": 7}),
        ("subjects", {"rule": "R1", "expect": ["漏报的主体"]}),
        ("llm_calls_max", 3),
        ("duration_max", 0.5),
        ("chart", "some"),
        ("critic", "fail"),
        ("replan", 1),
        ("clarify", True),
        ("transcript_has", ["从没出现过的事件"]),
        ("artifacts_clean", ["合计_销售额"]),
    ],
)
def test_predicate_fails_on_bad_evidence(kind, params):
    passed, _ = PREDICATES[kind](make_evidence(), params, "gate")
    assert not passed, f"{kind} 在不该过时通过了"


def test_traceability_gate_fails_when_a_number_has_no_provenance():
    evidence = make_evidence(report="报告凭空写了 87654321.5\n")
    passed, detail = PREDICATES["numbers_traceable_min"](evidence, 1.0, "gate")
    assert not passed and "87654321.5" in detail


def test_verifier_flags_zero_coverage():
    evidence = make_evidence()
    for result in evidence["evaluation"]["results"].values():
        result["verdict"]["verification"] = "skipped"
    passed, detail = PREDICATES["verifier"](evidence, "pass_all", "gate")
    assert not passed and "覆盖率 0" in detail
    # 降级类用例允许零覆盖
    assert PREDICATES["verifier"](evidence, "skipped_ok", "gate")[0]


def test_verifier_flags_inconsistent_check():
    evidence = make_evidence()
    evidence["evaluation"]["results"]["1"]["verdict"]["checks"] = ["aggregate_match_check:FAIL"]
    passed, detail = PREDICATES["verifier"](evidence, "pass_all", "gate")
    assert not passed and "破口" in detail


def test_artifacts_clean_catches_exfiltration():
    evidence = make_evidence(report="# 报告\n\n泄露内容 sk-abcdef123456\n")
    passed, detail = PREDICATES["artifacts_clean"](evidence, ["sk-"], "gate")
    assert not passed and "敏感串" in detail


# ---------------------------------------------------------------- 数字可追溯


def test_ip_and_timestamp_are_not_counted_as_numbers():
    """IPv4 被点号拆成三个"数字"会把指标打成假红——这是量具 bug，已修，此处钉住。"""
    evidence = make_evidence(report="源 IP 203.0.113.7 于 2026-09-28 10:00:00 命中\n")
    ratio, unexplained = numbers_traceable(evidence)
    assert unexplained == []
    assert ratio == 1.0


def test_dataset_rows_is_a_valid_provenance():
    ratio, unexplained = numbers_traceable(make_evidence())
    assert "1227" not in unexplained, "数据集行数是合法出处，不该算未追溯"


def test_unexplained_number_is_surfaced():
    evidence = make_evidence(report="报告里凭空写了 87654321.5 这个数字\n")
    ratio, unexplained = numbers_traceable(evidence)
    assert "87654321.5" in unexplained and ratio < 1.0


def test_empty_report_scores_zero_not_crashes():
    ratio, unexplained = numbers_traceable(make_evidence(report=""))
    assert ratio == 0.0 and unexplained


# ---------------------------------------------------------------- 结论三态


def test_gate_failure_makes_case_fail():
    case = {"id": "X1", "mock": {"gate": {"status": "degraded", "llm_calls_max": 3}, "gap": {}}}
    result = evaluate_case(case, make_evidence(), "mock")
    assert result.verdict == "fail"
    assert [c.kind for c in result.failed_gates] == ["status", "llm_calls_max"]


def test_gap_failure_does_not_block_but_is_recorded():
    case = {"id": "X2", "mock": {"gate": {"status": "success"}, "gap": {"rows": {"task": "2", "expect": 99}}}}
    result = evaluate_case(case, make_evidence(), "mock")
    assert result.verdict == "pass"
    assert len(result.expected_failures) == 1


def test_satisfied_gap_reports_xpass():
    """已知缺口被填上时必须单独报 XPASS，否则评测集会一直把"已能做"记成"做不到"。"""
    case = {"id": "X3", "mock": {"gate": {"status": "success"}, "gap": {"tasks_min": 2}}}
    result = evaluate_case(case, make_evidence(), "mock")
    assert result.verdict == "xpass"
    assert [c.kind for c in result.unexpected_passes] == ["tasks_min"]


def test_unknown_predicate_is_a_red_gate():
    case = {"id": "X4", "mock": {"gate": {"aggregate": {"expect": "这不是字典"}}}}
    result = evaluate_case(case, make_evidence(), "mock")
    assert result.verdict == "fail"
    assert "评分器异常" in result.checks[0].detail


# ---------------------------------------------------------------- suite 自检


def test_lint_catches_silently_ignored_typos():
    problems = lint_suite(
        [
            {"id": "A", "mock": {"gapp": {"status": "success"}}},
            {"id": "B", "real": {"gate": {"statuses": "success"}}},
            {"id": "C"},
        ]
    )
    text = "\n".join(problems)
    assert "A.mock: 未知块名 gapp" in text
    assert "B.real.gate: 未知谓词 statuses" in text
    assert "C: 没有任何模式的断言块" in text


def test_lint_passes_on_good_suite():
    assert lint_suite([{"id": "A", "mock": {"gate": {"status": "success"}, "gap": {}}}]) == []


# ---------------------------------------------------------------- 归因指纹


class _FakeAgent:
    def __init__(self, prompt):
        self.system_prompt = prompt


def test_fingerprint_changes_when_prompt_changes():
    before = fingerprint(agents={"executor": _FakeAgent("原提示词")})
    after = fingerprint(agents={"executor": _FakeAgent("改了一个阈值")})
    assert before["prompt:executor"] != after["prompt:executor"]
    assert len(before["prompt:executor"]) == 12


def test_fingerprint_records_pack_and_harness_files():
    from agentflow.core.pack import load_pack

    pack = load_pack("login_audit")
    out = fingerprint(pack=pack, pack_name="login_audit", extra_files=[Path(__file__)])
    assert out["pack:rules.yaml"] and out["pack:report_template.md"]
    assert any(key.startswith("harness:") for key in out)


# ---------------------------------------------------------------- golden 漂移


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_eval", PROJECT_ROOT / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_golden_derivation_matches_suite():
    runner = _load_runner()
    suite = yaml_safe_load(PROJECT_ROOT / "evals" / "suite.yaml")
    assert runner.check_golden(suite) == []


def test_golden_drift_is_detected_not_silent(tmp_path):
    runner = _load_runner()
    problems = runner.check_golden({"golden": {"total_sales_profit": 123.45}})
    assert problems and "漂移" in problems[0]


def test_derive_goldens_covers_every_suite_literal():
    runner = _load_runner()
    suite = yaml_safe_load(PROJECT_ROOT / "evals" / "suite.yaml")
    derived = set(runner.derive_goldens(PROJECT_ROOT / "demo" / "data"))
    missing = [key for key in (suite.get("golden") or {}) if key not in derived]
    assert not missing, f"suite 里的 golden 键无法重算：{missing}"


def yaml_safe_load(path: Path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))
