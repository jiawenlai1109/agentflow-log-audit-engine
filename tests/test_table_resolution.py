"""C-14：评测层看不见的"实际递给沙箱的是哪张表"。

三条盲区各自的形状（都曾在产物里不可见，现在都有留痕可断言）：

① **声明了 `primary_ref` 却被 refs 的顺序盖掉**。三源场景里 pack 给每条规则都写了主表
   （T1 必须是 auth.csv），但 `primary_path` 以前取的是 `task_tables()[0]`——refs 顺序一变，
   规则就在错的数据上跑出空结果，而 status 照样 success。
② **角色解析失败后静默退回 Bundle 主表**。声明了两张以上的表却一张都不在 Bundle 里，
   等于"这批数据没有你要的东西"；去读主表就是把"缺表"洗成"用别的表算出来了"。
③ **按 task_id 位置给规则贴标签**。角色解析失败的规则不生成任务，任务号与规则号当场
   错位，`pack.rules[task_id - 1]` 会把 T4 的数字标成 T3——数字对、指代错。

这里既测引擎侧（`dataset_scope`），也测评分侧的两个新谓词
（`table_resolution` / `rule_rollup`）——只修一头，另一头改了照样会悄悄退回去。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.core.dataset_scope import DatasetScopeError, primary_path  # noqa: E402
from agentflow.core.grading import PREDICATES, load_evidence  # noqa: E402
from agentflow.pipeline import run_analysis  # noqa: E402

TRIAGE = [
    "demo/data/triage/auth.csv",
    "demo/data/triage/assets.csv",
    "demo/data/triage/edr.csv",
]
QUESTION = "生产域主机的异常告警有哪些？哪些需要立刻处置"


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, event: dict) -> None:
        self.events.append(event)


def _table(tid: str, filename: str) -> SimpleNamespace:
    return SimpleNamespace(id=tid, source_file=filename, path=f"/abs/{filename}", row_count=10, columns=[])


def _ctx(tables: list[SimpleNamespace], transcript: _Recorder | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        bundle=SimpleNamespace(tables=tables),
        data_path="/abs/first.csv",
        transcript=transcript,
        table_resolutions={},
    )


def _task(refs: list[str], primary: str = "", task_id: int = 1) -> dict:
    return {"task_id": task_id, "dataset_refs": refs, "primary_ref": primary}


# ---------------------------------------------------------------- ①：primary_ref 必须赢过顺序


def test_primary_ref_beats_the_order_of_declared_refs():
    """refs 写成 [t2, t1] 而 primary_ref=t1 ⇒ 递的是 t1。

    旧实现取 `task_tables()[0]`，这里会递 t2（资产台账），T1 就在错的数据上跑出空结果，
    而 status 照样是 success——那正是"跑错了表而产物里一切正常"的盲区。
    """
    tables = [_table("t1", "auth.csv"), _table("t2", "assets.csv")]
    ctx = _ctx(tables)
    path = primary_path(ctx, _task(["t2", "t1"], primary="t1"))
    assert path.endswith("auth.csv"), path
    assert ctx.table_resolutions["1"]["via"] == "primary_ref", ctx.table_resolutions


def test_without_primary_ref_the_declared_order_still_rules():
    """没声明主表时不偷偷改语义：仍按 refs 顺序取第一张（并留下 `declared_order` 的因）。"""
    tables = [_table("t1", "auth.csv"), _table("t2", "assets.csv")]
    ctx = _ctx(tables)
    path = primary_path(ctx, _task(["t2", "t1"]))
    assert path.endswith("assets.csv"), path
    assert ctx.table_resolutions["1"]["via"] == "declared_order", ctx.table_resolutions


# ---------------------------------------------------------------- ②：解析不到不许退回主表


def test_unresolvable_refs_are_refused_not_silently_fallback():
    ctx = _ctx([_table("t1", "auth.csv"), _table("t2", "assets.csv")])
    with pytest.raises(DatasetScopeError) as raised:
        primary_path(ctx, _task(["t8", "t9"], primary="t8"))
    assert "不能退回 Bundle 主表" in str(raised.value), str(raised.value)


def test_single_table_strategy_is_untouched():
    """策略写死：不足两张不收窄。这条守的是"修 ② 时别把单表语义也改了"。"""
    ctx = _ctx([_table("t1", "auth.csv")])
    assert primary_path(ctx, _task(["t1"])) == "/abs/first.csv"
    assert ctx.table_resolutions["1"]["via"] == "bundle_default", ctx.table_resolutions


# ---------------------------------------------------------------- ③：留痕本身（去重、不外泄路径）


def test_resolution_is_recorded_once_per_task_and_leaks_no_path():
    transcript = _Recorder()
    ctx = _ctx([_table("t1", "auth.csv"), _table("t2", "assets.csv")], transcript)
    task = _task(["t1", "t2"], primary="t1")
    for _ in range(3):  # env 组装会把同一任务问多次
        primary_path(ctx, task)
    assert len(ctx.table_resolutions) == 1, ctx.table_resolutions
    assert len(transcript.events) == 1, transcript.events
    event = transcript.events[0]
    assert event["event"] == "table_resolved" and event["table_file"] == "auth.csv", event
    # 绝对路径不进产物（#15 同一口径：文件名是证据，机器上的位置不是）
    assert "/abs/" not in json.dumps(event, ensure_ascii=False), event


# ---------------------------------------------------------------- 接线：产物里真看得见


@pytest.fixture(scope="module")
def sigma_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("resolution")
    result = run_analysis(
        question=QUESTION,
        sources=[str(PROJECT_ROOT / item) for item in TRIAGE],
        mode="mock",
        pack="sigma_triage",
        outputs_root=root,
    )
    return result, load_evidence(Path(result["outputs_dir"]))


def test_the_pack_run_records_every_rule(sigma_run):
    _result, evidence = sigma_run
    resolutions = evidence["evaluation"]["table_resolutions"]
    by_file = {record["table_file"] for record in resolutions.values()}
    assert {"auth.csv", "edr.csv"} <= by_file, resolutions
    # T4 的主表必须是 edr：写成 auth 就是"高危告警伴随失败"这条规则其实没看告警表
    assert PREDICATES["table_resolution"](
        evidence, {"expect": {"T4": {"table_file": "edr.csv", "via": "primary_ref"}}}, "gate"
    )[0], evidence["evaluation"]["table_resolutions"]


def test_table_resolution_predicate_catches_a_wrong_table(sigma_run):
    _result, evidence = sigma_run
    passed, detail = PREDICATES["table_resolution"](
        evidence, {"expect": {"T4": {"table_file": "assets.csv"}}}, "gate"
    )
    assert not passed and "assets.csv" in detail and "期望" in detail, detail


def test_table_resolution_predicate_forbids_the_silent_fallback(sigma_run):
    _result, evidence = sigma_run
    assert PREDICATES["table_resolution"](evidence, {"forbid_via": ["bundle_default"]}, "gate")[0]
    evidence["evaluation"]["table_resolutions"]["9"] = {"via": "bundle_default", "task_id": 9}
    passed, detail = PREDICATES["table_resolution"](evidence, {"forbid_via": ["bundle_default"]}, "gate")
    assert not passed and "bundle_default" in detail, detail


def test_missing_resolution_note_is_red_not_green(sigma_run):
    """留痕没接上线时不许默默通过——那等于把 C-14 的前提取消。"""
    _result, evidence = sigma_run
    blank = dict(evidence)
    blank["evaluation"] = dict(evidence["evaluation"], table_resolutions={})
    passed, detail = PREDICATES["table_resolution"](blank, {"expect": {"T1": {"table_file": "auth.csv"}}}, "gate")
    assert not passed and "没接上线" in detail, detail


# ---------------------------------------------------------------- ③：报告头的规则号必须与账本同源

ROLLUP_REPORT = """# 分诊报告

