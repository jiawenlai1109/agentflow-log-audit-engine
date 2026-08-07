"""Orchestrator：调度中枢（DAG 并行 + 任务状态机 + 两阶段提交 + 评审闭环）。"""

from __future__ import annotations

import json
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
        max_review_rounds: int = 2,
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
        status = "success"
        try:
            self._explore(ctx)
            self._plan(ctx)
            self._execute_dag(ctx)
            degraded = self._run_degraded(ctx)
            self._report(ctx, degraded)
            self._review(ctx, max_review_rounds)
            if degraded or (ctx.report or {}).get("degraded"):
                status = "degraded"
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
        for redo in range(1, 4):
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
            "error": "Inspector 连续 3 次未通过：" + str(verdict.get("suggestion", "")),
            "error_class": "EMPTY_RESULT",
        }
        return task_id, "FAILED"

    def _report(self, ctx: RunContext, degraded: bool) -> None:
        failure_info = None
        if degraded:
            failed = [
                ctx.results[tid]
                for tid in ctx.results
                if ctx.results[tid].get("status") == "failed"
            ]
            if failed:
                first = failed[0]
                failure_info = {
                    "error_class": first.get("error_class", "UNKNOWN"),
                    "error": first.get("error", "任务执行失败"),
                    "suggestion": first.get("suggestion", "请检查列名或数据源"),
                }
        payload = json.dumps(
            {
                "question": ctx.question,
                "results": {str(k): v for k, v in ctx.results.items()},
                "figures": {str(k): v for k, v in ctx.figures.items()},
                "time_base": (ctx.task_list or {}).get("time_base"),
                "degraded": degraded,
                "failure_info": failure_info,
            },
            ensure_ascii=False,
        )
        reply = self.agents["reporter"].run(
            ctx, self._request(ctx, "reporter", "compose_report", payload)
        )
        ctx.report = json.loads(reply.content)

    def _review(self, ctx: RunContext, max_rounds: int) -> None:
        for _ in range(max(1, max_rounds)):
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

    def _run_degraded(self, ctx: RunContext) -> bool:
        tasks = (ctx.task_list or {}).get("tasks", [])
        if not tasks:
            return True
        return any(
            ctx.task_states.get(task["task_id"], "PENDING") in ("FAILED", "SKIPPED")
            for task in tasks
        )

    def _write_evaluation(self, ctx: RunContext, status: str, duration: float) -> None:
        evaluation = {
            "run_id": ctx.run_id,
            "question": ctx.question,
            "status": status,
            "duration_seconds": duration,
            "llm_calls": getattr(ctx.budget, "used", 0),
            "task_states": ctx.task_states,
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
