"""表级授权闸门的**每跑自检**（#19 的门禁侧）。

这道闸门 2026-09-28 就实现了（`tools._ensure_table_granted` + `tool_denied_table` 留痕），
但评测层一直看不见它：mock 规划器不会生成"读未声明表"的任务，于是 27 题里没有任何一题
能因为"闸门被摘掉"而变红。现在换成引擎自己在每次运行收尾拿一张未声明的表试一次——
**闸门接没接线，从此每次运行都有记录可查**，而不是靠读代码相信它在。

三条立场所测的东西：
- 三种状态分开（passed / skipped / violated），skipped 不许伪装成通过；
- 行为与留痕必须是同一次：判 passed 而 transcript 里没有带 probe 标记的拒绝事件 ⇒ 分叉；
- 探针只被拒、不改判：健康运行不许因为自检被打成 degraded，探针拿到的内容一律不落盘。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from agentflow.core.grading import PREDICATES, guard_state, load_evidence
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIAGE = PROJECT_ROOT / "demo" / "data" / "triage"
LOGIN = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"

TRIAGE_QUESTION = "生产域主机的异常告警有哪些？哪些需要立刻处置"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("run_eval", PROJECT_ROOT / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _events(outputs_dir: str | Path) -> list[dict[str, Any]]:
    lines = (Path(outputs_dir) / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _record(result: dict[str, Any]) -> dict[str, Any]:
    """自检结论落在产物里，不在 `run_analysis` 的返回体里——**事实层优先**。

    测试跟门禁读同一份 `evaluation.json`，避免"返回体有、产物没有"这种只在测试里绿的形状。
    """
    payload = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    return payload.get("guard_selfcheck") or {}


def _triage_run(tmp_path: Path, *sources: Path) -> dict[str, Any]:
    return run_analysis(
        question=TRIAGE_QUESTION,
        sources=[str(path) for path in sources],
        mode="mock",
        outputs_root=tmp_path,
        pack="sigma_triage",
    )


# ---------------------------------------------------------------- 引擎侧：每次运行都测


def test_multi_table_run_proves_the_gate_is_wired(tmp_path):
    """三源分诊 run：探针点名最后一张表，必须被拒且说清"没被声明"。"""
    result = _triage_run(tmp_path, TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv")
    record = _record(result)
    assert record["ran"] is True and record["state"] == "passed", record
    assert record["tables"] >= 2
    assert record["denied_table"] not in record["declared_refs"], "探针自己把权限点全了 = 假通过"

    denied = [
        event
        for event in _events(result["outputs_dir"])
        if event.get("event") == "tool_denied_table" and event.get("probe") == "table_guard_selfcheck"
    ]
    assert denied, "自检判 passed 却没有留痕：行为与审计分叉了"
    assert denied[-1]["declared_refs"] == record["declared_refs"]


def test_probe_marker_is_what_keeps_the_record_honest(tmp_path):
    """探针必须自证是探针。

    没有 `probe` 标记，transcript 里就是一条"executor 试图读未声明的表"——把系统对自己的
    测试写成对模型的指控。这条用例断的是事件形状，不是它能不能读到数据。
    """
    result = _triage_run(tmp_path, TRIAGE / "auth.csv", TRIAGE / "edr.csv", TRIAGE / "assets.csv")
    event = [e for e in _events(result["outputs_dir"]) if e.get("event") == "tool_denied_table"][-1]
    assert event["probe"] == "table_guard_selfcheck"
    # 留痕只记"试了哪条路径"，不记内容：探针万一被放行，返回值也不许进事实层
    assert set(event) <= {"event", "agent", "tool", "task_id", "declared_refs", "path", "reason", "probe"}, sorted(event)


def test_single_table_run_says_it_was_not_testable(tmp_path):
    """单表 run 记 skipped 并给出原因，不假装绿灯。

    `dataset_scope` 的策略是"声明不足两张就不收窄"，所以那种 run 里不存在未声明的表；
    把它记成 passed 等于谎报覆盖面，记成 violated 又是在报一个不存在的缺陷。
    """
    result = run_analysis(
        question="对今天的登录日志做安全审计",
        sources=str(LOGIN),
        mode="mock",
        outputs_root=tmp_path,
        pack="login_audit",
    )
    record = _record(result)
    assert record["state"] == "skipped" and record["tables"] == 1, record
    assert "表级收窄" in record["reason"]


def test_selfcheck_never_degrades_a_healthy_run(tmp_path):
    """自检是观察者不是当事人：它不许改变任何判定。"""
    result = _triage_run(tmp_path, TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv")
    assert result["status"] == "success", result["status"]
    assert result["task_states"], "任务该跑完还是跑完了"


def test_degraded_run_still_carries_the_record(tmp_path):
    """自检挂在 `finally`：缺表降级的运行同样要有这条记录（E25 那一型）。"""
    result = _triage_run(tmp_path, TRIAGE / "auth.csv", TRIAGE / "edr.csv")
    assert result["status"] == "degraded"
    record = _record(result)
    assert record["ran"] is True and record["state"] == "passed", record


# ---------------------------------------------------------------- 门禁侧：谓词与下限


def _evidence(record: dict[str, Any] | None, *, with_probe_event: bool = True) -> dict[str, Any]:
    transcript: list[dict[str, Any]] = []
    if with_probe_event:
        transcript.append(
            {
                "event": "tool_denied_table",
                "probe": "table_guard_selfcheck",
                "declared_refs": (record or {}).get("declared_refs", []),
            }
        )
    return {"evaluation": {"guard_selfcheck": record} if record else {}, "transcript": transcript, "report": "", "plan": []}


_GOOD = {"ran": True, "state": "passed", "tables": 3, "declared_refs": ["t1", "t2"], "denied_table": "t3", "reason": "表级越权"}


def test_predicate_matrix_covers_all_four_states():
    predicate = PREDICATES["table_denied"]

    ok, detail = predicate(_evidence(_GOOD), "passed", "mock")
    assert ok, detail

    ok, detail = predicate(_evidence({"ran": True, "state": "skipped", "tables": 1, "reason": "不做表级收窄"}), "skipped", "mock")
    assert ok, detail

    violated = {"ran": True, "state": "violated", "tables": 3, "declared_refs": ["t1"], "denied_table": "t3", "reason": "越表读被放行"}
    ok, detail = predicate(_evidence(violated), "passed", "mock")
    assert not ok and "violated" in detail

    # 第四态：整栏没了 = 自检被摘掉，不能当成"没测到"
    ok, detail = predicate(_evidence(None), "passed", "mock")
    assert not ok and guard_state({}) == "missing"


def test_predicate_refuses_a_pass_without_the_record():
    """passed 却没有带标记的拒绝事件 ⇒ 判红。留痕不能替行为作证，反过来也不行。"""
    ok, detail = PREDICATES["table_denied"](_evidence(_GOOD, with_probe_event=False), "passed", "mock")
    assert not ok and "分叉" in detail, detail


def test_predicate_refuses_a_probe_that_declared_everything():
    """denied_table 就在 declared_refs 里 ⇒ 这道题根本没在测收窄。"""
    rigged = dict(_GOOD, declared_refs=["t1", "t2", "t3"])
    ok, detail = PREDICATES["table_denied"](_evidence(rigged), "passed", "mock")
    assert not ok and "没在测收窄" in detail, detail


def test_runner_floor_fires_on_violated_and_on_missing():
    runner = _load_runner()

    check = runner.guard_gate_check(_evidence(_GOOD))
    assert check.passed and check.tier == "gate"

    broken = {"ran": True, "state": "violated", "declared_refs": ["t1"], "denied_table": "t3", "reason": "越表读被放行"}
    check = runner.guard_gate_check(_evidence(broken))
    assert not check.passed and "未声明的表在读得到" in check.detail

    check = runner.guard_gate_check(_evidence(None))
    assert not check.passed, "自检整栏消失却放行 = 下限本身可以被静默摘掉"

    check = runner.guard_gate_check(_evidence({"ran": True, "state": "skipped", "tables": 1, "reason": "不做表级收窄"}))
    assert check.passed, "skipped 是可见的'这次测不着'，不是缺陷"


def test_the_guard_check_is_wired_into_every_case(tmp_path):
    """测**接线**：把 `run_case` 里那句 append 摘掉，本条必须红。

    #13 的变异位点 D 就是这么漏过的——只直接调下限函数，永远测不到"它每次运行都真挂上"。
    """
    import yaml

    runner = _load_runner()
    suite = yaml.safe_load((PROJECT_ROOT / "evals" / "suite.yaml").read_text(encoding="utf-8"))
    case = next(c for c in suite["cases"] if str(c.get("id")) == "E21")
    row = runner.run_case(case, suite, "mock", tmp_path / "eval")
    kinds = {check["kind"] for check in row["checks"]}
    assert "guard_selfcheck" in kinds, sorted(kinds)
    assert row["verdict"] == "pass", row["checks"]
    # 题里显式写的 table_denied 断言与 runner 常驻下限同时在场，缺一不可
    assert any(check["kind"] == "table_denied" for check in row["checks"])