> 命中统计：T1=1，T3=4，T4=1

## 一、分诊队列
"""


def _rollup_evidence(header: str, ledger: dict[str, int]):
    aggregate = {f"规则{k}命中数": value for k, value in ledger.items()}
    return {
        "report": header,
        "plan": {"tasks": [{"task_id": 1, "code_hint": "rule_pack:T1"}]},
        "evaluation": {
            "results": {"1": {"status": "success", "summary": {"aggregate": aggregate}}}
        },
    }


def test_rollup_catches_a_number_copied_from_the_wrong_rule():
    """数字对、指代错：报告头把 T4 的 1 写成 T3=4 以外的合法值也照样抓（逐条比数值）。"""
    evidence = _rollup_evidence(ROLLUP_REPORT, {"T1": 1, "T3": 4, "T4": 1})
    assert PREDICATES["rule_rollup"](evidence, {}, "gate")[0]
    swapped = _rollup_evidence(
        "# 报告\n\n> 命中统计：T1=1，T3=1，T4=4\n\n## 一、分诊队列\n", {"T1": 1, "T3": 4, "T4": 1}
    )
    passed, detail = PREDICATES["rule_rollup"](swapped, {}, "gate")
    assert not passed and "T3" in detail, detail


def test_rollup_refuses_a_header_that_omits_a_counted_rule():
    evidence = _rollup_evidence(
        "# 报告\n\n> 命中统计：T1=1\n\n## 一、分诊队列\n", {"T1": 1, "T4": 1}
    )
    passed, detail = PREDICATES["rule_rollup"](evidence, {}, "gate")
    assert not passed and "报告头却没报" in detail, detail


def test_rollup_treats_undone_as_not_a_number():
    """`T3=未完成`（#49 修后的样子）不该被抓成"数字不符"——它根本没提供一个数。"""
    evidence = _rollup_evidence(
        "# 报告\n\n> 命中统计：T1=1，T3=未完成\n\n## 一、分诊队列\n", {"T1": 1}
    )
    assert PREDICATES["rule_rollup"](evidence, {}, "gate")[0], evidence
