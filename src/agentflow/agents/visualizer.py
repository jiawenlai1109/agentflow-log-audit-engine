"""VisualizerAgent：数据可视化师（确定性选图 + LLM 代码 + PNG 校验）。"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.agents.executor import strip_code_fence
from agentflow.core.llm import LLMError
from agentflow.core.messages import AgentMessage
from agentflow.schemas.figure import FigureResult


VISUALIZER_SYSTEM = """你是数据可视化专家。根据任务与数据生成 matplotlib 代码。
要求：
- 只输出纯 Python 代码，禁止 Markdown 围栏；
- 必须包含中文字体配置：plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']，且 axes.unicode_minus = False；
- 可读取 os.environ['DATA_PATH'] 或 os.environ['RESULT_PATH']；
- 图表保存到 os.environ['CHART_PATH']；
- 注意：本环境 pandas 为 3.x，月末频率请用 'ME'（'M' 已废弃），月初 'MS' 不变；
- 禁止访问网络。"""


class VisualizerAgent(BaseAgent):
    name = "visualizer"
    system_prompt = VISUALIZER_SYSTEM

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        data = json.loads(message.content)
        task = data["task"]
        result = data["result"]
        task_id = int(task["task_id"])

        chart_type = self._decide_chart_type(task, result, ctx)
        if chart_type == "none":
            figure = FigureResult(task_id=task_id, chart_type="none")
            return self.reply(ctx, "orchestrator", "figure_result", figure.model_dump_json())

        chart_path = ctx.artifacts_dir / f"chart_task_{task_id}.png"
        base_prompt = (
            f"任务：{task.get('description')}\n"
            f"图表类型：{chart_type}\n"
            f"数据列：{task.get('required_columns')}\n"
            "请输出生成图表的纯 Python 代码。"
        )
        last_stderr = ""
        for attempt in range(1, 3):
            try:
                code = self.complete(
                    ctx,
                    base_prompt
                    + (f"\n上次执行报错：{last_stderr[:1500]}\n请修正后重新输出代码。" if last_stderr else ""),
                )
                code = strip_code_fence(code)
            except LLMError as exc:
                last_stderr = str(exc)
                break
            outcome = self.registry.call(
                self.name,
                "execute_python",
                ctx,
                code=code,
                work_dir=ctx.task_work_dir(task_id),
                timeout=self._timeout(ctx),
                env={
                    "DATA_PATH": ctx.data_path,
                    "RESULT_PATH": result.get("intermediate_file") or "",
                    "CHART_PATH": str(chart_path),
                    "ARTIFACTS_DIR": str(ctx.artifacts_dir),
                    "CHART_TYPE": chart_type,
                    "MPLCONFIGDIR": str(ctx.task_work_dir(task_id) / "mplconfig"),
                },
            )
            if outcome.success and chart_path.exists() and chart_path.stat().st_size > 0:
                figure = FigureResult(
                    task_id=task_id,
                    chart_type=chart_type,
                    title=f"任务 {task_id} 图表",
                    file_path=str(chart_path),
                    note=self._compute_note(result),
                )
                return self.reply(
                    ctx,
                    "orchestrator",
                    "figure_result",
                    figure.model_dump_json(),
                    artifacts=[str(chart_path)],
                )
            last_stderr = outcome.stderr or "图表生成失败"

        note = f"图表生成失败：{last_stderr[:300]}"
        figure = FigureResult(task_id=task_id, chart_type=chart_type, title="", note=note)
        return self.reply(ctx, "orchestrator", "figure_result", figure.model_dump_json())

    # ------------------------------------------------------------ helpers
    def _decide_chart_type(self, task: dict[str, Any], result: dict[str, Any], ctx: Any) -> str:
        suggested = task.get("chart_type") or ""
        if suggested and suggested != "none":
            return suggested
        if suggested == "none":
            # 显式 none：Planner 明确不需要图表，不做推断
            return "none"
        required = task.get("required_columns", [])
        schema = ctx.schema_profile or {}
        date_col = schema.get("suggested_date_column")
        if date_col and date_col in required:
            return "line"
        return "bar"

    def _timeout(self, ctx: Any) -> int:
        return int(ctx.config.get("execution", {}).get("task_timeout_seconds", 30))

    def _compute_note(self, result: dict[str, Any]) -> str:
        head = (result.get("summary") or {}).get("head") or []
        key: str | None = None
        for row in head:
            for k, v in row.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    key = k
                    break
            if key:
                break
        if key:
            values = [
                float(row[key])
                for row in head
                if isinstance(row.get(key), (int, float))
            ]
            if values:
                return f"{key} 样例范围 {min(values)} ~ {max(values)}（基于结果前几行）"
        return "图表已生成"
