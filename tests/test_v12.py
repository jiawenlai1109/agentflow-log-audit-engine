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
    from agentflow.core.ingest import build_bundle

    data = tmp_path / "d.csv"
    data.write_text("a,销售额\n1,2\n2,3\n", encoding="utf-8")
    bundle = build_bundle([data], tmp_path / "bd")
    outputs_dir = tmp_path / "run"  # Bundle 在 outputs_dir 之外：只能靠 readable_paths 放行
    ctx = SimpleNamespace(
        outputs_dir=outputs_dir,
        data_path=bundle.primary.path,
        bundle=bundle,
        readable_paths=bundle.readable_paths(),
        transcript=None,
    )
    registry = build_default_registry()
    profile = registry.call("explorer", "profile_bundle", ctx)
    assert profile["row_count"] == 2
    assert profile["tables"][0]["id"] == "t1"


def test_bundle_outside_run_dir_is_allowed_but_strangers_are_not(tmp_path):
    from agentflow.core.ingest import build_bundle

    data = tmp_path / "d.csv"
    data.write_text("a\n1\n", encoding="utf-8")
    bundle = build_bundle([data], tmp_path / "bd")
    stranger = tmp_path / "other.csv"
    stranger.write_text("a\n1\n", encoding="utf-8")
    ctx = SimpleNamespace(
        outputs_dir=tmp_path / "run",
        data_path=bundle.primary.path,
        bundle=bundle,
        readable_paths=bundle.readable_paths(),
        transcript=None,
    )
    ensure_authorized(ctx, bundle.primary.path)
    with pytest.raises(PathViolationError):
        ensure_authorized(ctx, str(stranger))


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

# ---------------------------------------------------------------- 校验器不得误判（real 批次回归）
_SCHEMA = {
    "columns": [
        {"name": "订单日期", "dtype": "object"},
        {"name": "产品类别", "dtype": "object"},
        {"name": "销售额", "dtype": "float64"},
        {"name": "销量", "dtype": "int64"},
    ]
}


def _verify(task, reported, question):
    from agentflow.core.verification import run_verification

    return run_verification(
        task={**task, "_question": question},
        result={"summary": {"aggregate": reported}},
        data_path=str(DATA_PROFIT),
        schema_profile=_SCHEMA,
    )


def test_classify_prefers_count_over_sum_for_order_count():
    """"一共有多少笔订单"含"总"字，但必须判为计数类（real 批次曾误判成 sum 导致假 FAIL）。"""
    from agentflow.core.verification import classify_task

    assert (
        classify_task({"description": "统计一共有多少笔订单 一共有多少笔订单？", "code_hint": ""})
        == "count"
    )


def test_verification_count_not_false_fail():
    outcome = _verify(
        {"description": "统计订单笔数", "code_hint": "count", "required_columns": []},
        {"订单总数": 2000},
        "一共有多少笔订单？",
    )
    assert outcome["status"] == "pass", outcome


def test_verification_respects_time_window():
    """趋势题：对照必须落在同一"最近7天"窗口内，否则会把正确结果判成不一致。"""
    import pandas as pd

    df = pd.read_csv(DATA_PROFIT)
    df["订单日期"] = pd.to_datetime(df["订单日期"])
    cutoff = df["订单日期"].max() - pd.Timedelta(days=6)
    daily = (
        df[df["订单日期"] >= cutoff]
        .groupby(df[df["订单日期"] >= cutoff]["订单日期"].dt.date)["销售额"]
        .sum()
    )
    outcome = _verify(
        {
            "description": "按日期聚合销售额趋势",
            "code_hint": "按日期分组聚合",
            "required_columns": ["订单日期", "销售额"],
        },
        {"合计_销售额": round(float(daily.sum()), 2), "记录条数": float(len(daily))},
        "最近7天每日销售额的走势如何？",
    )
    assert outcome["status"] == "pass", outcome


def test_verification_ignores_non_intersect_metrics():
    """模板没算的指标（此处全列合计被故意写错）不参与对照，只比 Top1 相关项。"""
    import pandas as pd

    df = pd.read_csv(DATA_PROFIT)
    top = df.groupby("产品类别")["销售额"].sum().sort_values(ascending=False)
    outcome = _verify(
        {
            "description": "按类别对比销售额取Top",
            "code_hint": "分组聚合",
            "required_columns": ["产品类别", "销售额"],
        },
        {
            "合计_销售额": 1.0,
            "Top1_销售额": round(float(top.iloc[0]), 2),
            "Top1_类别": str(top.index[0]),
        },
        "哪个产品类别卖得最好？",
    )
    assert outcome["status"] == "pass", outcome


def test_verification_catches_real_mismatch():
    """真错必须抓到：Top1 数值与独立重算不符 → fail。"""
    outcome = _verify(
        {
            "description": "按类别对比销售额取Top",
            "code_hint": "分组聚合",
            "required_columns": ["产品类别", "销售额"],
        },
        {"Top1_销售额": 12345.67, "Top1_类别": "不存在的类别"},
        "哪个产品类别卖得最好？",
    )
    assert outcome["status"] == "fail", outcome


def test_parse_window_days():
    from agentflow.core.verification import parse_window_days

    assert parse_window_days("最近7天每日销售额走势") == 7
    assert parse_window_days("近30天") == 30
    assert parse_window_days("总销售额是多少") is None
