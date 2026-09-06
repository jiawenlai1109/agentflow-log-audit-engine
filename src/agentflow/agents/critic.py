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
            sections=data.get("sections"),
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
        llm_error_text = ""
        try:
            llm_review = self.complete_structured(
                ctx,
                Review,
                user_content=(
                    f"用户问题：{question}\n"
                    f"数据集总行数：{data.get('dataset_rows', '未知')}\n"
                    # 摘要剔除证据行：原始证据会撑爆截断窗口，且属攻击者可控内容不进评审
                    f"数据摘要（已剔除证据行，仅保留可核对数字）：{json.dumps(self._compact_results(results), ensure_ascii=False)[:3000]}\n"
                    # 视窗必须覆盖完整报告：截断视图会让 LLM 评审把"我看不全"误判为"报告被截断"
                    f"报告内容：\n{report_text[:8000]}\n"
                    "评审须知：发现中的 evidence 证据行是样例（每条至多 5 行），不用于核对数值；"
                    "数值以独立复算结果为准。"
                ),
            )
        except Exception as exc:
            llm_review = Review(verdict="PASS", rounds=1, issues=[])
            llm_unavailable = True
            llm_error_text = str(exc)[:150]

        all_issues = issues + [issue.model_dump() for issue in llm_review.issues]
        if llm_unavailable:
            # v1.2：fail-open 的降级必须留痕，否则评估数据无法区分"通过"与"评审没跑成"
            all_issues.append(
                {
                    "severity": "low",
                    "section": "评审",
                    "message": f"LLM 语义评审不可用（{llm_error_text or '原因未知'}），本次仅完成确定性检查",
                }
            )
        verdict = "FAIL" if (all_issues or llm_review.verdict == "FAIL") else "PASS"
        review = Review(
            verdict=verdict,
            rounds=1,
            issues=[ReviewIssue(**issue) for issue in all_issues],
        )
        return self.reply(ctx, "orchestrator", "review", review.model_dump_json())

    @staticmethod
    def _compact_results(results: dict[str, Any] | None) -> dict[str, Any]:
        """评审用紧凑摘要：剔除证据行与过程字段，只留可核对的数字与结论。"""
        compact: dict[str, Any] = {}
        for tid, entry in (results or {}).items():
            entry = entry or {}
            summary = entry.get("summary") or {}
            compact[str(tid)] = {
                "status": entry.get("status"),
                "rows": summary.get("rows"),
                "aggregate": summary.get("aggregate"),
                "findings": [
                    {k: f.get(k) for k in ("rule_id", "subject", "metric", "value")}
                    for f in summary.get("findings") or []
                    if isinstance(f, dict)
                ],
            }
        return compact
