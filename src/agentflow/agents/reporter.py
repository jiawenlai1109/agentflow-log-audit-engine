"""ReporterAgent：商业智能汇报专家（Jinja2 模板 + 真实数字 + LLM 叙述）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Template

from agentflow.agents.base import BaseAgent
from agentflow.core.llm import LLMError
from agentflow.core.messages import AgentMessage
from agentflow.schemas.report import FailureInfo, ReportResult


REPORTER_SYSTEM = """你是商业智能汇报专家。只能引用输入中提供的真实数字与结论，禁止计算或猜测。"""

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
    system_prompt = REPORTER_SYSTEM

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        data = json.loads(message.content)
        question = data.get("question", "")
        results: dict[str, Any] = data.get("results", {})
        figures: dict[str, Any] = data.get("figures", {})
        degraded = bool(data.get("degraded", False))
        partial = bool(data.get("partial", False))
        failure_info = data.get("failure_info")
        time_base = data.get("time_base")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report_path = ctx.outputs_dir / "report.md"

        if degraded:
            text = Template(DEGRADED_TEMPLATE).render(
                timestamp=timestamp,
                failure_info=failure_info or {"error": "未知错误", "error_class": "UNKNOWN", "suggestion": ""},
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
        try:
            narrative = self.complete(
                ctx,
                (
                    f"用户问题：{question}\n"
                    f"关键数字：{numbers}\n"
                    f"数据明细：\n{detail}\n"
                    "请输出三段叙述，分别以【总体概况】【趋势分析】【结论建议】开头。"
                ),
            )
        except LLMError:
            narrative = "【总体概况】本次分析已完成。\n【趋势分析】趋势请结合图表查看。\n【结论建议】建议关注表格中的关键指标。"

        overview, trend, conclusion = self._split_narrative(narrative)
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
