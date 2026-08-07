"""Orchestrator：调度中枢（DAG 并行 + 任务状态机 + 两阶段提交 + 评审闭环）。"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from agentflow.core.budget import BudgetCounter
from agentflow.core.context import RunContext, new_run_id
from agentflow.core.messages import AgentMessage
from agentflow.core.transcript import TranscriptWriter


class Orchestrator:
    """把七个角色 Agent 串成流水线：EXPLORE → PLAN → EXECUTE → VERIFY → VISUALIZE → REPORT → REVIEW。"""

    def __init__(
        self,
        config: dict[str, Any],
        registry: Any,
        agents: dict[str, Any],
        budget: Any = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.agents = agents
        self.budget = budget
        self._commit_lock = threading.Lock()

    # ------------------------------------------------------------ 主流程
    def run(
        self,
        question: str,
        data_path: str,
        outputs_root: str | Path,
        session: Any = None,
        max_review_rounds: int | None = None,
    ) -> dict[str, Any]:
        run_id = new_run_id()
        outputs_dir = Path(outputs_root) / run_id
        outputs_dir.mkdir(parents=True, exist_ok=True)
        transcript = TranscriptWriter(outputs_dir / "transcript.jsonl")
        budget = self.budget or BudgetCounter(
            int(self.config.get("execution", {}).get("max_llm_calls", 30))
        )
        ctx = RunContext(
            run_id=run_id,
            question=question,
            data_path=str(Path(data_path).resolve()),
            outputs_dir=outputs_dir,
            config=self.config,
            session=session,
            transcript=transcript,
            budget=budget,
        )
        started = time.monotonic()
        status = "failed"
        try:
            self._explore(ctx)
            self._plan(ctx)
            self._execute_dag(ctx)
            status, degraded, failure_info = self._classify_state(ctx)
            self._report(
                ctx,
                degraded=degraded,
                partial=status == "partial",
                failure_info=failure_info,
            )
            if not degraded:
                self._review(ctx, max_review_rounds)
        except Exception as exc:  # noqa: BLE001 - 运行级兜底
            status = "failed"
            transcript.write({"event": "run_failed", "error": str(exc)})
        finally:
            duration = round(time.monotonic() - started, 3)
            self._write_evaluation(ctx, status, duration)
            transcript.close()
        return {
            "run_id": run_id,
            "outputs_dir": str(outputs_dir),
            "status": status,
            "report": ctx.report,
            "task_states": ctx.task_states,
        }

    # ------------------------------------------------------------ 阶段
    def _explore(self, ctx: RunContext) -> None:
        message = self._request(ctx, "explorer", "explore_request", ctx.question)
        reply = self.agents["explorer"].run(ctx, message)
        ctx.schema_profile = json.loads(reply.content)

    def _plan(self, ctx: RunContext) -> None:
        message = self._request(ctx, "planner", "plan_request", ctx.question)
        reply = self.agents["planner"].run(ctx, message)
        ctx.task_list = json.loads(reply.content)

    def _execute_dag(self, ctx: RunContext) -> None:
        tasks = ctx.task_list["tasks"]
        states: dict[int, str] = {task["task_id"]: "PENDING" for task in tasks}
        deps = {task["task_id"]: list(task.get("depends_on", [])) for task in tasks}
        task_by_id = {task["task_id"]: task for task in tasks}
        pending = set(states)
        max_workers = max(
            1, int(self.config.get("execution", {}).get("max_concurrency", 3))
        )
        while pending:
            ready = [tid for tid in pending if all(d not in pending for d in deps[tid])]
            if not ready:
                for tid in pending:
                    states[tid] = "SKIPPED"
                break
            to_run: list[int] = []
            for tid in ready:
                dep_states = [states[dep] for dep in deps[tid]]
                if any(s in ("FAILED", "SKIPPED") for s in dep_states):
                    states[tid] = "SKIPPED"
                else:
                    to_run.append(tid)
            pending -= set(ready)
            if to_run:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(self._task_unit, ctx, task_by_id[tid]): tid
                        for tid in to_run
                    }
                    for future in as_completed(futures):
                        tid, new_state = future.result()
                        states[tid] = new_state
        ctx.task_states = states

    def _task_unit(self, ctx: RunContext, task: dict[str, Any]) -> tuple[int, str]:
        """执行单元 = 生成代码 → 子进程执行 → Inspector 审核 → 两阶段提交 → Visualizer。"""
        task_id = int(task["task_id"])
        result: dict[str, Any] = {}
        max_redos = max(
            1, int(ctx.config.get("execution", {}).get("max_inspector_redos", 3))
        )
        for redo in range(1, max_redos + 1):
            message = self._request(
                ctx, "executor", "execute_task", json.dumps(task, ensure_ascii=False)
            )
            reply = self.agents["executor"].run(ctx, message)
            result = json.loads(reply.content)
            ctx.results[task_id] = result
            if result["status"] == "failed":
                return task_id, "FAILED"
            verdict_msg = self._request(
                ctx,
                "inspector",
                "inspect_result",
                json.dumps(
                    {
                        "task": task,
                        "result": result,
                        "question": ctx.question,
                    },
                    ensure_ascii=False,
                ),
            )
            verdict = json.loads(self.agents["inspector"].run(ctx, verdict_msg).content)
            if verdict["status"] != "FAIL":
                self._commit(ctx, task_id)
                figure_msg = self._request(
                    ctx,
                    "visualizer",
                    "visualize",
                    json.dumps(
                        {"task": task, "result": ctx.results[task_id]},
                        ensure_ascii=False,
                    ),
                )
                ctx.figures[task_id] = json.loads(
                    self.agents["visualizer"].run(ctx, figure_msg).content
                )
                return task_id, "SUCCEEDED"
            # Inspector FAIL：携带建议重做
            task = {**task, "_redo_suggestion": verdict.get("suggestion")}
        ctx.results[task_id] = {
            **result,
            "status": "failed",
            "error": f"Inspector 连续 {max_redos} 次未通过：" + str(verdict.get("suggestion", "")),
            "error_class": "EMPTY_RESULT",
        }
        return task_id, "FAILED"

    def _classify_state(
        self, ctx: RunContext
    ) -> tuple[str, bool, dict[str, Any] | None]:
        """运行状态分类：success / partial / degraded（区分设计内降级与异常）。"""
        states = ctx.task_states
        tasks = (ctx.task_list or {}).get("tasks", [])
        failed = [tid for tid, state in states.items() if state in ("FAILED", "SKIPPED")]
        if not failed:
            ctx.degraded_reason = None
            return "success", False, None
        if not tasks or len(failed) == len(tasks):
            ctx.degraded_reason = "all_tasks_failed"
            return "degraded", True, self._failure_info(ctx, failed)
        if self._is_critical_failure(ctx, failed):
            ctx.degraded_reason = "critical_task_failed"
            return "degraded", True, self._failure_info(ctx, failed)
        ctx.degraded_reason = "partial_failure"
        return "partial", False, None

    def _is_critical_failure(self, ctx: RunContext, failed: list[int]) -> bool:
        """关键失败：关闭部分结果时；或用户问题中明确提到的字段缺失（如 TC-03）。"""
        allow_partial = bool(
            ctx.config.get("execution", {}).get("allow_partial_results", True)
        )
        if not allow_partial:
            return True
        for tid in failed:
            result = ctx.results.get(tid, {})
            if result.get("error_class") != "MISSING_COLUMN":
                continue
            # 提取错误信息中引号内的疑似列名，判断是否出现在用户问题中（如 TC-03 的"利润"）
            missing_names = {
                name
                for name in re.findall(r"['\"]([^'\"]{1,20})['\"]", result.get("error", ""))
                if name
            }
            if any(name in ctx.question for name in missing_names):
                return True
        return False

    def _failure_info(
        self, ctx: RunContext, failed: list[int]
    ) -> dict[str, Any]:
        failed_results = [
            ctx.results[tid] for tid in failed if tid in ctx.results
        ]
        if not failed_results:
            return {
                "error_class": "UNKNOWN",
                "error": "任务执行失败",
                "suggestion": "请检查任务与数据",
            }
        first = failed_results[0]
        return {
            "error_class": first.get("error_class", "UNKNOWN"),
            "error": first.get("error", "任务执行失败"),
            "suggestion": first.get("suggestion", "请检查列名或数据源"),
        }

    def _report(
        self,
        ctx: RunContext,
        degraded: bool = False,
        partial: bool = False,
        failure_info: dict[str, Any] | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "question": ctx.question,
                "results": {str(k): v for k, v in ctx.results.items()},
                "figures": {str(k): v for k, v in ctx.figures.items()},
                "time_base": (ctx.task_list or {}).get("time_base"),
                "degraded": degraded,
                "partial": partial,
                "failure_info": failure_info,
            },
            ensure_ascii=False,
        )
        reply = self.agents["reporter"].run(
            ctx, self._request(ctx, "reporter", "compose_report", payload)
        )
        ctx.report = json.loads(reply.content)

    def _review(self, ctx: RunContext, max_review_rounds: int | None = None) -> None:
        max_rounds = max(
            1,
            max_review_rounds
            or int(ctx.config.get("execution", {}).get("max_review_rounds", 2)),
        )
        ctx.critic_passed = False
        for _ in range(max_rounds):
            payload = json.dumps(
                {
                    "report_path": ctx.report["report_path"],
                    "question": ctx.question,
                    "results": {str(k): v for k, v in ctx.results.items()},
                    "degraded": ctx.report.get("degraded", False),
                },
                ensure_ascii=False,
            )
            review = json.loads(
                self.agents["critic"]
                .run(ctx, self._request(ctx, "critic", "review_report", payload))
                .content
            )
            if review["verdict"] == "PASS":
                ctx.critic_passed = True
                return
            # 不通过：携带 issues 返回 Reporter 重写
            report_payload = json.dumps(
                {
                    "question": ctx.question,
                    "results": {str(k): v for k, v in ctx.results.items()},
                    "figures": {str(k): v for k, v in ctx.figures.items()},
                    "time_base": (ctx.task_list or {}).get("time_base"),
                    "degraded": ctx.report.get("degraded", False),
                    "failure_info": ctx.report.get("failure_info"),
                    "review_issues": review["issues"],
                },
                ensure_ascii=False,
            )
            reply = self.agents["reporter"].run(
                ctx, self._request(ctx, "reporter", "rewrite_report", report_payload)
            )
            ctx.report = json.loads(reply.content)

    # ------------------------------------------------------------ 提交与评估
    def _commit(self, ctx: RunContext, task_id: int) -> None:
        """两阶段提交：work/<task_id> → artifacts/step_<task_id>（临时文件 + 原子 rename）。"""
        source = ctx.task_work_dir(task_id) / f"step_{task_id}_result.json"
        if not source.exists():
            return
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        target = ctx.artifacts_dir / f"step_{task_id}_result.json"
        temp = ctx.artifacts_dir / f".step_{task_id}.tmp"
        temp.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        with self._commit_lock:
            temp.replace(target)
            manifest = ctx.artifacts_dir / "manifest.json"
            entries = []
            if manifest.exists():
                entries = json.loads(manifest.read_text(encoding="utf-8"))
            entries.append(
                {"task_id": task_id, "file": str(target), "committed_at": time.time()}
            )
            manifest.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        ctx.results[task_id]["intermediate_file"] = str(target)

    def _write_evaluation(self, ctx: RunContext, status: str, duration: float) -> None:
        figures = ctx.figures
        chart_attempted = sum(
            1
            for figure in figures.values()
            if figure.get("chart_type") and figure["chart_type"] != "none"
        )
        chart_success = sum(1 for figure in figures.values() if figure.get("file_path"))
        evaluation = {
            "run_id": ctx.run_id,
            "question": ctx.question,
            "status": status,
            "duration_seconds": duration,
            "llm_calls": getattr(ctx.budget, "used", 0),
            "task_states": ctx.task_states,
            "degraded_reason": ctx.degraded_reason,
            "chart_success": chart_success if chart_attempted else None,
            "critic_pass": ctx.critic_passed,
            "results": {str(k): v for k, v in ctx.results.items()},
        }
        (ctx.outputs_dir / "evaluation.json").write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _request(
        self, ctx: RunContext, receiver: str, kind: str, content: str
    ) -> AgentMessage:
        message = AgentMessage(
            run_id=ctx.run_id,
            sender="orchestrator",
            receiver=receiver,
            kind=kind,
            content=content,
        )
        if ctx.transcript is not None:
            ctx.transcript.write(
                {
                    "run_id": ctx.run_id,
                    "sender": "orchestrator",
                    "receiver": receiver,
                    "kind": kind,
                    "content": content[:1000],
                }
            )
        return message
