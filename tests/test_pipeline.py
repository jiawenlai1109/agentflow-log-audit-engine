"""集成测试：TC-01 / TC-02 / TC-03（Mock 模式，离线验收）。"""

import json
from pathlib import Path

from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"


def test_tc01_total_sales_success(tmp_path):
    result = run_analysis("总销售额是多少？", str(DATA), outputs_root=tmp_path)
    assert result["status"] == "success"
    report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
    assert "关键指标" in report


def test_tc02_trend_with_chart(tmp_path):
    result = run_analysis("最近7天每日销售额的走势如何？", str(DATA), outputs_root=tmp_path)
    assert result["status"] == "success"
    evaluation = json.loads(
        (Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8")
    )
    assert evaluation["chart_success"] is not None
    assert evaluation["chart_success"] >= 1


def test_tc03_missing_profit_degraded(tmp_path):
    result = run_analysis("分析一下上周的利润情况", str(DATA), outputs_root=tmp_path)
    assert result["status"] == "degraded"
    assert result["report"]["degraded"] is True
    report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
    assert "利润" in report


def test_mock_session_recall_without_recompute(tmp_path):
    """记忆回答：第二轮回忆第一轮结论，关键数字来自记忆而非重算。"""
    session_id = "sess_recall_test"
    first = run_analysis(
        "总销售额是多少？", str(DATA), outputs_root=tmp_path, session_id=session_id
    )
    assert first["status"] == "success"
    second = run_analysis(
        "我之前问的第一个问题的答案是什么？",
        str(DATA),
        outputs_root=tmp_path,
        session_id=session_id,
    )
    assert second["status"] == "success"
    report = Path(second["report"]["report_path"]).read_text(encoding="utf-8")
    assert "1950562" in report
    summary = json.loads(
        (tmp_path / "sessions" / session_id / "summary.json").read_text(encoding="utf-8")
    )
    assert "key_findings" in summary
