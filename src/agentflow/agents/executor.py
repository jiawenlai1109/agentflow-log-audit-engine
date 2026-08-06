"""ExecutorAgent：代码执行者（Code Interpreter：写码-执行-调试自愈循环）。"""

from __future__ import annotations

import json
import re
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core.llm import LLMError, extract_json
from agentflow.core.messages import AgentMessage
from agentflow.schemas.result import ErrorClass, TaskExecutionResult


EXECUTOR_SYSTEM = """你是高级数据工程师。根据任务编写 pandas 代码并从 CSV 提取数据。
要求：
- 只输出纯 Python 代码，禁止 Markdown 围栏；
- 读取 os.environ['DATA_PATH'] 为 df；
- 结果用 print(json.dumps({'rows': ..., 'columns': [...], 'head': [...]})) 输出 JSON；
- 必须 try/except 捕获异常并 print 错误信息；
- 禁止访问网络、禁止写源数据目录。"""


class ExecutorAgent(BaseAgent):
    name = "executor"
    system_prompt = EXECUTOR_SYSTEM

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        task = json.loads(message.content) if isinstance(message.content, str) else message.content
        task_id = int(task["task_id"])
        work_dir = ctx.task_work_dir(task_id)
        messages = [{"role": "user", "content": self._task_prompt(ctx, task)}]
        result: TaskExecutionResult | None = None
        last_outcome: Any = None

        for attempt in range(1, 4):
            try:
                code = self.complete(ctx, self._task_prompt(ctx, task), messages=messages)
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
            outcome = self.registry.call(
                self.name,
                "execute_python",
                ctx,
                code=code,
                work_dir=work_dir,
                timeout=self._task_timeout(ctx),
                env={
                    "DATA_PATH": ctx.data_path,
                    "ARTIFACTS_DIR": str(ctx.artifacts_dir),
                },
            )
            if outcome.success:
                result = TaskExecutionResult(
                    task_id=task_id,
                    status="success",
                    summary=self._parse_summary(outcome.stdout),
                    duration_seconds=outcome.duration_seconds,
                    attempts=attempt,
                )
                break
            messages += [
                {"role": "assistant", "content": code},
                {
                    "role": "user",
                    "content": f"执行失败（第 {attempt} 次）：\n{outcome.stderr[:2000]}\n请修正代码后重新输出纯 Python 代码。",
                },
            ]
            last_outcome = outcome
        else:
            error_class = self._classify_error(ctx, task, last_outcome)
            result = TaskExecutionResult(
                task_id=task_id,
                status="failed",
                error=(last_outcome.stderr or "")[:500],
                error_class=error_class,
                duration_seconds=last_outcome.duration_seconds,
                attempts=3,
                suggestion=self._suggestion(error_class),
            )

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

    # ------------------------------------------------------------ helpers
    def _task_prompt(self, ctx: Any, task: dict[str, Any]) -> str:
        schema = ctx.schema_profile or {}
        return (
            f"任务：{task.get('description')}\n"
            f"任务编号：{task.get('task_id')}\n"
            f"必需列：{task.get('required_columns')}\n"
            f"可用列：{[c['name'] for c in schema.get('columns', [])]}\n"
            f"提示：{task.get('code_hint', '')}\n"
            "请输出实现该任务的纯 Python 代码。"
        )

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

    def _classify_error(self, ctx: Any, task: dict[str, Any], outcome: Any) -> ErrorClass:
        stderr = (outcome.stderr or "").lower()
        if outcome.timed_out:
            return ErrorClass.TIMEOUT
        schema_cols = [c["name"] for c in (ctx.schema_profile or {}).get("columns", [])]
        tokens = re.findall(r"['\"\[]([^'\"\]]{1,20})['\"\]]", outcome.stderr or "")
        unknown = [t for t in tokens if t not in schema_cols]
        if "keyerror" in stderr or "不存在" in stderr or "not in index" in stderr:
            return ErrorClass.MISSING_COLUMN if unknown else ErrorClass.CODE_ERROR
        return ErrorClass.CODE_ERROR

    def _suggestion(self, error_class: ErrorClass) -> str:
        return {
            ErrorClass.MISSING_COLUMN: "检查列名或数据源",
            ErrorClass.EMPTY_RESULT: "扩大时间范围或检查筛选条件",
            ErrorClass.TIMEOUT: "简化计算或增大超时",
            ErrorClass.CODE_ERROR: "检查代码逻辑",
            ErrorClass.LLM_ERROR: "检查模型配置或预算",
        }.get(error_class, "检查数据与任务描述")
