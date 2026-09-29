"""PlannerAgent：数据分析规划师（Plan-and-Execute 的 Plan 阶段 + 重规划环/澄清）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core.llm import MockLLM
from agentflow.core.memory import (
    build_turn_view,
    is_recall_question,
    render_summary_text,
    score_turn,
)
from agentflow.core.messages import AgentMessage
from agentflow.core.pack import available_columns as pack_available_columns
from agentflow.core.pack import missing_required as pack_missing
from agentflow.core.pack import pack_plan_tasks
from agentflow.schemas.clarify import ClarifyRequest
from agentflow.schemas.plan import TaskList
from agentflow.core.prompts import load_prompt




class PlannerAgent(BaseAgent):
    name = "planner"
    system_prompt = load_prompt("planner")

    METRIC_KEYWORDS = ("利润", "利润率", "销售额", "销量")

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        if getattr(ctx, "pack", None) is not None:
            return self._pack_plan(ctx, message)
        if message.kind == "replan_request":
            return self._replan(ctx, message)
        question = message.content
        schema = ctx.schema_profile or {}
        columns = [col["name"] for col in schema.get("columns", [])]
        turns = ctx.session.read_turns() if ctx.session else []
        # recall 回退（v1.2）：回忆词命中只是必要条件，还需历史轮次与问题有相关性——
        # 防"前面三种商品的对比"这类误伤；误判方向不对称，宁可多算不可答非所问
        recall = (
            is_recall_question(question)
            and bool(turns)
            and any(score_turn(turn, question) > 0 for turn in turns)
        )
        session_memory = self._load_session_memory(ctx, question)

        if isinstance(self.llm, MockLLM):
            task_list = self._mock_plan(question, schema, recall=recall)
        else:
            task_list = self._llm_plan(ctx, question, columns, session_memory, recall=recall)

        constraints = task_list.get("constraints")
        if constraints:
            # 约束一等公民（v1.2）：写入 RunContext，由 Orchestrator 注入下游全部 Agent
            ctx.constraints = constraints
        plan_path = ctx.outputs_dir / "plan.json"
        plan_path.write_text(json.dumps(task_list, ensure_ascii=False), encoding="utf-8")
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="task_list",
            content=json.dumps(task_list, ensure_ascii=False),
            artifacts=[str(plan_path)],
        )

    # ------------------------------------------------------------ 场景包模式（工作规划 §6.2）
    def _pack_plan(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        """场景包模式：按规则目录确定性规划，不调 LLM——检测标准来自规则包而非模型。

        规划前预检数据约定必需列，缺列抛错 → Orchestrator 降级（诚实失败，不做半套审计）。
        """
        if message.kind == "replan_request":
            return self._pack_replan(ctx, message)
        pack = ctx.pack
        # 缺列判据只有 pack.py 一处：边界预检与这里必须给同一份清单
        missing = pack_missing(pack, pack_available_columns(pack, ctx.bundle))
        if missing:
            raise ValueError(
                f"数据缺少场景包 {pack.name} 必需列：{'、'.join(missing)}"
                f"（需要：{pack.required_columns}，见 packs/{pack.name}/data_convention.md）"
            )
        tasks = pack_plan_tasks(pack, ctx.bundle)
        task_list = {
            "question": ctx.question,
            "time_base": None,
            "constraints": None,
            "tasks": tasks,
        }
        plan_path = ctx.outputs_dir / "plan.json"
        plan_path.write_text(json.dumps(task_list, ensure_ascii=False), encoding="utf-8")
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="task_list",
            content=json.dumps(task_list, ensure_ascii=False),
            artifacts=[str(plan_path)],
        )

    def _pack_replan(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        """pack 模式重规划：确定性保留剩余规则任务，不让 LLM 修改检测标准。"""
        data = json.loads(message.content)
        remaining = data.get("remaining_tasks") or []
        survivors = [task for task in remaining if task.get("rule_params")]
        if not survivors:
            clarify = ClarifyRequest(
                reason="unrecoverable_plan",
                missing_columns=[],
                question="场景包规则任务无法继续执行。",
                options=["改为普通分析模式（不使用 --pack）", "仅说明问题并结束"],
                suggestion="检查数据格式是否符合场景包数据约定（packs/login_audit/data_convention.md）",
            )
            return self.reply(
                ctx, "orchestrator", "clarify_request", clarify.model_dump_json()
            )
        revised = self._renumber(survivors)
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="revised_task_list",
            content=json.dumps(revised, ensure_ascii=False),
        )

    # ------------------------------------------------------------ 重规划环 / 澄清（v1.2）
    def _replan(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        """错误路由（MISSING_COLUMN / memory 回退）到规划层：增量修订剩余任务，不可修订则澄清。"""
        data = json.loads(message.content)
        question = data.get("question", "")
        schema = ctx.schema_profile or {}
        columns = [col["name"] for col in schema.get("columns", [])]
        missing = data.get("missing_columns") or []
        remaining = data.get("remaining_tasks") or []
        disable_memory = bool(data.get("disable_memory"))
        join_rejects = data.get("join_rejects") or []
        failed_desc = str((data.get("failed_task") or {}).get("description", ""))

        if isinstance(self.llm, MockLLM):
            revised = self._mock_replan(
                question, schema, remaining, missing, disable_memory, join_rejects
            )
        else:
            revised = self._llm_replan(
                ctx, question, columns, missing, failed_desc, remaining, disable_memory
            )

        if not revised:
            clarify = ClarifyRequest(
                reason="missing_column" if missing else "unrecoverable_plan",
                missing_columns=missing,
                question=(
                    f"数据中缺少字段：{'、'.join(missing)}，且无语义等价列可替代。"
                    if missing
                    else "当前计划无法继续执行。"
                )
                + "是否改为分析其他可用指标？",
                options=["改为分析现有可用字段", "仅说明问题并结束"],
                suggestion="如需该字段分析，请上传包含相应列的数据集",
            )
            return self.reply(
                ctx, "orchestrator", "clarify_request", clarify.model_dump_json()
            )

        revised = self._renumber(revised)
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="revised_task_list",
            content=json.dumps(revised, ensure_ascii=False),
        )

    def _mock_replan(
        self,
        question: str,
        schema: dict[str, Any],
        remaining: list[dict[str, Any]],
        missing: list[str],
        disable_memory: bool,
        join_rejects: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]] | None:
        """确定性修订：丢弃依赖缺失列的任务；其余保持原样（等 orchestrator 重新派发）。

        M2-3：预检已判定连不上的那组表，剩余任务里再声明同一组 refs 的一并剔掉——
        重复派发只会再算一次同样的基数，浪费一次执行还把 run 拖向超时。
        """
        columns = {col["name"] for col in schema.get("columns", [])}
        rejected_refs = {
            tuple(sorted(str(ref) for ref in (item.get("refs") or [])))
            for item in (join_rejects or [])
            if item.get("refs")
        }
        survivors: list[dict[str, Any]] = []
        for task in remaining:
            if disable_memory and task.get("code_hint") == "memory_answer":
                continue
            refs = tuple(sorted(str(ref) for ref in (task.get("dataset_refs") or [])))
            if len(refs) > 1 and refs in rejected_refs:
                continue
            required = task.get("required_columns") or []
            if missing and any(col in missing for col in required):
                continue
            if required and not all(col in self._column_scope(schema, task) for col in required):
                continue
            survivors.append(task)
        return survivors or None

    @staticmethod
    def _column_scope(schema: dict[str, Any], task: dict[str, Any]) -> set[str]:
        """任务的列域：单表 = 主表列；跨表 = 所声明那几张表的列并集（不放宽到未声明的表）。"""
        columns = {col["name"] for col in schema.get("columns", [])}
        refs = [str(ref) for ref in (task.get("dataset_refs") or [])]
        if len(refs) < 2:
            return columns
        by_id = {str(item.get("id")): item for item in schema.get("tables") or []}
        for ref in refs:
            columns |= {str(col) for col in (by_id.get(ref) or {}).get("columns") or []}
        return columns

    def _llm_replan(
        self,
        ctx: Any,
        question: str,
        columns: list[str],
        missing: list[str],
        failed_desc: str,
        remaining: list[dict[str, Any]],
        disable_memory: bool,
    ) -> list[dict[str, Any]] | None:
        extra = (
            "\n注意：不要输出记忆回答任务（code_hint=memory_answer），请按正常数据计算重新规划。"
            if disable_memory
            else ""
        )
        user_content = (
            f"用户问题：{question}\n"
            f"可用列：{columns}\n"
            f"失败任务：{failed_desc}（缺失字段：{missing}）\n"
            f"剩余未执行任务：{json.dumps(remaining, ensure_ascii=False)}\n"
            "请修订剩余任务：只允许修改或删除未执行任务，不得重复已完成的任务；"
            "可为缺失字段寻找语义等价列，或把分析目标调整为现有数据可回答的形式。"
            "输出修订后的任务清单 JSON（task_id 从 1 递增）；若无可行修订，输出 {\"tasks\": []}。"
            + extra
        )
        try:
            task_list = self.complete_structured(ctx, schema=TaskList, user_content=user_content)
            return [task.model_dump() for task in task_list.tasks] or None
        except Exception:  # noqa: BLE001 - 修订失败退化为确定性修订
            schema = ctx.schema_profile or {}
            return self._mock_replan(question, schema, remaining, missing, disable_memory)

    @staticmethod
    def _renumber(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """重新编号为 1..N 连续，清理指向已删除任务的依赖与上游引用。"""
        kept = [dict(task) for task in tasks]
        kept.sort(key=lambda task: int(task.get("task_id", 0)))
        old_to_new: dict[int, int] = {}
        for new_id, task in enumerate(kept, start=1):
            old_to_new[int(task.get("task_id", 0))] = new_id
            task["task_id"] = new_id
        for task in kept:
            new_deps = [old_to_new[d] for d in task.get("depends_on", []) if d in old_to_new]
            task["depends_on"] = new_deps
            old_refs = task.get("upstream_refs") or []
            task["upstream_refs"] = [
                ref for ref in old_refs if int(re.search(r"step_(\d+)_", ref).group(1)) in old_to_new
            ] if old_refs else []
        return {"tasks": kept}


    # ------------------------------------------------------------ mock 模式
    def _mock_plan(
        self, question: str, schema: dict[str, Any], recall: bool = False
    ) -> dict[str, Any]:
        if recall:
            return {
                "question": question,
                "time_base": None,
                "tasks": [self._memory_task()],
            }
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
        # Top-N / 对比优先于趋势（"近七天哪个产品利润最高？前三名"应走对比分支）
        if any(
            k in question for k in ("对比", "哪个", "最高", "最好", "排名", "top", "前三", "前3")
        ):
            cat = self._pick_category(schema)
            num = self._pick_numeric(schema, question)
            top_label = "前三名" if any(k in question for k in ("前三", "前3", "排名", "top")) else "Top"
            tasks.append(
                {
                    "task_id": 2,
                    "description": f"按{cat}对比{num}（取{top_label}）",
                    "required_columns": [cat, num],
                    "code_hint": f"按类别分组聚合取 {top_label}",
                    "chart_type": "bar",
                    "depends_on": [],
                }
            )
        elif date_col and any(
            k in question for k in ("趋势", "走势", "变化", "每日", "最近", "近七天", "近7天", "一周")
        ):
            num = self._pick_numeric(schema, question)
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
        join_task = self._mock_join_task(schema)
        if join_task is not None:
            join_task["task_id"] = len(tasks) + 1
            tasks.append(join_task)
        return {
            "question": question,
            "time_base": {"type": "data_max_date"} if date_col else None,
            "tasks": tasks,
        }

    @staticmethod
    def _mock_join_task(schema: dict[str, Any]) -> dict[str, Any] | None:
        """多源 Bundle 的确定性跨表任务：取 join 候选里重叠率最高且可用的一对表。

        只在 `multi_table` 为真时出现——单表计划的形状一个字节都不变。
        """
        if not schema.get("multi_table"):
            return None
        candidate = next(
            (item for item in schema.get("join_candidates") or [] if item.get("usable")), None
        )
        if candidate is None:
            return None
        tables = {str(item.get("id")): item for item in schema.get("tables") or []}
        left, right = str(candidate["left"]), str(candidate["right"])
        if left not in tables or right not in tables:
            return None
        key = str(candidate["column"])
        return {
            "task_id": 1,
            "description": f"按{key}关联 {left}×{right} 后汇总（跨表）",
            "required_columns": [key],
            "code_hint": "join",
            "chart_type": "bar",
            "depends_on": [],
            "dataset_refs": [left, right],
            "join_keys": [key],
        }

    def _pick_numeric(self, schema: dict[str, Any], question: str) -> str:
        """优先选择问题中提到的指标列（如"利润"），否则取第一个数值列。"""
        columns = schema.get("columns", [])
        for keyword in self.METRIC_KEYWORDS:
            if keyword in question:
                for col in columns:
                    if col["name"] == keyword and ("int" in col["dtype"] or "float" in col["dtype"]):
                        return keyword
                for col in columns:
                    if keyword in col["name"] and ("int" in col["dtype"] or "float" in col["dtype"]):
                        return col["name"]
        for col in columns:
            if "int" in col["dtype"] or "float" in col["dtype"]:
                return col["name"]
        return columns[-1]["name"] if columns else ""

    def _pick_category(self, schema: dict[str, Any]) -> str:
        """选择类别列（排除日期列，避免把日期当类别）。"""
        columns = schema.get("columns", [])
        for col in columns:
            if col.get("is_date"):
                continue
            if col["dtype"] == "object":
                return col["name"]
        for col in columns:
            if not col.get("is_date"):
                return col["name"]
        return columns[0]["name"] if columns else ""

    @staticmethod
    def _memory_task() -> dict[str, Any]:
        return {
            "task_id": 1,
            "description": "基于会话记忆回答历史问题",
            "required_columns": [],
            "code_hint": "memory_answer",
            "chart_type": "none",
            "depends_on": [],
        }

    # ------------------------------------------------------------ LLM 模式
    def _llm_plan(
        self,
        ctx: Any,
        question: str,
        columns: list[str],
        session_memory: str,
        recall: bool = False,
    ) -> dict[str, Any]:
        recall_hint = (
            "\n注意：这是一个回忆/引用历史轮次的问题，请只输出一个任务："
            "description 以'基于会话记忆回答'开头，code_hint 为 memory_answer，"
            "required_columns 为空数组，chart_type 为 none。"
            "不要规划重新加载数据计算的子任务。"
            if recall
            else ""
        )
        schema = ctx.schema_profile or {}
        user_content = (
            f"用户问题：{question}\n"
            f"可用列：{columns}\n"
            f"{self._tables_hint(schema)}"
            f"会话记忆：{session_memory}\n"
            f"{recall_hint}\n"
            "请输出任务清单 JSON。"
        )
        messages = [{"role": "user", "content": user_content}]
        known_refs = {
            str(item.get("id")) for item in (schema.get("tables") or [])
        } or {"t1"}
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
            bad_refs = [
                ref
                for task in task_list.tasks
                for ref in task.dataset_refs
                if ref not in known_refs
            ]
            if not missing and not bad_refs:
                if recall:
                    # 回忆问题：只保留记忆回答任务，不重新计算
                    return {
                        "question": question,
                        "time_base": None,
                        "tasks": [self._memory_task()],
                    }
                return task_list.model_dump(mode="json")
            messages += [
                {"role": "assistant", "content": task_list.model_dump_json()},
                {
                    "role": "user",
                    "content": (
                        f"列名校验失败：以下列不存在 {missing}，请使用可用列重新规划。"
                        if missing
                        else f"表 id 校验失败：以下引用不在可用表里 {bad_refs}，请只引用"
                        f"清单里列出的表 id，或改为单表任务。"
                    ),
                },
            ]
        # 兜底：LLM 连续失败时退化为 mock 计划
        return self._mock_plan(question, ctx.schema_profile or {}, recall=recall)

    @staticmethod
    def _tables_hint(schema: dict[str, Any]) -> str:
        """多源时把"可用表 + join 候选"写进 prompt——这是确定性预检算出来的事实，
        不是让 LLM 自己猜哪两张表连得上（I2：判定权不下放）。
        """
        tables = schema.get("tables") or []
        if len(tables) < 2:
            return ""
        lines = ["可用表（id、来源文件、行数、列）："]
        for item in tables:
            lines.append(
                f"- {item.get('id')}：{item.get('source_file')}，{item.get('row_count')} 行，"
                f"列 {item.get('columns')}"
            )
        candidates = schema.get("join_candidates") or []
        lines.append(
            "join 候选（同名列 + 取值重叠率，usable=可用作键）："
            + (
                "；".join(
                    f"{c['left']}↔{c['right']} on {c['column']}（重叠 {c['overlap']}）"
                    for c in candidates[:6]
                )
                or "无（这些表之间没有同名列，不要规划跨表 join）"
            )
        )
        return "\n".join(lines) + "\n"

    def _load_session_memory(self, ctx: Any, question: str) -> str:
        if not ctx.session:
            return "（无）"
        summary = ctx.session.load_summary()
        turns = ctx.session.read_turns()
        parts: list[str] = []
        if summary:
            summary_text = render_summary_text(summary)
            if summary_text and summary_text != "（无）":
                parts.append("历史摘要：\n" + summary_text)
        conflicts = (summary or {}).get("conflicts") or []
        if conflicts:
            parts.append("冲突提醒：" + "；".join(conflicts)[:500])
        view = build_turn_view(turns, question)
        if view and view != "（无历史对话）":
            parts.append("相关历史对话：\n" + view)
        return "\n".join(parts) or "（无）"
