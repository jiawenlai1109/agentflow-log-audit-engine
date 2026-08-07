"""Inspector 上下文规则 与 Critic 数值比对 的单元测试。"""

from agentflow.core.tools import _check_report, _validate_rules


def _ctx(tmp_path):
    class Ctx:
        outputs_dir = tmp_path
        data_path = None

    return Ctx()


def test_empty_result_with_filter_hint_is_warn(tmp_path):
    checks = _validate_rules(
        _ctx(tmp_path),
        {"summary": {"rows": 0, "columns": ["销售额"]}},
        {"task_id": 1, "required_columns": ["销售额"]},
        "某天华东地区的销售额",
        {"row_count": 100},
    )
    assert all(check["level"] != "FAIL" for check in checks)
    assert any(check["level"] == "WARN" for check in checks)


def test_empty_result_without_filter_is_fail(tmp_path):
    checks = _validate_rules(
        _ctx(tmp_path),
        {"summary": {"rows": 0, "columns": ["销售额"]}},
        {"task_id": 1, "required_columns": ["销售额"]},
        "总销售额是多少？",
        {"row_count": 100},
    )
    assert any(check["level"] == "FAIL" for check in checks)


def test_critic_numeric_compare_tolerates_format(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("合计为 1950562.47", encoding="utf-8")
    results = {"1": {"summary": {"aggregate": {"合计_销售额": 1950562.47}, "head": []}}}
    issues = _check_report(_ctx(tmp_path), report, "总销售额是多少？", results)
    assert not any("关键数字" in issue["message"] for issue in issues)


def test_critic_numeric_compare_detects_missing(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("报告中没有数字", encoding="utf-8")
    results = {"1": {"summary": {"aggregate": {"合计_销售额": 1950562.47}, "head": []}}}
    issues = _check_report(_ctx(tmp_path), report, "总销售额是多少？", results)
    assert any("关键数字" in issue["message"] for issue in issues)
