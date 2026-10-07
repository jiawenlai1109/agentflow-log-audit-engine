"""VisualizerAgent：数据可视化师（确定性选图 + LLM 代码 + PNG 校验）。"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.agents.executor import strip_code_fence
from agentflow.core import dataset_scope
from agentflow.core.llm import LLMError
from agentflow.core.messages import AgentMessage
from agentflow.schemas.figure import FigureResult
from agentflow.core.prompts import load_prompt




class VisualizerAgent(BaseAgent):
    name = "visualizer"
    system_prompt = load_prompt("visualizer")

    # 降级必留因：不画的时候要说清为什么不画，否则下游只能看到一个空字段
    no_chart_reason: str = ""

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        data = json.loads(message.content)
        task = data["task"]
        result = data["result"]
        task_id = int(task["task_id"])

        self.no_chart_reason = ""
        chart_type = self._decide_chart_type(task, result, ctx)
        if chart_type == "none":
            figure = FigureResult(
                task_id=task_id, chart_type="none", note=self.no_chart_reason
            )
            return self.reply(ctx, "orchestrator", "figure_result", figure.model_dump_json())

        chart_path = ctx.artifacts_dir / f"chart_task_{task_id}.png"
        base_prompt = (
            f"任务：{task.get('description')}\n"
            f"图表类型：{chart_type}\n"
            f"数据列：{task.get('required_columns')}\n"
            "请输出生成图表的纯 Python 代码。"
        )
        last_stderr = ""
        try:
            data_path = dataset_scope.primary_path(ctx, task)
        except dataset_scope.DatasetScopeError as exc:
            # C-14② 的拒绝是这个任务的，不是整条 run 的：图画不出来就如实说没画，
            # 让图表异常把已经算完的结果打死是 #22 收过的那类越界。
            figure = FigureResult(
                task_id=task_id,
                chart_type="none",
                note=f"未画图：本任务声明的表解析不到（{str(exc)[:120]}）",
            )
            return self.reply(ctx, "orchestrator", "figure_result", figure.model_dump_json())
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
                _scope={
                    "task_id": task_id,
                    "grants": [result["intermediate_file"]]
                    if result.get("intermediate_file")
                    else [],
                    "dataset_refs": dataset_scope.scope_refs(task),
                },
                code=code,
                work_dir=ctx.task_work_dir(task_id),
                timeout=self._timeout(ctx),
                env={
                    # 跨表任务的 DATA_PATH 必须是本任务声明的第一张表，否则会画到主表上去
                    "DATA_PATH": data_path,
                    "RESULT_PATH": result.get("intermediate_file") or "",
                    "CHART_PATH": str(chart_path),
                    "ARTIFACTS_DIR": str(ctx.artifacts_dir),
                    "CHART_TYPE": chart_type,
                    "MPLCONFIGDIR": str(ctx.task_work_dir(task_id) / "mplconfig"),
                    **{
                        f"DATA_PATH_{table_id.upper()}": path
                        for table_id, path in dataset_scope.table_paths(ctx, task).items()
                    },
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
    # `when` 只准用这几个键：规则表是外部文件，给它一个可执行的表达式语法
    # 就等于把代码执行权交给一份 yaml。这里是一组封闭谓词，不是小语言。
    RULE_KEYS = ("constraint_contains", "task_chart_type_present", "required_has_date_column")

    def _decide_chart_type(self, task: dict[str, Any], result: dict[str, Any], ctx: Any) -> str:
        """按 skill `chart_selection` 的规则表选图；没有规则表就不画。

        规则原先硬编码在这个函数里（M4-B 之前）。搬出去不是为了好看：
        搬出来之后"关掉方法"是一个可执行的动作，门禁也就能量出它没了之后差在哪。
        """
        rules = self._chart_rules(ctx)
        if not rules:
            self.no_chart_reason = (
                "未装载选图方法（skill chart_selection），系统没有选图依据，故不生成图表"
            )
            return "none"
        constraints = getattr(ctx, "constraints", None) or {}
        schema = ctx.schema_profile or {}
        date_col = schema.get("suggested_date_column")
        for rule in rules:
            when = rule.get("when") or {}
            unknown = set(when) - set(self.RULE_KEYS)
            if unknown:
                raise ValueError(
                    f"选图规则 {rule.get('id')} 含未知条件 {sorted(unknown)}；"
                    "静默跳过一条规则 = 少一条在守门的规则，宁可报错"
                )
            if not self._matches(when, task, constraints, date_col):
                continue
            then = rule.get("then") or {}
            chosen = then.get("chart_type")
            if chosen == "from_task":
                return str(task.get("chart_type"))
            if isinstance(chosen, str) and chosen:
                return chosen
            raise ValueError(f"选图规则 {rule.get('id')} 没有给出 chart_type")
        # 规则表走完仍无命中 = 表里缺兜底条目
        self.no_chart_reason = "选图规则表无兜底条目，未选择图表类型"
        return "none"

    def _matches(
        self,
        when: dict[str, Any],
        task: dict[str, Any],
        constraints: dict[str, Any],
        date_col: str | None,
    ) -> bool:
        if "constraint_contains" in when:
            texts = [str(item) for item in (constraints.get("display") or [])]
            if not any(any(k in text for k in when["constraint_contains"]) for text in texts):
                return False
        if "task_chart_type_present" in when:
            present = bool(task.get("chart_type"))
            if present is not bool(when["task_chart_type_present"]):
                return False
        if "required_has_date_column" in when:
            has = bool(date_col) and date_col in (task.get("required_columns") or [])
            if has is not bool(when["required_has_date_column"]):
                return False
        return True

    def _chart_rules(self, ctx: Any) -> list[dict[str, Any]]:
        """L3 明细按需读取：每次选图都从规则表现取，读到什么在 transcript 留痕。"""
        skills = getattr(self, "skills", None)
        skill = skills.get("chart_selection") if skills is not None else None
        if skill is None:
            return []
        data = skill.reference(skill.reference_path("rules"), ctx)
        rules = (data or {}).get("rules")
        if not isinstance(rules, list):
            raise ValueError("skill chart_selection 的 rules.yaml 缺 rules 列表")
        return rules

    def _timeout(self, ctx: Any) -> int:
        return int(ctx.config.get("execution", {}).get("task_timeout_seconds", 30))

    def _compute_note(self, result: dict[str, Any]) -> str:
        head = (result.get("summary") or {}).get("head") or []
        key: str | None = None
        for row in head:
            if not isinstance(row, dict):
                continue
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
