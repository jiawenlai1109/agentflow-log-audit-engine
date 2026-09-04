"""v1.2 机制回归测试：白名单强制 / grants 授权 / 独立校验 / 错误路由与澄清 / 有界历史 / recall 回退。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.agents.planner import PlannerAgent
from agentflow.core.executor import static_scan
from agentflow.core.llm import MockLLM
from agentflow.core.messages import AgentMessage, MessageHistory
from agentflow.core.tools import (
    PathViolationError,
    ToolError,
    build_default_registry,
    ensure_authorized,
)
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"
DATA_PROFIT = PROJECT_ROOT / "demo" / "data" / "retail_sales_with_profit.csv"


class _Ctx:
    def __init__(self, tmp_path):
        self.outputs_dir = tmp_path
        self.data_path = None
        self.transcript = None


# ---------------------------------------------------------------- 白名单运行时强制
def test_whitelist_denies_unauthorized_tool(tmp_path):
    registry = build_default_registry()
    with pytest.raises(ToolError):
        registry.call("planner", "execute_python", _Ctx(tmp_path), code="print(1)", work_dir=tmp_path)


def test_whitelist_allows_authorized_tool(tmp_path):
    data = tmp_path / "d.csv"
    data.write_text("a,销售额\n1,2\n2,3\n", encoding="utf-8")
    ctx = _Ctx(tmp_path)
    ctx.data_path = str(data)
    registry = build_default_registry()
    profile = registry.call("explorer", "profile_csv", ctx, data_path=str(data))
    assert profile["row_count"] == 2


# ---------------------------------------------------------------- grants（依赖边=授权边）
def test_sibling_work_dir_denied(tmp_path):
    ctx = _Ctx(tmp_path)
    (tmp_path / "work" / "1").mkdir(parents=True)
    secret = tmp_path / "work" / "1" / "step_1_result.json"
    secret.write_text("{}", encoding="utf-8")
    with pytest.raises(PathViolationError):
        ensure_authorized(ctx, secret, task_id=2)
    (tmp_path / "work" / "2").mkdir(parents=True)
    own = tmp_path / "work" / "2" / "script.py"
    own.write_text("x", encoding="utf-8")
    assert ensure_authorized(ctx, own, task_id=2) == own.resolve()


def test_cross_task_artifact_requires_grants(tmp_path):
    ctx = _Ctx(tmp_path)
    art = tmp_path / "artifacts"
    art.mkdir()
    f1 = art / "step_1_result.json"
    f1.write_text("{}", encoding="utf-8")
    with pytest.raises(PathViolationError):
        ensure_authorized(ctx, f1, task_id=2)
    assert ensure_authorized(ctx, f1, task_id=2, grants=[str(f1.resolve())]) == f1.resolve()
    assert ensure_authorized(ctx, f1, task_id=None) == f1.resolve()


# ---------------------------------------------------------------- 静态扫描扩展
def test_static_scan_blocks_write_and_relative_escape():
    assert static_scan("df.to_csv('C:/tmp/x.csv')")
    assert static_scan("df.to_csv('../x.csv')")
    assert static_scan("open('../secret')")
    assert static_scan("df.to_excel('D:/x.xlsx')")
    assert not static_scan("df.to_csv('out.csv')")


# ---------------------------------------------------------------- MessageHistory
def test_message_history_bounded_keeps_latest():
    history = MessageHistory(limit=4)
    history.append("user", "u0")
    for i in range(3):
        history.append("assistant", f"a{i}")
        history.append("user", f"u{i + 1}")
    messages = history.to_llm_messages()
    assert len(messages) <= 4
    assert messages[-1]["content"] == "u3"


# ---------------------------------------------------------------- 错误路由 → 重规划 → 澄清
def test_missing_column_routes_to_replan_and_clarify(tmp_path):
    result = run_analysis("分析一下上周的利润情况", str(DATA), outputs_root=tmp_path)
    assert result["status"] == "degraded"
    evaluation = json.loads(
        (Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8")
    )
    assert evaluation["replan_used"] == 1
    assert evaluation["clarify"]
    transcript = (Path(result["outputs_dir"]) / "transcript.jsonl").read_text(encoding="utf-8")
    assert "error_routed" in transcript
    assert "clarify_request" in transcript
    report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
    assert "利润" in report
    assert "建议下一步" in report


# ---------------------------------------------------------------- 独立校验模板
def test_verification_match_check_passes(tmp_path):
    result = run_analysis("总销售额是多少？", str(DATA_PROFIT), outputs_root=tmp_path)
    assert result["status"] == "success"
    transcript = (Path(result["outputs_dir"]) / "transcript.jsonl").read_text(encoding="utf-8")
    assert "aggregate_match_check" in transcript
    assert '"PASS"' in transcript or "PASS" in transcript


# ---------------------------------------------------------------- recall 回退（防误伤）
def test_recall_guard_ignores_irrelevant_history(tmp_path):
    planner = PlannerAgent(llm=MockLLM())
    schema = {
        "columns": [
            {"name": "订单日期", "dtype": "object", "is_date": True},
            {"name": "产品类别", "dtype": "object", "is_date": False},
            {"name": "销售额", "dtype": "float64", "is_date": False},
        ],
        "suggested_date_column": "订单日期",
    }
    ctx = SimpleNamespace(
        schema_profile=schema,
        session=SimpleNamespace(
            read_turns=lambda limit=None: [
                {
                    "turn": 1,
                    "question": "总销售额是多少？",
                    "answer_summary": "总销售额为100",
                    "key_numbers": {"总销售额": 100.0},
                }
            ],
            load_summary=lambda: None,
        ),
        outputs_dir=tmp_path,
        run_id="run_x",
        transcript=None,
        constraints=None,
    )
    message = AgentMessage(
        run_id="run_x",
        sender="orchestrator",
        receiver="planner",
        kind="plan_request",
        content="前面三种商品的对比如何？",
    )
    reply = planner.run(ctx, message)
    plan = json.loads(reply.content)
    assert plan["tasks"]
    assert plan["tasks"][0].get("code_hint") != "memory_answer"