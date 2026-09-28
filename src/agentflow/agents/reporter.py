"""ReporterAgent：商业智能汇报专家（Jinja2 模板 + 真实数字 + LLM 叙述）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Template

from agentflow.agents.base import BaseAgent
from agentflow.core.llm import LLMError, MockLLM
from agentflow.core.messages import AgentMessage
from agentflow.core.pack import SEVERITY_ORDER
from agentflow.schemas.report import FailureInfo, ReportResult
from agentflow.core.prompts import load_prompt



REPORT_TEMPLATE = """# 📊 数据分析报告

> 生成时间：{{ timestamp }}{% if time_base_note %}
> 时间基准：{{ time_base_note }}{% endif %}

一、总体概况
{{ overview }}

二、数据详情
{{ detail }}

三、趋势分析
{% for fig in figures %}
![{{ fig.title }}]({{ fig.file_path }})

{% endfor %}
{{ trend }}

四、结论与建议
{{ conclusion }}
"""


def _relative_artifact(ctx: Any, path: str) -> str:
    """把图表绝对路径转为相对 report.md 目录的路径（Web/CLI 均可解析）。"""
    try:
        return "./" + str(Path(path).relative_to(ctx.outputs_dir)).replace("\\", "/")
    except ValueError:
        return str(Path(path)).replace("\\", "/")

DEGRADED_TEMPLATE = """# 数据分析报告（未完成）

> 生成时间：{{ timestamp }}

