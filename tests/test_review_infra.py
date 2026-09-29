"""#13：评审器自己没跑成时，不许折算成一次内容结论。

现场（修之前）：Critic 捕住 LLM 异常（含"调用预算耗尽"）后追加一条 `severity=low` 的
**内容** issue ⇒ 运维级失败与"报告有毛病"共用一条通道；内容 issue 还会驱动 Reporter 重写，
而重写修不好一个没接通的评审器（只是把剩余预算烧在无效重试上）；运行仍以 `status=success`
收口，门禁只看到 `critic_pass` 变假，看不出假在哪一层。

这批用例因此分成两类：一类钉"分开记"（基础设施失败走自己的字段），
一类钉"分开了有用"（谓词能区分 clean / unavailable / skipped，runner 的不变量会红）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from agentflow.core.grading import PREDICATES, CaseResult, load_evidence, review_state
from agentflow.core.llm import LLMError, MockLLM
from agentflow.pipeline import run_analysis
from agentflow.schemas.review import Review

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"
QUESTION = "对2026-09-05的登录日志做安全审计"


class CriticDownLLM(MockLLM):
    """只在评审那一步炸掉的 LLM：其余角色照常应答，模拟"预算走到评审才耗尽"。"""

    def complete_structured(self, system: Any = None, messages: Any = None, schema: Any = None, **kwargs: Any):
        if schema is Review:
            raise LLMError("调用预算耗尽（mock）")
        return super().complete_structured(
            system=system, messages=messages, schema=schema, **kwargs
        )


def _run(tmp_path: Path, llm: MockLLM | None = None) -> dict[str, Any]:
    result = run_analysis(
        question=QUESTION,
        sources=str(DATA),
        mode="mock",
        llm=llm or MockLLM(),
        outputs_root=tmp_path,
        pack="login_audit",
    )
    result["_dir"] = Path(result["outputs_dir"])
    return result


def _evaluation(run: dict[str, Any]) -> dict[str, Any]:
    return json.loads((run["_dir"] / "evaluation.json").read_text(encoding="utf-8"))


def _transcript(run: dict[str, Any]) -> list[dict[str, Any]]:
    lines = (run["_dir"] / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_a_healthy_run_reports_a_completed_review(tmp_path):
    """反向对照：正常情况下这三栏必须是"评完了"，否则后面的红分不清是修坏了还是本来就坏。"""
    run = _run(tmp_path)
    evaluation = _evaluation(run)
    assert evaluation["status"] == "success"
    assert evaluation["critic_pass"] is True
    assert review_state(evaluation) == "clean"


def test_infrastructure_failure_is_recorded_separately_from_content(tmp_path):
    """预算耗尽必须落在自己的字段上，而不是变成一条看起来像内容毛病的 issue。"""
    run = _run(tmp_path, CriticDownLLM())
    evaluation = _evaluation(run)
    assert evaluation["review_ran"] is True
    assert evaluation["review_infra_error"], "评审器掉线却没记下原因——运行会看起来「评审通过」了"
    assert "预算耗尽" in evaluation["review_infra_error"]
    assert review_state(evaluation) == "unavailable"
    # 判红与没评是两件事，但"没评"绝不能记成"评过且通过"
    assert evaluation["critic_pass"] is False

    review_messages = [
        record for record in _transcript(run) if record.get("kind") == "review"
    ]
    assert review_messages, "评审结论没进 transcript，事实层就无从核对"
    payload = json.loads(review_messages[-1]["output"]["content"])
    assert payload["infrastructure_error"] and "预算耗尽" in payload["infrastructure_error"]
    assert not [i for i in payload["issues"] if i.get("section") == "评审"], (
        "运维级失败又混进内容 issues 了——这正是 #13 的原始形态"
    )

    # 把**这次真实运行的产物**喂给 runner 的下限：门禁要能对真东西咬，不是只对合成证据咬
    check = _load_runner().review_gate_check(load_evidence(str(run["_dir"])))
    assert check is not None and not check.passed and "预算耗尽" in check.detail
    result = CaseResult(case_id="E90", mode="mock", tier="gate+gap", status="success")
    result.checks.append(check)
    assert result.verdict == "fail"


def test_infrastructure_failure_does_not_burn_a_rewrite_round(tmp_path):
    """重写修不好一个没接通的评审器：不许因为它再打一轮 Reporter。"""
    run = _run(tmp_path, CriticDownLLM())
    kinds = [record.get("kind") for record in _transcript(run)]
    assert "rewrite_report" not in kinds
    # 留痕要能说清"为什么没重写"，否则看起来像漏了一步
    unavailable = [r for r in _transcript(run) if r.get("event") == "review_unavailable"]
    assert unavailable and "不折算成内容结论" in unavailable[-1]["note"]


def test_the_gate_check_is_wired_into_every_case(tmp_path):
    """这条测的是**接线**，不是函数本身。

    变异复测抓出来的洞：只直接调 `review_gate_check()`，把 `run_case()` 里那句 append 摘掉
    照样全绿——"下限存在"与"下限每次运行都真的挂上去了"是两件事。
    """
    import yaml

    runner = _load_runner()
    suite = yaml.safe_load((PROJECT_ROOT / "evals" / "suite.yaml").read_text(encoding="utf-8"))
    case = next(c for c in suite["cases"] if str(c.get("id")) == "E21")
    row = runner.run_case(case, suite, "mock", tmp_path / "eval")
    kinds = {check["kind"] for check in row["checks"]}
    assert "review_completed" in kinds, sorted(kinds)
    assert row["verdict"] == "pass", row["checks"]
    # 题里显式写的 review 断言与 runner 常驻下限同时在场，缺一不可
    assert "review" in kinds, sorted(kinds)


def test_predicate_separates_three_review_states():
    """`review` 谓词三态各自可判；未知值红，不静默放行。"""
    predicate = PREDICATES["review"]

    def evidence(**evaluation):
        return {"evaluation": {"status": "success", **evaluation}}

    ok, detail = predicate(evidence(review_ran=True), "clean", "mock")
    assert ok, detail
    ok, detail = predicate(
        evidence(review_ran=True, review_infra_error="LLM 不可用"), "unavailable", "mock"
    )
    assert ok and "LLM 不可用" in detail
    ok, detail = predicate(evidence(), "skipped", "mock")
    assert ok, detail
    # 一次没评审的运行不许被读成"评审完成"
    assert predicate(evidence(), "clean", "mock")[0] is False
    assert predicate(evidence(review_ran=True, review_infra_error="x"), "clean", "mock")[0] is False


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_eval_probe", PROJECT_ROOT / "scripts" / "run_eval.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_gate_check_catches_success_without_a_completed_review():
    """不写在题里的下限：一次以 success 收口的运行，不许同时承认评审没跑完。"""
    from agentflow.core.grading import CaseResult

    runner = _load_runner()
    broken = runner.review_gate_check(
        {"evaluation": {"status": "success", "review_ran": True, "review_infra_error": "预算耗尽"}}
    )
    assert broken is not None and not broken.passed and broken.tier == "gate"
    assert "预算耗尽" in broken.detail

    # 红是真的会判 fail，不是"记一笔就算了"
    result = CaseResult(case_id="E90", mode="mock", tier="gate+gap", status="success")
    result.checks.append(broken)
    assert result.verdict == "fail"

    healthy = runner.review_gate_check({"evaluation": {"status": "success", "review_ran": True}})
    assert healthy is not None and healthy.passed
    # 降级运行本来就不进评审阶段：把它一起报红会让人去看错的地方
    assert runner.review_gate_check({"evaluation": {"status": "degraded", "review_ran": False}}) is None
