"""CriticAgent：报告评审员（确定性检查 + LLM 语义评审，锚定真实数据）。"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core.messages import AgentMessage
from agentflow.schemas.review import Review, ReviewIssue


CRITIC_SYSTEM = """你是报告质量评审员。检查报告是否完整、数字是否与数据一致、是否有洞察。
输出 JSON：{"verdict": "PASS" 或 "FAIL", "rounds": 1, "issues": [{"severity": "high|medium|low", "section": "...", "message": "..."}]}"""


class CriticAgent(BaseAgent):
    name = "critic"
    system_prompt = CRITIC_SYSTEM

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        data = json.loads(message.content)
        report_path = data.get("report_path")
        question = data.get("question", "")
        results: dict[str, Any] = data.get("results", {})
        degraded = bool(data.get("degraded", False))

        issues = self.registry.call(
            self.name,
            "check_report",
            ctx,
            report_path=report_path,
            question=question,
            results=results,
        )
        if degraded:
            # 降级报告不检查标准章节/数字（预期缺失），只确认文件可读
            exists = True
            try:
                exists = ctx.ensure_within(report_path).exists()
            except Exception:
                exists = False
            if not exists:
                issues = [{"severity": "high", "section": "整体", "message": "降级报告文件不存在"}]
            review = Review(
                verdict="PASS" if not issues else "FAIL",
                rounds=1,
                issues=[ReviewIssue(**issue) for issue in issues],
            )
            return self.reply(ctx, "orchestrator", "review", review.model_dump_json())

        report_text = ""
        try:
            report_text = ctx.ensure_within(report_path).read_text(encoding="utf-8")
        except Exception:
            issues.append({"severity": "high", "section": "整体", "message": "报告无法读取"})
        llm_unavailable = False
        try:
            llm_review = self.complete_structured(
                ctx,
                Review,
                user_content=(
                    f"用户问题：{question}\n"
                    f"数据摘要：{json.dumps(results, ensure_ascii=False)[:1500]}\n"
                    f"报告内容：\n{report_text[:3000]}"
                ),
            )
        except Exception:
            llm_review = Review(verdict="PASS", rounds=1, issues=[])
            llm_unavailable = True

        all_issues = issues + [issue.model_dump() for issue in llm_review.issues]
        if llm_unavailable:
            # v1.2：fail-open 的降级必须留痕，否则评估数据无法区分"通过"与"评审没跑成"
            all_issues.append(
                {
                    "severity": "low",
                    "section": "评审",
                    "message": "LLM 语义评审不可用，本次仅完成确定性检查",
                }
            )
        verdict = "FAIL" if (all_issues or llm_review.verdict == "FAIL") else "PASS"
        review = Review(
            verdict=verdict,
            rounds=1,
            issues=[ReviewIssue(**issue) for issue in all_issues],
        )
        return self.reply(ctx, "orchestrator", "review", review.model_dump_json())
