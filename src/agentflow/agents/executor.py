"""ExecutorAgent：代码执行者（Code Interpreter：写码-执行-调试自愈循环）。"""

from __future__ import annotations

import json
import re
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core import dataset_scope
from agentflow.core.executor import ExecutionOutcome
from agentflow.core.llm import LLMError, MockLLM, extract_json
from agentflow.core.memory import clean_summary
from agentflow.core.messages import AgentMessage
from agentflow.core.tools import ToolError
from agentflow.schemas.result import ErrorClass, TaskExecutionResult
from agentflow.core.prompts import load_prompt




def strip_code_fence(code: str) -> str:
    """剥离 LLM 输出中的 Markdown 代码围栏（容错，即使违反提示词也能执行）。"""
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z0-9_]*\s*\n?", "", code)
        code = re.sub(r"\n?```\s*$", "", code)
    return code.strip()


class ExecutorAgent(BaseAgent):
    name = "executor"
    system_prompt = load_prompt("executor")

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        task = json.loads(message.content) if isinstance(message.content, str) else message.content
        task_id = int(task["task_id"])
        work_dir = ctx.task_work_dir(task_id)
        is_memory = (
            task.get("code_hint") == "memory_answer"
            or "基于会话记忆回答" in str(task.get("description", ""))
        )
        if is_memory:
            return self._answer_from_memory(ctx, task_id, work_dir)

        rule_params = task.get("rule_params") or {}
        if rule_params and isinstance(self.llm, MockLLM):
            # 场景包规则任务（mock）：短路执行参考实现，确定性可复现（先例：memory_answer）
            result = self._run_rule_reference(ctx, task, work_dir, rule_params)
            return self._finish(ctx, task_id, work_dir, result)

        upstream = self._upstream_grants(ctx, task)
        history = self.new_history()
        history.append("user", self._task_prompt(ctx, task))
        result: TaskExecutionResult | None = None
        last_outcome: Any = None
        last_error_text: str | None = None
        attempts_used = 0
        max_attempts = int(
            ctx.config.get("execution", {}).get("max_executor_attempts", 3)
        )
        timeout_seconds = self._task_timeout(ctx)
        bumped_timeout = False

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            try:
                code = self.complete(
                    ctx, self._task_prompt(ctx, task), messages=history.to_llm_messages()
                )
            except LLMError as exc:
                result = TaskExecutionResult(
                    task_id=task_id,
                    status="failed",
                    error=str(exc),
                    error_class=ErrorClass.LLM_ERROR,
                    attempts=attempt,
                    suggestion="检查模型配置或预算",
                )
                break
            code = strip_code_fence(code)
            try:
                # 表解析放进 try：C-14② 的 DatasetScopeError 是**这个任务**的事，
                # 让它从参数构造里逃出去就会打死整条 run——那正是 #22 收过的那类越界。
                data_path = self._primary_path(ctx, task)
            except dataset_scope.DatasetScopeError as exc:
                result = TaskExecutionResult(
                    task_id=task_id,
                    status="failed",
                    error=str(exc)[:400],
                    error_class=ErrorClass.DATASET_SCOPE,
                    attempts=attempt,
                    suggestion="重新声明本任务要读的表（dataset_refs 必须是 Bundle 里存在的表 id）",
                )
                break
            outcome, contained = self._execute(
                ctx,
                task_id=task_id,
                attempt=attempt,
                scope={
                    "task_id": task_id,
                    "grants": upstream["grants"],
                    "dataset_refs": self._refs(task),
                },
                code=code,
                work_dir=work_dir,
                timeout=timeout_seconds,
                env={
                    "DATA_PATH": data_path,
                    "ARTIFACTS_DIR": str(ctx.artifacts_dir),
                    **self._dataset_env(ctx, task),
                    **upstream["env"],
                },
            )
            if contained:
                # 守卫拒绝的东西重写三轮也还是越界，直接结束这个任务的自愈
                last_outcome = outcome
                break
            if outcome.success:
                summary = self._parse_summary(outcome.stdout)
                if summary and summary.get("error"):
                    # 代码把错误写进结果 JSON 伪装成功：按失败处理进入自愈
                    last_error_text = str(summary["error"])[:500]
                    history.append("assistant", code)
                    history.append(
                        "user",
                        (
                            f"执行结果包含错误（第 {attempt} 次）：{last_error_text}\n"
                            "请修正代码后重新输出纯 Python 代码，注意让错误自然抛出或显式非零退出。"
                        ),
                    )
                    continue
                result = TaskExecutionResult(
                    task_id=task_id,
                    status="success",
                    summary=summary,
                    duration_seconds=outcome.duration_seconds,
                    attempts=attempt,
                )
                break
            # v1.2 错误路由（TIMEOUT → Executor 提档）：先翻倍超时重试一次，而非同超时盲目重试
            if outcome.timed_out and not bumped_timeout:
                bumped_timeout = True
                timeout_seconds = min(timeout_seconds * 2, 120)
            failure_text = (outcome.stderr or "")[:2000]
            if not failure_text.strip():
                # 非零退出但 stderr 为空：按提示词约定错误在 stdout 的 {"error": ...} 里，
                # 丢弃它会让重做 prompt 变成空错误——自愈循环三轮盲飞（2026-09-06 real 批次实证）
                parsed = self._parse_summary(outcome.stdout)
                if isinstance(parsed, dict) and parsed.get("error"):
                    failure_text = str(parsed["error"])[:2000]
                    last_error_text = str(parsed["error"])[:500]
            history.append("assistant", code)
            history.append(
                "user",
                f"执行失败（第 {attempt} 次）：\n{failure_text}\n请修正代码后重新输出纯 Python 代码。",
            )
            last_outcome = outcome
            # v1.2 错误路由：字段缺失是规划错误，改码自愈无解——首次分类即跳出，交 Orchestrator 路由
            err_class, _ = self._classify_error(ctx, last_outcome)
            if err_class == ErrorClass.MISSING_COLUMN:
                break

        if result is None:
            outcome = last_outcome
            error_class, missing = (
                self._classify_error(ctx, outcome) if outcome else (ErrorClass.CODE_ERROR, [])
            )
            result = TaskExecutionResult(
                task_id=task_id,
                status="failed",
                # 取 stderr 末尾（真实异常通常在 traceback 尾部）
                error=last_error_text
                or ((outcome.stderr or "")[-500:] if outcome else ""),
                error_class=error_class,
                missing_columns=missing,
                duration_seconds=outcome.duration_seconds if outcome else 0.0,
                # 记**实际跑过的轮数**，不是上限：守卫收容会提前 break，
                # 写 max_attempts 等于让产物谎报"我试了三轮"。
                attempts=attempts_used or max_attempts,
                suggestion=self._suggestion(error_class),
            )

        return self._finish(ctx, task_id, work_dir, result)

    def _finish(
        self, ctx: Any, task_id: int, work_dir: Any, result: TaskExecutionResult
    ) -> AgentMessage:
        data = result.model_dump(mode="json")
        step_file = work_dir / f"step_{task_id}_result.json"
        work_dir.mkdir(parents=True, exist_ok=True)
        step_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="execution_result",
            content=json.dumps(data, ensure_ascii=False),
            artifacts=[str(step_file)],
        )

    def _execute(
        self,
        ctx: Any,
        *,
        task_id: int,
        attempt: int,
        scope: dict[str, Any],
        **params: Any,
    ) -> tuple[Any, bool]:
        """调用 `execute_python`，并把守卫异常收容成"这一个任务的这次失败"。

        以前两个调用点（自愈循环 / 规则包参考实现）都没有 try：`PathViolationError`
        与 `ToolError` 会穿出 `registry.call` → 穿出任务线程 → 穿出编排器的
        `future.result()`，于是整条 run 被判 degraded、`task_states` 清空，
        同一 run 里**已经完成并出图的任务一起作废**。单任务失败本来就有正规出口。

        返回 `(结果, 是否被收容)`：被收容时调用方**不要自愈重试**——守卫拒绝的是
        "这段代码不该被允许跑"，再写三轮也还是越界，只会把预算烧在同一个越界上。
        """
        try:
            return (
                self.registry.call(self.name, "execute_python", ctx, _scope=scope, **params),
                False,
            )
        except ToolError as exc:
            if ctx.transcript is not None:
                ctx.transcript.write(
                    {
                        "event": "task_guard_contained",
                        "task_id": task_id,
                        "attempt": attempt,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        "note": "按单任务失败处理，不作废整条 run",
                    }
                )
            return (
                ExecutionOutcome(success=False, stderr=f"{type(exc).__name__}: {exc}"),
                True,
            )

    def _run_rule_reference(
        self, ctx: Any, task: dict[str, Any], work_dir: Any, rule_params: dict[str, Any]
    ) -> TaskExecutionResult:
        """mock 模式规则任务：执行规则包自带参考实现（与 verify_code 异构，校验器另算）。"""
        task_id = int(task["task_id"])
        rule = ctx.pack.rule(str(rule_params.get("id", "")))
        try:
            data_path = self._primary_path(ctx, task)
        except dataset_scope.DatasetScopeError as exc:
            return TaskExecutionResult(
                task_id=task_id,
                status="failed",
                error=str(exc)[:400],
                error_class=ErrorClass.DATASET_SCOPE,
                suggestion="重新声明本任务要读的表（dataset_refs 必须是 Bundle 里存在的表 id）",
            )
        outcome, _ = self._execute(
            ctx,
            task_id=task_id,
            attempt=1,
            scope={"task_id": task_id, "grants": [], "dataset_refs": self._refs(task)},
            code=rule.reference_code,
            work_dir=work_dir,
            timeout=self._task_timeout(ctx),
            env={
                "DATA_PATH": data_path,
                "ARTIFACTS_DIR": str(ctx.artifacts_dir),
                **self._dataset_env(ctx, task),
                **self._role_env(ctx, rule),
            },
        )
        if outcome.success:
            summary = self._parse_summary(outcome.stdout)
            if (
                summary
                and not summary.get("error")
                and isinstance(summary.get("rows"), int)
            ):
                return TaskExecutionResult(
                    task_id=task_id,
                    status="success",
                    summary=summary,
                    duration_seconds=outcome.duration_seconds,
                    attempts=1,
                )
            return TaskExecutionResult(
                task_id=task_id,
                status="failed",
                error=str((summary or {}).get("error") or "参考实现未输出有效结果")[:500],
                error_class=ErrorClass.CODE_ERROR,
                attempts=1,
                suggestion=self._suggestion(ErrorClass.CODE_ERROR),
            )
        error_class, missing = self._classify_error(ctx, outcome)
        return TaskExecutionResult(
            task_id=task_id,
            status="failed",
            error=(outcome.stderr or "")[-500:],
            error_class=error_class,
            missing_columns=missing,
            duration_seconds=outcome.duration_seconds,
            attempts=1,
            suggestion=self._suggestion(error_class),
        )

    # ------------------------------------------------------------ helpers
    def _answer_from_memory(
        self, ctx: Any, task_id: int, work_dir: Any
    ) -> AgentMessage:
        """记忆回答：不重新加载数据计算，直接读取会话历史返回可读结论。"""
        turns = ctx.session.read_turns(limit=10) if ctx.session else []
        if not turns:
            result = TaskExecutionResult(
                task_id=task_id,
                status="failed",
                error="会话中没有可用的历史记忆",
                error_class=ErrorClass.EMPTY_RESULT,
                suggestion="请先完成至少一轮分析",
                attempts=1,
            )
        else:
            lines: list[str] = []
            head: list[dict[str, Any]] = []
            for turn in turns[-5:]:
                conclusion = clean_summary(
                    turn.get("answer_summary") or turn.get("summary") or ""
                )[:150]
                numbers = turn.get("key_numbers") or {}
                numbers_text = "；".join(f"{k}={v}" for k, v in numbers.items())
                trace = str(turn.get("run_id", ""))[-8:]
                line = (
                    f"第{turn.get('turn')}轮（run_{trace}）：问题：{turn.get('question', '')}"
                    f" → 结论：{conclusion}"
                )
                if numbers_text:
                    line += f"；关键数字：{numbers_text}"
                lines.append(line)
                head.append(
                    {
                        "轮次": turn.get("turn"),
                        "问题": turn.get("question", ""),
                        "关键数字": numbers_text or "-",
                    }
                )
            result = TaskExecutionResult(
                task_id=task_id,
                status="success",
                summary={
                    "rows": len(lines),
                    "columns": ["轮次", "问题", "结论"],
                    "head": head,
                    "memory_text": "\n".join(lines),
                },
                attempts=1,
            )
        data = result.model_dump(mode="json")
        work_dir.mkdir(parents=True, exist_ok=True)
        step_file = work_dir / f"step_{task_id}_result.json"
        step_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="execution_result",
            content=json.dumps(data, ensure_ascii=False),
            artifacts=[str(step_file)],
        )

    # ------------------------------------------------------------ 表级作用域（#19，口径见 core/dataset_scope）
    # 这些方法一律薄转发：executor / visualizer / inspector 必须问同一份"本任务能碰哪几张表"，
    # 各算一遍迟早算出三种授权范围。

    @staticmethod
    def _refs(task: dict[str, Any]) -> list[str]:
        return dataset_scope.scope_refs(task)

    @staticmethod
    def _tables(ctx: Any, task: dict[str, Any]) -> list[Any]:
        return dataset_scope.task_tables(ctx, task)

    def _primary_path(self, ctx: Any, task: dict[str, Any]) -> str:
        return dataset_scope.primary_path(ctx, task)

    def _available_columns(self, ctx: Any, task: dict[str, Any]) -> list[str]:
        return dataset_scope.column_scope(ctx, task)

    def _join_lines(self, ctx: Any, task: dict[str, Any]) -> list[str]:
        tables = self._tables(ctx, task)
        if not tables:
            return []
        pairs = dataset_scope.join_pairs(ctx, task)
        lines = [
            f"多表关联（本任务读 {len(tables)} 张表，各表路径已注入环境变量，只允许读下列出的表）："
        ]
        for table in tables:
            lines.append(
                f"- 表 {table.id}（{table.source_file}，{table.row_count} 行）"
                f"列 {list(table.columns)} → os.environ['DATA_PATH_{str(table.id).upper()}']"
            )
        for pair in pairs:
            if pair["left_column"] != pair["right_column"]:
                # 列名不同是跨源数据的常态；不说清 rename，模型就会 merge 出空表或报错
                lines.append(
                    f"- join 键：{pair['canonical']}（{pair['left']} 侧列名 {pair['left_column']!r}、"
                    f"{pair['right']} 侧列名 {pair['right_column']!r}）——两侧列名不同，"
                    f"先把 {pair['left_column']!r} 重命名为 {pair['canonical']!r} 再 inner join"
                )
            else:
                lines.append(
                    f"- join 键：{pair['canonical']}（{pair['left']} ↔ {pair['right']}，inner join；"
                    "结果行数已由预检确认，禁止无键笛卡尔关联）"
                )
        if not pairs:
            lines.append(
                "- join 键：（未声明，取两表同名列中重叠最高的那个；inner join，"
                "结果行数已由预检确认，禁止无键笛卡尔关联）"
            )
        return lines

    def _dataset_env(self, ctx: Any, task: dict[str, Any]) -> dict[str, str]:
        """声明表的归一化 CSV 路径注入 env：只注入声明过的，天然是一份窄授权。"""
        return {
            f"DATA_PATH_{table_id.upper()}": path
            for table_id, path in dataset_scope.table_paths(ctx, task).items()
        }

    @staticmethod
    def _role_env(ctx: Any, rule: Any) -> dict[str, str]:
        """规则按角色声明数据（`DATA_PATH_AUTH` / `DATA_PATH_ASSETS`）。

        角色名比表 id 稳：id 取决于上传顺序，而规则写的"认证日志/资产台账"是领域事实。
        """
        from agentflow.core.pack import role_env

        pack = getattr(ctx, "pack", None)
        if pack is None or rule is None or getattr(ctx, "bundle", None) is None:
            return {}
        return role_env(pack, ctx.bundle, rule)

    def _rule_role_lines(self, ctx: Any, rule: Any) -> list[str]:
        env = self._role_env(ctx, rule)
        if not env:
            return []
        return [
            "本规则按角色读取以下数据（角色名即环境变量名，不要改用表 id 猜路径）："
            + "；".join(f"{name} → os.environ[{key!r}]" for key, name in env.items())
        ]

    def _task_prompt(self, ctx: Any, task: dict[str, Any]) -> str:
        schema = ctx.schema_profile or {}
        parts = [
            f"任务：{task.get('description')}",
            f"任务编号：{task.get('task_id')}",
            f"必需列：{task.get('required_columns')}",
            f"可用列：{self._available_columns(ctx, task)}",
        ]
        join_lines = self._join_lines(ctx, task)
        if join_lines:
            parts.extend(join_lines)
        refs = task.get("upstream_refs") or []
        if refs:
            parts.append(
                f"上游产物（经授权可读，绝对路径已注入环境变量 UPSTREAM_<任务号>_PATH）：{refs}"
            )
        constraints = getattr(ctx, "constraints", None)
        if constraints:
            parts.append(f"用户约束（必须遵守）：{json.dumps(constraints, ensure_ascii=False)}")
        rule_params = task.get("rule_params") or {}
        if rule_params and getattr(ctx, "pack", None) is not None:
            # 场景包规则任务（real 模式）：检测标准来自规则包，LLM 只做实现，校验器独立把关
            rule = ctx.pack.rule(str(rule_params.get("id", "")))
            parts.append(
                f"检测规则规格（必须严格按此实现，不得自行发明或修改检测标准）：{rule.detection_spec}"
            )
            parts.extend(self._rule_role_lines(ctx, rule))
            parts.append(
                "输出契约：除通用要求外，必须输出 \"findings\" 数组，每条为 "
                "{\"rule_id\": \"" + rule.id + "\", \"subject\": \"...\", \"window_start\": \"...\", "
                "\"window_end\": \"...\", \"metric\": \"...\", \"value\": 数值, \"evidence\": [至多5条原始日志行字典]}；"
                "aggregate 必须含 {\"规则" + rule.id + "命中数\": findings数组长度}；"
                "rows 写 findings 数组长度，columns 写 [\"rule_id\", \"subject\", \"value\"]，"
                "head 每条含 rule_id/subject/value 三个键；"
                "window_start/window_end 使用 YYYY-MM-DD HH:MM:SS 格式（空格分隔，不用 ISO 的 T）。"
            )
        # 闭环回流契约（v1.2）：Inspector 的 suggestion 必须真实进入重做 prompt
        if task.get("_redo_suggestion"):
            parts.append(f"上轮审核未通过，审核建议：{task['_redo_suggestion']}")
        parts.append(f"提示：{task.get('code_hint', '')}")
        parts.append("请输出实现该任务的纯 Python 代码。")
        return "\n".join(parts)

    def _upstream_grants(self, ctx: Any, task: dict[str, Any]) -> dict[str, Any]:
        """把任务声明的 upstream_refs 解析为授权清单（grants）与环境变量（依赖边=授权边）。"""
        env: dict[str, str] = {}
        grants: list[str] = []
        for ref in task.get("upstream_refs") or []:
            target = (ctx.outputs_dir / ref).resolve()
            grants.append(str(target))
            match = re.search(r"step_(\d+)_result", target.name)
            if match:
                env[f"UPSTREAM_{match.group(1)}_PATH"] = str(target)
        return {"env": env, "grants": grants}

    def _parse_summary(self, stdout: str) -> dict[str, Any]:
        try:
            data = extract_json(stdout)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        return {"stdout_tail": stdout[-500:]}

    def _task_timeout(self, ctx: Any) -> int:
        return int(ctx.config.get("execution", {}).get("task_timeout_seconds", 30))

    def _classify_error(self, ctx: Any, outcome: Any) -> tuple[ErrorClass, list[str]]:
        """错误分类 + 结构化缺失列（v1.2：路由与澄清依赖结构化字段，不再反解析 error 文本）。"""
        stderr = outcome.stderr or ""
        lowered = stderr.lower()
        if outcome.timed_out:
            return ErrorClass.TIMEOUT, []
        schema_cols = [c["name"] for c in (ctx.schema_profile or {}).get("columns", [])]
        tokens = re.findall(r"['\"\[]([^'\"\]]{1,20})['\"\]]", stderr)
        missing = [
            token
            for token in tokens
            if token not in schema_cols and not token.strip().isdigit() and token.strip()
        ]
        if "keyerror" in lowered or "不存在" in lowered or "not in index" in lowered:
            if missing:
                return ErrorClass.MISSING_COLUMN, missing[:5]
            return ErrorClass.CODE_ERROR, []
        return ErrorClass.CODE_ERROR, []

    def _suggestion(self, error_class: ErrorClass) -> str:
        return {
            ErrorClass.MISSING_COLUMN: "检查列名或数据源",
            ErrorClass.EMPTY_RESULT: "扩大时间范围或检查筛选条件",
            ErrorClass.TIMEOUT: "简化计算或增大超时",
            ErrorClass.CODE_ERROR: "检查代码逻辑",
            ErrorClass.LLM_ERROR: "检查模型配置或预算",
        }.get(error_class, "检查数据与任务描述")
