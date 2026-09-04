"""InspectorAgent：数据质量审核员（确定性规则 + 独立校验模板 + LLM 语义检查）。"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core.llm import MockLLM
from agentflow.core.messages import AgentMessage
from agentflow.schemas.verdict import CheckResult, Verdict


class InspectorAgent(BaseAgent):
    name = "inspector"
    system_prompt = "你是数据质量审核员：审核执行结果是否为空、列是否齐全、数值是否合理、能否回答原始问题。"

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
            checks, verification = self._verify(ctx, task, result, question, checks)
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
    ) -> tuple[list[dict[str, str]], str | None]:
        """producer ≠ verifier：按任务类别用确定性模板独立重算，与上报 aggregate 容差比对。"""
        try:
            outcome = self.registry.call(
                self.name,
                "verify_aggregate",
                ctx,
                result=result,
                task={**task, "_question": question},
                data_path=ctx.data_path,
                schema_profile=ctx.schema_profile or {},
            )
        except Exception as exc:  # noqa: BLE001 - 校验器自身故障不阻塞主流程，留痕
            checks.append(
                {
                    "rule": "aggregate_match_check",
                    "level": "WARN",
                    "message": f"独立校验不可用：{str(exc)[:120]}",
                }
            )
            return checks, "skipped"
        if outcome.get("status") == "fail":
            checks.append(
                {
                    "rule": "aggregate_match_check",
                    "level": "FAIL",
                    "message": f"{outcome.get('message', '')}；独立重算值={outcome.get('expected')}",
                }
            )
            return checks, "ok"
        if outcome.get("status") == "pass":
            checks.append(
                {
                    "rule": "aggregate_match_check",
                    "level": "PASS",
                    "message": outcome.get("message", "独立重算一致"),
                }
            )
            return checks, "ok"
        return checks, "skipped"

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
        if isinstance(self.llm, MockLLM):
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
