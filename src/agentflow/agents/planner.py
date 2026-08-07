"""PlannerAgent：数据分析规划师（Plan-and-Execute 的 Plan 阶段）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core.llm import MockLLM
from agentflow.core.messages import AgentMessage
from agentflow.schemas.plan import TaskList


PLANNER_SYSTEM = """你是一位资深的数据分析规划师。把用户的业务问题拆解为 1~5 个结构化子任务。
输出必须是合法 JSON，结构为：
{"question": "...", "time_base": {...} 或 null, "tasks": [
  {"task_id": 1, "description": "...", "required_columns": ["..."], "code_hint": "...", "chart_type": "none|line|bar|pie|hist", "depends_on": []}
]}
规则：
- required_columns 必须来自"可用列"，不得臆造列名；
- task_id 从 1 递增；depends_on 为空表示无依赖（可并行），只能引用更小的 task_id；
- 相对时间（最近7天/上周）在 time_base 中注明以数据集最大日期为基准。"""


class PlannerAgent(BaseAgent):
    name = "planner"
    system_prompt = PLANNER_SYSTEM

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        question = message.content
        schema = ctx.schema_profile or {}
        columns = [col["name"] for col in schema.get("columns", [])]
        session_memory = self._load_session_memory(ctx)

        if isinstance(self.llm, MockLLM):
            task_list = self._mock_plan(question, schema)
        else:
            task_list = self._llm_plan(ctx, question, columns, session_memory)

        plan_path = ctx.outputs_dir / "plan.json"
        plan_path.write_text(json.dumps(task_list, ensure_ascii=False), encoding="utf-8")
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="task_list",
            content=json.dumps(task_list, ensure_ascii=False),
            artifacts=[str(plan_path)],
        )

    # ------------------------------------------------------------ mock 模式
    def _mock_plan(self, question: str, schema: dict[str, Any]) -> dict[str, Any]:
        columns = [col["name"] for col in schema.get("columns", [])]
        numeric = [
            col["name"]
            for col in schema.get("columns", [])
            if "int" in col["dtype"] or "float" in col["dtype"]
        ]
        date_col = schema.get("suggested_date_column")
        tasks: list[dict[str, Any]] = [
            {
                "task_id": 1,
                "description": "数据总体概况",
                "required_columns": columns,
                "code_hint": "describe 统计与行数",
                "chart_type": "none",
                "depends_on": [],
            }
        ]
        if date_col and any(k in question for k in ("趋势", "走势", "变化", "每日", "最近")):
            num = numeric[0] if numeric else columns[-1]
            tasks.append(
                {
                    "task_id": 2,
                    "description": f"按日期聚合{num}趋势",
                    "required_columns": [date_col, num],
                    "code_hint": "按日期分组聚合",
                    "chart_type": "line",
                    "depends_on": [],
                }
            )
        elif any(k in question for k in ("对比", "哪个", "最高", "最好", "排名", "top")):
            cat = next(
                (col["name"] for col in schema.get("columns", []) if col["dtype"] == "object"),
                columns[0],
            )
            num = numeric[0] if numeric else columns[-1]
            tasks.append(
                {
                    "task_id": 2,
                    "description": f"按{cat}对比{num}",
                    "required_columns": [cat, num],
                    "code_hint": "按类别分组聚合取 Top",
                    "chart_type": "bar",
                    "depends_on": [],
                }
            )
        for keyword in ("利润", "利润率", "成本", "毛利率", "费用"):
            if keyword in question and not any(keyword in col for col in columns):
                tasks.append(
                    {
                        "task_id": len(tasks) + 1,
                        "description": f"分析{keyword}",
                        "required_columns": [keyword],
                        "code_hint": "引用该列",
                        "chart_type": "none",
                        "depends_on": [],
                    }
                )
                break
        return {
            "question": question,
            "time_base": {"type": "data_max_date"} if date_col else None,
            "tasks": tasks,
        }

    # ------------------------------------------------------------ LLM 模式
    def _llm_plan(
        self,
        ctx: Any,
        question: str,
        columns: list[str],
        session_memory: str,
    ) -> dict[str, Any]:
        user_content = (
            f"用户问题：{question}\n"
            f"可用列：{columns}\n"
            f"会话记忆：{session_memory}\n"
            "请输出任务清单 JSON。"
        )
        messages = [{"role": "user", "content": user_content}]
        for _ in range(3):
            task_list = self.complete_structured(
                ctx,
                schema=TaskList,
                user_content=user_content,
                messages=messages,
            )
            missing = [
                col
                for task in task_list.tasks
                for col in task.required_columns
                if col not in columns
            ]
            if not missing:
                return task_list.model_dump(mode="json")
            messages += [
                {"role": "assistant", "content": task_list.model_dump_json()},
                {
                    "role": "user",
                    "content": f"列名校验失败：以下列不存在 {missing}，请使用可用列重新规划。",
                },
            ]
        # 兜底：LLM 连续失败时退化为 mock 计划
        return self._mock_plan(question, ctx.schema_profile or {})

    def _load_session_memory(self, ctx: Any) -> str:
        if not ctx.session:
            return "（无）"
        parts: list[str] = []
        if ctx.session.summary_path.exists():
            parts.append("历史摘要：" + ctx.session.summary_path.read_text(encoding="utf-8")[:1000])
        if ctx.session.conversation_path.exists():
            lines = ctx.session.conversation_path.read_text(encoding="utf-8").strip().splitlines()
            parts.append("最近对话：" + " | ".join(lines[-3:]))
        return "\n".join(parts) or "（无）"
