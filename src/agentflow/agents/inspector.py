"""InspectorAgent：数据质量审核员（确定性规则 + 独立校验模板 + LLM 语义检查）。"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core import dataset_scope
from agentflow.core.llm import MockLLM
from agentflow.core.messages import AgentMessage
from agentflow.schemas.verdict import CheckResult, Verdict
from agentflow.core.prompts import load_prompt
from agentflow.core.report_lint import finding_triple


def _recompute_triples(expected: Any) -> list[dict[str, Any]]:
    """复算侧的 findings → 可核对的 (主体, 指标, 数值) 三格。

    归一化只在 `report_lint.finding_triple` 一处做（数值按报告里印出来的样子比，
    7.0 记成 7）：写在这里、写在评分器里、写在尺里各来一遍，就会有三种"相等"的含义。
    缺任何一格的条目不构成一行——宁可少一格被评分器点名，也不要拿半截数据冒充复算。
    """
    triples: list[dict[str, Any]] = []
    for item in expected or []:
        if not isinstance(item, dict):
            continue
        triple = finding_triple(item)
        if triple:
            triples.append({"subject": triple[0], "metric": triple[1], "value": triple[2]})
    return triples


class InspectorAgent(BaseAgent):
    name = "inspector"
    system_prompt = load_prompt("inspector")

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        data = json.loads(message.content)
        task: dict[str, Any] = data["task"]
        result: dict[str, Any] = data["result"]
        question: str = data.get("question", "")

        if result.get("status") == "failed":
            checks = [
                {
                    "rule": "execution_failed",
                    "level": "FAIL",
                    "message": result.get("error") or "执行失败",
                }
            ]
            verification: str | None = None
            recompute: list[dict[str, Any]] = []
        else:
            checks = self.registry.call(
                self.name,
                "validate_rules",
                ctx,
                result=result,
                task=task,
                question=question,
                schema_profile=ctx.schema_profile or {},
            )
            checks, verification, recompute = self._verify(ctx, task, result, question, checks)
            checks = self._semantic_check(ctx, task, result, question, checks)

        status = (
            "FAIL"
            if any(c["level"] == "FAIL" for c in checks)
            else ("WARN" if any(c["level"] == "WARN" for c in checks) else "PASS")
        )
        suggestion = next((c["message"] for c in checks if c["level"] == "FAIL"), None)
        verdict = Verdict(
            task_id=int(task["task_id"]),
            status=status,
            checks=[CheckResult(**check) for check in checks],
            suggestion=suggestion,
            verification=verification,
            recompute=recompute,
        )
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="verdict",
            content=verdict.model_dump_json(),
        )

    # ------------------------------------------------------------ 独立校验（v1.2）
    def _verify(
        self,
        ctx: Any,
        task: dict[str, Any],
        result: dict[str, Any],
        question: str,
        checks: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], str | None, list[dict[str, Any]]]:
        """producer ≠ verifier：场景包走 finding 独立重算，通用任务走 aggregate 模板重算。

        第三个返回值是**复算侧的 (主体, 指标, 数值)**，只有 finding 级重算才有；
        aggregate 那条路重算的是一个标量，给不出逐主体的三元组，就老实返回空表
        （评分器据此少跑一条腿，而不是假装跑过）。
        """
        rule_params = task.get("rule_params") or {}
        if getattr(ctx, "pack", None) is not None and rule_params:
            return self._verify_findings(ctx, task, result, checks)
        try:
            outcome = self.registry.call(
                self.name,
                "verify_aggregate",
                ctx,
                result=result,
                task={**task, "_question": question},
                # 跨表任务按它自己声明的表重放（与执行器同一口径，见 core/dataset_scope）
                data_path=dataset_scope.primary_path(ctx, task),
                schema_profile=ctx.schema_profile or {},
                table_paths=dataset_scope.table_paths(ctx, task),
                pairs=dataset_scope.join_pairs(ctx, task),
            )
        except Exception as exc:  # noqa: BLE001 - 校验器自身故障不阻塞主流程，留痕
            checks.append(
                {
                    "rule": "aggregate_match_check",
                    "level": "WARN",
                    "message": f"独立校验不可用：{str(exc)[:120]}",
                }
            )
            return checks, "skipped", []
        if outcome.get("status") == "fail":
            checks.append(
                {
                    "rule": "aggregate_match_check",
                    "level": "FAIL",
                    "message": f"{outcome.get('message', '')}；独立重算值={outcome.get('expected')}",
                }
            )
            return checks, "ok", []
        if outcome.get("status") == "pass":
            checks.append(
                {
                    "rule": "aggregate_match_check",
                    "level": "PASS",
                    "message": outcome.get("message", "独立重算一致"),
                }
            )
            return checks, "ok", []
        return checks, "skipped", []

    def _verify_findings(
        self,
        ctx: Any,
        task: dict[str, Any],
        result: dict[str, Any],
        checks: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], str | None, list[dict[str, Any]]]:
        """场景包独立校验：规则 verify_code（纯 Python 异构实现）重算 findings 按 subject 比对。

        复算侧的三元组**PASS 也要落盘**：只在 FAIL 时把 `expected` 塞进 message 字符串，
        等于"出事才留证据"，那条线平时读不到复算值，也就没法逐格等值核对。
        """
        try:
            outcome = self.registry.call(
                self.name,
                "verify_findings",
                ctx,
                task=task,
                result=result,
                data_path=ctx.data_path,
                pack=ctx.pack,
            )
        except Exception as exc:  # noqa: BLE001 - 校验器自身故障不阻塞主流程，留痕
            checks.append(
                {
                    "rule": "finding_match_check",
                    "level": "WARN",
                    "message": f"独立校验不可用：{str(exc)[:120]}",
                }
            )
            return checks, "skipped", []
        recompute = _recompute_triples(outcome.get("expected"))
        status = outcome.get("status")
        if status == "fail":
            checks.append(
                {
                    "rule": "finding_match_check",
                    "level": "FAIL",
                    "message": f"{outcome.get('message', '')}；独立重算={outcome.get('expected')}",
                }
            )
            return checks, "ok", recompute
        if status == "skipped":
            checks.append(
                {
                    "rule": "finding_match_check",
                    "level": "WARN",
                    "message": f"独立校验跳过：{outcome.get('message', '')}",
                }
            )
            return checks, "skipped", []
        checks.append(
            {
                "rule": "finding_match_check",
                "level": "PASS",
                "message": outcome.get("message", "独立复算一致"),
            }
        )
        return checks, "ok", recompute

    # ------------------------------------------------------------ LLM 语义检查（v1.2 落地）
    def _semantic_check(
        self,
        ctx: Any,
        task: dict[str, Any],
        result: dict[str, Any],
        question: str,
        checks: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """仅"结果能否回答原始问题"一项语义判断；失败留痕不阻塞（fail-open 留痕）。"""
        if isinstance(self.llm, MockLLM) or task.get("rule_params"):
            # 规则任务能否回答用户问题由规则包定义，语义检查只服务开放式分析任务
            return checks
        from pydantic import BaseModel

        class SemanticVerdict(BaseModel):
            status: str  # PASS / WARN / FAIL
            reason: str = ""

        try:
            verdict = self.complete_structured(
                ctx,
                schema=SemanticVerdict,
                user_content=(
                    f"原始问题：{question}\n"
                    f"任务：{task.get('description')}\n"
                    f"执行结果摘要：{json.dumps(result.get('summary') or {}, ensure_ascii=False)[:1200]}\n"
                    "请判断该结果能否回答原始问题，输出 {\"status\": \"PASS|WARN|FAIL\", \"reason\": \"...\"}。"
                ),
            )
        except Exception:  # noqa: BLE001 - 语义检查失败留痕跳过
            checks.append(
                {
                    "rule": "semantic_check",
                    "level": "WARN",
                    "message": "LLM 语义检查不可用（已跳过）",
                }
            )
            return checks
        status = (verdict.status or "").upper()
        if status == "FAIL":
            checks.append(
                {
                    "rule": "semantic_check",
                    "level": "FAIL",
                    "message": f"结果无法回答原始问题：{verdict.reason[:200]}",
                }
            )
        elif status == "WARN":
            checks.append(
                {
                    "rule": "semantic_check",
                    "level": "WARN",
                    "message": f"结果疑似未完全回答原始问题：{verdict.reason[:200]}",
                }
            )
        return checks
