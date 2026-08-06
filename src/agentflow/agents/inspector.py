"""InspectorAgent：数据质量审核员（确定性规则优先，语义检查可选）。"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agents.base import BaseAgent
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
        )
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="verdict",
            content=verdict.model_dump_json(),
        )