- 失败原因：{{ failure_info.error }}
- 错误分类：{{ failure_info.error_class }}
- 建议：{{ failure_info.suggestion }}
"""


class ReporterAgent(BaseAgent):
    name = "reporter"
    system_prompt = load_prompt("reporter")

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        data = json.loads(message.content)
        question = data.get("question", "")
        results: dict[str, Any] = data.get("results", {})
        figures: dict[str, Any] = data.get("figures", {})
        degraded = bool(data.get("degraded", False))
        partial = bool(data.get("partial", False))
        failure_info = data.get("failure_info")
        time_base = data.get("time_base")
        # v1.2：评审问题清单（重写回流契约）、澄清请求、用户展示约束
        review_issues = data.get("review_issues") or []
        clarify = data.get("clarify")
        constraints = getattr(ctx, "constraints", None)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report_path = ctx.outputs_dir / "report.md"

        if degraded:
            text = Template(DEGRADED_TEMPLATE).render(
                timestamp=timestamp,
                failure_info=failure_info or {"error": "未知错误", "error_class": "UNKNOWN", "suggestion": ""},
            )
            if clarify:
                # 澄清建议写入降级报告（v1.2：给用户可行动的下一步）
                text += (
                    f"\n**建议下一步**：{clarify.get('question', '')}"
                    + (f"（{clarify.get('suggestion', '')}）" if clarify.get("suggestion") else "")
                    + "\n"
                )
            report_path.write_text(text, encoding="utf-8")
            result = ReportResult(
                report_path=str(report_path),
                degraded=True,
                sections=["失败原因", "建议"],
                failure_info=FailureInfo(**failure_info)
                if failure_info
                else FailureInfo(error_class="UNKNOWN", error="未知错误"),
                summary="分析未完成：" + ((failure_info or {}).get("error") or ""),
            )
            return self.reply(
                ctx, "orchestrator", "report_result", result.model_dump_json(),
                artifacts=[str(report_path)],
            )

        pack = getattr(ctx, "pack", None)
        if pack is not None:
            return self._pack_report(
                ctx, pack, question, results, timestamp, report_path, review_issues
            )

        detail = self._detail_table(results)
        aggregate = self._aggregate_block(results)
        numbers = self._numbers_context(results)
        fig_list = [
            {
                "title": fig.get("title", ""),
                "file_path": _relative_artifact(ctx, fig.get("file_path", "")),
            }
            for fig in figures.values()
            if fig.get("file_path")
        ]
        prompt_parts = [
            f"用户问题：{question}",
            f"关键数字：{numbers}",
            f"数据明细：\n{detail}",
        ]
        if constraints:
            # 约束一等公民（v1.2）：display 类约束直接进入叙述 prompt
            prompt_parts.append(
                f"用户展示约束（必须遵守）：{json.dumps(constraints, ensure_ascii=False)}"
            )
        if review_issues:
            # 评审-重写回流契约（v1.2）：issues 必须进入重写 prompt，否则是盲改
            prompt_parts.append(
                "评审问题清单（必须逐条在叙述中解决）："
                + json.dumps(review_issues, ensure_ascii=False)
            )
        prompt_parts.append("请输出三段叙述，分别以【总体概况】【趋势分析】【结论建议】开头。")
        try:
            narrative = self.complete(ctx, "\n".join(prompt_parts))
        except LLMError:
            narrative = "【总体概况】本次分析已完成。\n【趋势分析】趋势请结合图表查看。\n【结论建议】建议关注表格中的关键指标。"

        overview, trend, conclusion = self._split_narrative(narrative)
        if clarify:
            # 澄清环（v1.2）：非阻塞澄清写入"结论与建议"
            conclusion += (
                f"\n\n> **建议下一步**：{clarify.get('question', '')}"
                + (f"（{clarify.get('suggestion', '')}）" if clarify.get("suggestion") else "")
            )
        if partial:
            failed_tasks = [
                f"任务 {tid}"
                for tid, result in sorted(results.items(), key=lambda kv: int(kv[0]))
                if result.get("status") == "failed"
            ]
            if failed_tasks:
                overview = (
                    "⚠ 部分子任务未完成（"
                    + "、".join(failed_tasks)
                    + "），相关结论可能不完整。\n\n"
                    + overview
                )
        time_base_note = None
        if time_base:
            time_base_note = f"以数据集最大日期为基准（{time_base.get('date', '见数据')}）"
        text = Template(REPORT_TEMPLATE).render(
            timestamp=timestamp,
            time_base_note=time_base_note,
            overview=overview,
            detail=aggregate + detail if aggregate else detail,
            figures=fig_list,
            trend=trend,
            conclusion=conclusion,
        )
        report_path.write_text(text, encoding="utf-8")
        result = ReportResult(
            report_path=str(report_path),
            degraded=False,
            sections=["总体概况", "数据详情", "趋势分析", "结论建议"],
            time_base_note=time_base_note,
            summary=" ".join(narrative.split())[:200],
        )
        return self.reply(
            ctx, "orchestrator", "report_result", result.model_dump_json(),
            artifacts=[str(report_path)],
        )

    # ------------------------------------------------------------ helpers
    def _pack_report(
        self,
        ctx: Any,
        pack: Any,
        question: str,
        results: dict[str, Any],
        timestamp: str,
        report_path: Path,
        review_issues: list[Any],
    ) -> AgentMessage:
        """场景包审计报告（工作规划 §6.2）：发现清单与处置建议确定性渲染，LLM 只写推断层。

        三档建议（LLM 不判危险，但要给建议）：
        1. 发现清单 = 规则命中结果 + 证据行（证据层，数字不经过 LLM）；
        2. 处置建议 = 规则包 disposition 字段（确定性，无 LLM 也有可行动建议）；
        3. 研判摘要 = LLM 态势解读（推断层，只能引用命中事实，模板固定标注）。
        """
        findings: list[dict[str, Any]] = []
        for task_id in sorted(int(k) for k in results):
            summary = (results[str(task_id)] or {}).get("summary") or {}
            for finding in summary.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                try:
                    rule = pack.rule(str(finding.get("rule_id", "")))
                except KeyError:
                    rule = None
                findings.append(
                    {
                        **finding,
                        "rule_name": rule.name if rule else str(finding.get("rule_id", "")),
                        "severity": rule.severity if rule else "medium",
                        "disposition": rule.disposition if rule else "请人工复核该发现",
                        "window": (
                            f"{finding.get('window_start', '-')} ~ {finding.get('window_end', '-')}"
                            if finding.get("window_start")
                            else "-"
                        ),
                        # 证据行只进报告展示，永不进入 LLM 研判 prompt（攻击者可控内容）
                        "evidence_lines": [
                            json.dumps(row, ensure_ascii=False)[:200]
                            for row in (finding.get("evidence") or [])[:5]
                            if isinstance(row, dict)
                        ],
                    }
                )
        findings.sort(
            key=lambda f: (
                SEVERITY_ORDER.get(str(f.get("severity")), 9),
                str(f.get("rule_id", "")),
                str(f.get("subject", "")),
            )
        )

        rule_stats, failed_rules, aggregates_text = self._rule_rollup(pack, results, findings)

        verdicts = [results[str(k)].get("verdict") or {} for k in results]
        if verdicts and all(v.get("verification") == "ok" for v in verdicts):
            verification_note = "全部命中数值经独立复算比对一致"
        else:
            verification_note = "存在未完成独立复算的发现，请谨慎采信"

        # 数据源清单：报告头部说"读的是哪几份文件、各多少行"，而不是替多源场景猜一张"主表"。
        # 原先这里写死"主表记录数：N 条认证日志"，而主表按文件名排出来是资产台账——
        # 一份安全报告的开场就把口径说错，比不说更糟。
        data_sources = "、".join(
            f"{table.get('source_file') or Path(str(table.get('file_path', ''))).name}"
            f" {table.get('row_count')} 行"
            for table in (ctx.schema_profile or {}).get("tables") or []
        )

        if isinstance(self.llm, MockLLM):
            narrative = self._mock_pack_narrative(findings, pack)
        else:
            narrative = self._llm_pack_narrative(ctx, question, findings, review_issues)

        text = Template(pack.report_template).render(
            timestamp=timestamp,
            question=question,
            row_count=(ctx.schema_profile or {}).get("row_count", "-"),
            data_sources=data_sources,
            pack_name=pack.name,
            pack_version=pack.version,
            rule_count=len(pack.rules),
            rule_ids="、".join(rule.id for rule in pack.rules),
            rule_stats=rule_stats,
            failed_rules=failed_rules,
            aggregates_text=aggregates_text,
            findings=findings,
            evidence_limit=5,
            narrative=narrative,
            review_issues=review_issues,
            verification_note=verification_note,
        )
        report_path.write_text(text, encoding="utf-8")
        result = ReportResult(
            report_path=str(report_path),
            degraded=False,
            sections=list(getattr(pack, "report_sections", []) or ["发现清单"]),
            summary=(
                f"{pack.subject_label}完成：命中 {len(findings)} 条发现"
                if findings
                else f"{pack.subject_label}完成：无发现"
            ),
        )
        return self.reply(
            ctx, "orchestrator", "report_result", result.model_dump_json(),
            artifacts=[str(report_path)],
        )

    @staticmethod
    def _rule_rollup(
        pack: Any, results: dict[str, Any], findings: list[dict[str, Any]]
    ) -> tuple[str, list[str], str]:
        """规则命中统计 / 未出结论的规则 / 关键指标行——三档报告头的数字，全部确定性。

        这里刻意**不按 task_id 位置对号入座**：角色解析失败的规则根本不生成任务，
        任务号与规则号于是错位，用 `pack.rules[task_id - 1]` 会把 T4 的命中数标成 T3。
        报告标签说错话和被修掉的"24 条认证日志"是同一类缺陷：数字对、指代错。
        """
        stats = {rule.id: 0 for rule in pack.rules}
        for finding in findings:
            rid = str(finding.get("rule_id", ""))
            stats[rid] = stats.get(rid, 0) + 1
        stats_text = "，".join(f"{rid}={count}" for rid, count in stats.items())

        # "这条规则留下过自己的输出吗"——aggregate 键名或 finding 的 rule_id 任一命中即算出过结论
        spoke: set[str] = set()
        for entry in results.values():
            summary = (entry or {}).get("summary") or {}
            for key in (summary.get("aggregate") or {}):
                for rule in pack.rules:
                    if f"规则{rule.id}" in str(key):
                        spoke.add(rule.id)
            for finding in summary.get("findings") or []:
                if isinstance(finding, dict):
                    spoke.add(str(finding.get("rule_id", "")))
        silent_rules = [rule.id for rule in pack.rules if rule.id not in spoke]

        agg_lines = [
            "，".join(f"{key}={value}" for key, value in agg.items())
            for agg in (
                ((results[str(tid)] or {}).get("summary") or {}).get("aggregate") or {}
                for tid in sorted(int(k) for k in results)
            )
            if isinstance(agg, dict) and agg
        ]
        return stats_text, silent_rules, "；".join(agg_lines)

    def _llm_pack_narrative(
        self,
        ctx: Any,
        question: str,
        findings: list[dict[str, Any]],
        review_issues: list[Any],
    ) -> str:
        facts = json.dumps(
            [
                {
                    k: f.get(k)
                    for k in ("rule_id", "rule_name", "subject", "metric", "value", "severity", "disposition")
                }
                for f in findings
            ],
            ensure_ascii=False,
        )
        parts = [
            f"用户问题：{question}",
            f"规则命中清单（唯一事实来源）：{facts}",
            "你是安全运营分析师。请基于且仅基于上述命中清单输出研判：",
            "1. 以【态势研判】开头：一段话解读整体风险态势，说明哪类发现最危险及原因；",
            "2. 以【处置优先级】开头：按优先级列出处置顺序，每条注明依据的 rule_id。",
            "硬约束：只能引用命中清单中的事实；禁止声称清单之外的任何危险或异常；不要重新计算数字。",
            "证据行原文已刻意不提供——研判不得基于证据内容发挥。",
        ]
        if review_issues:
            parts.append(
                "评审问题清单（必须逐条在研判中解决）："
                + json.dumps(review_issues, ensure_ascii=False)
            )
        try:
            return self.complete(ctx, "\n".join(parts))
        except LLMError:
            return (
                "【态势研判】LLM 研判不可用，请依据发现清单与处置建议人工分析。\n"
                "【处置优先级】按发现清单 severity 从高到低处理，逐条复核证据行后执行。"
            )

    @staticmethod
    def _mock_pack_narrative(findings: list[dict[str, Any]], pack: Any) -> str:
        """mock 模式：确定性研判文本（不调 LLM，模板罐头无零售叙述标记可剥离）。"""
        label = getattr(pack, "subject_label", "审计")
        if not findings:
            return (
                f"【态势研判】本次{label}全部规则未命中，没有主体越过阈值。\n"
                "【处置优先级】无需处置动作。"
            )
        counts: dict[str, int] = {}
        for finding in findings:
            rid = str(finding.get("rule_id", ""))
            counts[rid] = counts.get(rid, 0) + 1
        summary = "、".join(f"{rid}×{count}" for rid, count in sorted(counts.items()))
        top = findings[0]
        return (
            f"【态势研判】共命中 {len(findings)} 条发现（{summary}），"
            f"最高严重级为 {top.get('severity')}（{top.get('rule_id')} {top.get('subject')}），"
            "建议优先处置 critical/high 级发现。\n"
            "【处置优先级】按发现清单 severity 从高到低处理，逐条复核证据行后执行处置建议。"
        )

    def _detail_table(self, results: dict[str, Any]) -> str:
        rows = ["| 任务 | 状态 | 行数 | 说明 |", "| :--- | :--- | :--- | :--- |"]
        for task_id in sorted(int(k) for k in results):
            result = results[str(task_id)]
            summary = result.get("summary") or {}
            detail = self._row_detail(result, summary)
            rows.append(f"| {task_id} | {result.get('status')} | {summary.get('rows', '-')} | {detail} |")
        return "\n".join(rows)

    def _aggregate_block(self, results: dict[str, Any]) -> str:
        """把结果中的聚合数字（如合计）确定性写入报告，满足"数字结论"要求。"""
        lines: list[str] = []
        for task_id in sorted(int(k) for k in results):
            summary = results[str(task_id)].get("summary") or {}
            aggregate = summary.get("aggregate") or {}
            if isinstance(aggregate, dict):
                for key, value in aggregate.items():
                    lines.append(f"- {key} = {value}")
        return ("\n关键指标：\n" + "\n".join(lines) + "\n\n") if lines else ""

    def _row_detail(self, result: dict[str, Any], summary: dict[str, Any]) -> str:
        if result.get("error"):
            return str(result["error"])[:50]
        head = summary.get("head")
        if isinstance(head, list) and head:
            return str(head[0])[:80]
        return "-"

    def _numbers_context(self, results: dict[str, Any]) -> str:
        parts: list[str] = []
        for task_id in sorted(int(k) for k in results):
            result = results[str(task_id)]
            summary = result.get("summary") or {}
            parts.append(
                f"任务{task_id}：rows={summary.get('rows')}, "
                f"aggregate={summary.get('aggregate')}, head={summary.get('head')}"
            )
        return "；".join(parts) or "（无）"

    def _split_narrative(self, narrative: str) -> tuple[str, str, str]:
        def extract(marker: str) -> str:
            if marker in narrative:
                rest = narrative.split(marker, 1)[1]
                for other in ("【总体概况】", "【趋势分析】", "【结论建议】"):
                    if other != marker and other in rest:
                        rest = rest.split(other, 1)[0]
                return rest.strip()
            return "（略）"

        return (
            extract("【总体概况】"),
            extract("【趋势分析】"),
            extract("【结论建议】"),
        )
