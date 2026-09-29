"""Orchestrator：调度中枢（DAG 并行 + 任务状态机 + 两阶段提交 + 评审闭环）。"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from agentflow.core.budget import BudgetCounter
from agentflow.core.context import RunContext, new_run_id
from agentflow.core.messages import AgentMessage
from agentflow.core.transcript import TranscriptWriter



# ------------------------------------------------------------ 面向用户的错误正文
# 降级报告是会被截图、被转发的产物：`.venv/Lib/site-packages/...` 与项目绝对路径等于
# 把环境布局寄出去。同一批文本里的 pandas 内部行号（4378 / 3648）还会被追溯率当成
# "报告里冒出没有出处的数字"——隐私与量具噪声在这里是同一件事的两面。
# 完整 traceback 仍然留在 transcript 的 run_failed_traceback 事件与 task 产物里：
# **事实层留全，展示层脱敏**，脱敏不许反过来削弱可诊断性。
_FRAME_RE = re.compile(r'File "[^"]+", line \d+(?:, in [^\n]*)?')
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"<>|]+|/(?:Users|home|var|tmp|opt|usr|private|appdata)[^\s'\"<>|]*", re.IGNORECASE)
_LINE_NO_RE = re.compile(r"\bline \d+\b", re.IGNORECASE)


def display_error(error: Any, limit: int = 300) -> str:
    """把错误正文压成一句可以说给用户听的话：挑出异常摘要行，再抹掉栈帧、绝对路径与行号。

    取"最后一行像异常摘要的那行"而不是首行：子进程 stderr 的末行才是 `KeyError: '利润率'`
    这种可行动信息，首行往往是 `Traceback (most recent call last):` 或被截断剩下的半条路径。
    """
    text = str(error or "").strip()
    if not text:
        return "（无错误详情）"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = lines[-1]
    for line in reversed(lines):
        if re.search(r"(\w*Error|\w*Exception|\w*Warning)\s*:", line):
            summary = line
            break
    cleaned = _FRAME_RE.sub("<栈帧>", summary)
    cleaned = _ABS_PATH_RE.sub("<路径>", cleaned)
    cleaned = _LINE_NO_RE.sub("line <N>", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…（正文已截断，完整记录见产物与 transcript）"
    return cleaned


class Orchestrator:
    """把七个角色 Agent 串成流水线：EXPLORE → PLAN → EXECUTE → VERIFY → VISUALIZE → REPORT → REVIEW。"""

    def __init__(
        self,
        config: dict[str, Any],
        registry: Any,
        agents: dict[str, Any],
        budget: Any = None,
        on_event: Any = None,
        skills: Any = None,
        mcp: Any = None,
        mcp_approvals: dict[str, Any] | None = None,
        run_origin: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.agents = agents
        self.budget = budget
        self.on_event = on_event
        self.skills = skills
        self.mcp = mcp
        # 请求侧带来的批准**原样**交给 hub，由闸门按 grantable 名单过滤。
        # 不在这里先合一遍 config.approvals：合并点必须只有一处，否则"谁覆盖了谁"
        # 就有两个解释，而这类分歧最后都是靠读代码猜的。
        self.mcp_approvals = dict(mcp_approvals or {})
        self.run_origin = dict(run_origin or {})
        self._commit_lock = threading.Lock()

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event({**event, "run_id": getattr(self, "_run_id", None)})
            except Exception:  # noqa: BLE001 - 事件推送失败不影响运行
                pass

    # ------------------------------------------------------------ 主流程
    def run(
        self,
        question: str,
        bundle: Any,
        outputs_root: str | Path,
        session: Any = None,
        max_review_rounds: int | None = None,
        pack: Any = None,
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
            bundle=bundle,
            outputs_dir=outputs_dir,
            config=self.config,
            session=session,
            transcript=transcript,
            budget=budget,
            pack=pack,
            skills=self.skills,
            mcp=self.mcp,
            mcp_approvals=dict(self.mcp_approvals),
            run_origin=dict(self.run_origin),
        )
        self._record_capabilities(ctx, pack)
        started = time.monotonic()
        self._started = started  # 墙钟守卫基准（total_budget_seconds 的执行点）
        status = "failed"
        try:
            self._run_id = run_id
            self._emit({"type": "phase", "phase": "explore"})
            self._explore(ctx)
            self._emit({"type": "phase", "phase": "plan"})
            self._plan(ctx)
            self._emit({"type": "phase", "phase": "execute"})
            self._execute_dag(ctx)
            status, degraded, failure_info = self._classify_state(ctx)
            self._emit({"type": "phase", "phase": "report"})
            self._report(
                ctx,
                degraded=degraded,
                partial=status == "partial",
                failure_info=failure_info,
            )
            if not degraded:
                self._emit({"type": "phase", "phase": "review"})
                self._review(ctx, max_review_rounds)
            self._emit({"type": "done", "status": status, "run_id": run_id})
        except Exception as exc:  # noqa: BLE001 - 运行级兜底
            from agentflow.core.llm import LLMError
            from agentflow.core.pack import PackContractError

            status = "degraded"
            if isinstance(exc, PackContractError):
                # 数据不满足场景包契约是**可行动**的失败：分类与建议由异常自己带出来。
                # 落到下面那条兜底会变成 "UNKNOWN / 检查运行日志"，等于把用户支去翻日志，
                # 而系统其实知道缺哪几列、约定写在哪个文件里。
                ctx.degraded_reason = "pack_contract"
                failure_info = {
                    "error_class": exc.error_class,
                    "error": str(exc)[:500],
                    "suggestion": exc.suggestion,
                    "missing_columns": exc.missing,
                }
            else:
                is_llm_error = isinstance(exc, LLMError)
                ctx.degraded_reason = "llm_error" if is_llm_error else "run_error"
                failure_info = {
                    "error_class": "LLM_ERROR" if is_llm_error else "UNKNOWN",
                    # 这里留原文：脱敏发生在写报告的那一步（`display_error`），
                    # 而 transcript 与本报告共用这一份 failure_info——两处各脱一次敏，
                    # 就会出现"报告干净、别处漏"的分叉。
                    "error": str(exc)[:500],
                    "suggestion": (
                        "检查 LLM 配置（OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL）"
                        if is_llm_error
                        else "检查运行日志"
                    ),
                }
            try:
                self._write_degraded_report(ctx, failure_info)
            except Exception:  # noqa: BLE001 - 降级报告失败不影响状态记录
                pass
            transcript.write({"event": "run_failed", "error": str(exc)})
            transcript.write(
                {"event": "run_failed_traceback", "traceback": traceback.format_exc()[-3000:]}
            )
            self._emit({"type": "error", "error": str(exc)[:500]})
        finally:
            duration = round(time.monotonic() - started, 3)
            self._write_evaluation(ctx, status, duration)
            if ctx.mcp is not None:
                # 外部 server 是子进程：不关就是每跑一次漏一个进程，
                # 而"跑批跑着跑着机器没了内存"这种缺陷最难归因。
                ctx.mcp.transcript = None
                ctx.mcp.close()
            transcript.close()
        return {
            "run_id": run_id,
            "outputs_dir": str(outputs_dir),
            "status": status,
            "report": ctx.report,
            "task_states": ctx.task_states,
            # 回传实际读到的输入快照：CLI/Web 要能回答"这次跑的是哪几份文件、它们的 sha256"
            "bundle": ctx.bundle,
        }

    def _record_capabilities(self, ctx: RunContext, pack: Any) -> None:
        """把"这次运行到底装了什么能力"写进 transcript：装了什么、拒了什么、注入了什么。

        降级必留因同样适用于能力面：关掉一只 skill 与拒装一只 skill 都必须在事实层可见，
        否则一次质量下降会被解释成"模型今天状态不好"。
        """
        if ctx.mcp is not None:
            # hub 是 run 级对象、transcript 也是 run 级才建出来的，所以接线只能发生在这里：
            # 早一步（pipeline 里）transcript 还不存在，晚一步则第一次外部调用不留痕——
            # "出站数据标记"这条安全线就会变成"写在文档里、没落在事实层"。
            ctx.mcp.transcript = ctx.transcript
        if ctx.transcript is None:
            return
        if ctx.skills is not None:
            summary = ctx.skills.summary()
            ctx.transcript.write({"event": "skills_loaded", **summary})
            for injection in summary["injected"]:
                ctx.transcript.write({"event": "skill_injected", **injection})
            # 关停与拒装也要进事实层：装载发生在 run 目录建出来之前，
            # 那时还没有 transcript 可写，所以由这里补上（装载器自带 transcript 时不会走到这）。
            for entry in summary["disabled"]:
                ctx.transcript.write({"event": "skill_disabled", **entry})
            for entry in summary["refused"]:
                ctx.transcript.write({"event": "skill_refused_no_escalation", **entry})
        if ctx.mcp is not None:
            # 走 hub 的同一个方法，不是在这儿再算一遍：判定与留痕必须是同一次解析的结果
            accepted, rejected = ctx.mcp.resolve_approvals(ctx.mcp_approvals)
            ctx.transcript.write(
                {
                    "event": "mcp_attached",
                    "servers": ctx.mcp.summary()["servers"],
                    "max_calls": ctx.mcp.config.max_calls,
                    # 谁签的分开记：运维的、请求想要的、请求签字里算数的、被丢掉的。
                    # 合成一个 approvals 字段就回答不了"这次写能力是谁批的"。
                    "approvals_from_config": dict(ctx.mcp.config.approvals),
                    "approvals_requested": dict(ctx.mcp_approvals),
                    "approvals_accepted": accepted,
                    "approvals_rejected": rejected,
                    "grantable": sorted(ctx.mcp.config.grantable),
                }
            )
        if ctx.run_origin:
            # 运行来源单独一条：不是谁请求的、批准了什么，这些要能在事实层查回来
            ctx.transcript.write({"event": "run_origin", **ctx.run_origin})

    # ------------------------------------------------------------ 阶段
    def _explore(self, ctx: RunContext) -> None:
        message = self._request(ctx, "explorer", "explore_request", ctx.question)
        reply = self.agents["explorer"].run(ctx, message)
        ctx.schema_profile = json.loads(reply.content)

    def _plan(self, ctx: RunContext) -> None:
        message = self._request(ctx, "planner", "plan_request", ctx.question)
        reply = self.agents["planner"].run(ctx, message)
        ctx.task_list = json.loads(reply.content)

    def _preflight_joins(self, ctx: RunContext, pending: list[int]) -> list[int]:
        """派发前 join 预检（M2-3）：返回被拒任务 id。

        判定完全确定性——任务声明的 `dataset_refs` / `join_keys` 交给 `core/join.py`
        算基数，LLM 不参与"这个 join 好不好"的判断（I2）。被拒任务不进 DAG：一次笛卡尔积
        一旦执行，代价是内存与一条已被下游引用的错误结果，而拦它只需要两个键列的计数。
        """
        from agentflow.core.join import preflight

        # 别名只来自场景包：通用分析没有领域知识判断"src_ip 就是主机"，猜出来等于编造
        aliases = getattr(ctx.pack, "column_aliases", None) or None
        rejected: list[int] = []
        for task in (ctx.task_list or {}).get("tasks", []):
            tid = int(task.get("task_id", 0))
            refs = list(task.get("dataset_refs") or [])
            if tid not in pending or len(refs) < 2:
                continue
            check = preflight(ctx.bundle, refs, list(task.get("join_keys") or []), aliases)
            ctx.join_preflight[str(tid)] = check.as_dict()
            if ctx.transcript is not None:
                ctx.transcript.write(
                    {"event": "join_preflight", "task_id": tid, **check.as_dict()}
                )
            self._emit(
                {
                    "type": "join_preflight",
                    "task_id": tid,
                    "refs": refs,
                    "ok": check.ok,
                    "reason": check.reason_code,
                }
            )
            if check.ok:
                continue
            rejected.append(tid)
            ctx.results[tid] = {
                "task_id": tid,
                "status": "failed",
                "error": str(check),
                "error_class": "JOIN_PRECHECK",
                "missing_columns": [],
                "suggestion": (
                    "改用两表共有的键列，或在场景包 data_convention 里补列映射"
                    "（如 src_ip → 主机）后重新提问"
                ),
            }
        return rejected

    def _execute_dag(self, ctx: RunContext) -> None:
        """DAG 调度（v1.2）：波次并行 + 墙钟守卫 + 波次末错误路由（MISSING_COLUMN → 重规划）。"""
        max_workers = max(
            1, int(self.config.get("execution", {}).get("max_concurrency", 3))
        )
        states: dict[int, str] = {}
        while True:
            tasks = ctx.task_list["tasks"]
            deps = {int(t["task_id"]): list(t.get("depends_on", [])) for t in tasks}
            task_by_id = {int(t["task_id"]): t for t in tasks}
            for tid in task_by_id:
                states.setdefault(tid, "PENDING")

            # 派发前 join 预检（M2-3）：坏 join 不进 DAG，标 FAILED 后交给错误路由
            for tid in self._preflight_joins(
                ctx, [t for t, state in states.items() if state == "PENDING"]
            ):
                states[tid] = "FAILED"
            pending = {tid for tid, s in states.items() if s == "PENDING"}

            # 墙钟守卫：每次派发前检查 deadline（恢复与回滚设计 §4.2）
            if self._deadline_exceeded(ctx):
                for tid in pending:
                    states[tid] = "CANCELLED"
                ctx.wall_clock_timeout = True
                break

            ready = [tid for tid in sorted(pending) if all(d not in pending for d in deps[tid])]
            if not ready:
                for tid in pending:
                    states[tid] = "SKIPPED"
                break
            to_run: list[int] = []
            for tid in ready:
                if any(states.get(d) in ("FAILED", "SKIPPED") for d in deps[tid]):
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

            # 波次末错误路由：计划修订则重建调度结构进入下一波
            if self._route_planning_failures(ctx, states):
                continue
            if not any(s == "PENDING" for s in states.values()):
                break
        ctx.task_states = states

    def _deadline_exceeded(self, ctx: RunContext) -> bool:
        seconds = int(ctx.config.get("execution", {}).get("total_budget_seconds", 120))
        return (time.monotonic() - getattr(self, "_started", time.monotonic())) > seconds

    def _max_replan(self, ctx: RunContext) -> int:
        return max(1, int(ctx.config.get("execution", {}).get("max_replan_rounds", 1)))

    def _route_planning_failures(self, ctx: RunContext, states: dict[int, str]) -> bool:
        """错误路由（v1.2）：MISSING_COLUMN 是规划错误，路由给 Planner 增量修订剩余任务。

        M2-3 起 JOIN_PRECHECK 走同一条通道：坏 join 同样是规划错误（键选错/列名不统一），
        不该由执行器去试错——派发前已经算准了它连不上或会膨胀。

        返回 True 表示计划已修订、调度器应重建结构进入下一波；不可修订时产出非阻塞澄清请求。
        """
        if ctx.replan_used >= self._max_replan(ctx):
            return False
        routable = ("MISSING_COLUMN", "JOIN_PRECHECK")
        fresh = [
            tid
            for tid, s in states.items()
            if s == "FAILED"
            and (ctx.results.get(tid) or {}).get("error_class") in routable
            and not (ctx.results[tid].get("_routed"))
        ]
        if not fresh:
            return False
        tid = fresh[0]
        ctx.results[tid]["_routed"] = True
        remaining = [
            task
            for task in ctx.task_list["tasks"]
            if states.get(int(task["task_id"])) == "PENDING"
        ]
        missing = (ctx.results.get(tid) or {}).get("missing_columns") or []
        join_rejects = [
            {
                "task_id": int(key),
                "refs": check.get("refs"),
                "reason": check.get("reason"),
                "detail": check.get("detail"),
            }
            for key, check in ctx.join_preflight.items()
            if not check.get("ok")
        ]
        payload = {
            "question": ctx.question,
            "failed_task": next(
                (t for t in ctx.task_list["tasks"] if int(t["task_id"]) == tid), {}
            ),
            "missing_columns": missing,
            "remaining_tasks": remaining,
            "join_rejects": join_rejects,
        }
        ctx.replan_used += 1
        self._emit(
            {
                "type": "error_routed",
                "task_id": tid,
                "error_class": (ctx.results[tid] or {}).get("error_class", "MISSING_COLUMN"),
                "action": "replan",
                "missing_columns": missing,
            }
        )
        if ctx.transcript is not None:
            ctx.transcript.write(
                {
                    "event": "error_routed",
                    "task_id": tid,
                    "error_class": (ctx.results[tid] or {}).get("error_class", "MISSING_COLUMN"),
                    "action": "replan",
                }
            )
        reply = self.agents["planner"].run(
            ctx,
            self._request(
                ctx, "planner", "replan_request", json.dumps(payload, ensure_ascii=False)
            ),
        )
        reply_data = json.loads(reply.content)
        if reply.kind == "clarify_request":
            ctx.clarify = reply_data
            self._emit({"type": "clarify_request", **reply_data})
            return False
        revised = reply_data.get("tasks") or []
        if not revised:
            return False
        # 合并修订：只动未执行任务（PENDING），已完成/失败任务保留原定义
        kept = [
            task
            for task in ctx.task_list["tasks"]
            if states.get(int(task["task_id"]), "PENDING") != "PENDING"
        ]
        ctx.task_list = {**ctx.task_list, "tasks": kept + revised}
        return True

    def _task_unit(self, ctx: RunContext, task: dict[str, Any]) -> tuple[int, str]:
        """执行单元 = 生成代码 → 子进程执行 → Inspector 审核 → 两阶段提交 → Visualizer。"""
        task_id = int(task["task_id"])
        result: dict[str, Any] = {}
        is_memory = (
            task.get("code_hint") == "memory_answer"
            or "基于会话记忆回答" in str(task.get("description", ""))
        )
        if is_memory:
            # 记忆回答任务：不审核、不画图，直接提交结果
            message = self._request(
                ctx, "executor", "execute_task", json.dumps(task, ensure_ascii=False)
            )
            reply = self.agents["executor"].run(ctx, message)
            result = json.loads(reply.content)
            ctx.results[task_id] = result
            if result["status"] != "success" and ctx.replan_used < self._max_replan(ctx):
                # v1.2 recall 回退：记忆回答失败 → 一次正常重规划（宁可多算不可答非所问）
                ctx.replan_used += 1
                payload = {
                    "question": ctx.question,
                    "failed_task": task,
                    "missing_columns": [],
                    "remaining_tasks": [],
                    "disable_memory": True,
                }
                self._emit(
                    {"type": "error_routed", "task_id": task_id, "error_class": "MEMORY_FALLBACK", "action": "replan"}
                )
                reply = self.agents["planner"].run(
                    ctx,
                    self._request(
                        ctx,
                        "planner",
                        "replan_request",
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                if reply.kind == "revised_task_list":
                    revised = json.loads(reply.content).get("tasks") or []
                    if revised:
                        new_task = {**revised[0], "task_id": task_id}
                        message = self._request(
                            ctx, "executor", "execute_task", json.dumps(new_task, ensure_ascii=False)
                        )
                        result = json.loads(self.agents["executor"].run(ctx, message).content)
                        ctx.results[task_id] = result
            if result["status"] == "success":
                self._commit(ctx, task_id)
                ctx.figures[task_id] = {
                    "task_id": task_id,
                    "chart_type": "none",
                    "title": "",
                    "file_path": None,
                    "note": "",
                }
                self._emit({"type": "task", "task_id": task_id, "status": "SUCCEEDED"})
                return task_id, "SUCCEEDED"
            self._emit({"type": "task", "task_id": task_id, "status": "FAILED"})
            return task_id, "FAILED"
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
                self._emit({"type": "task", "task_id": task_id, "status": "FAILED"})
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
                # 审核结论随结果留存：供 evaluation.json 统计校验覆盖率与重做率
                ctx.results[task_id]["verdict"] = {
                    "status": verdict["status"],
                    "verification": verdict.get("verification"),
                    "redos": redo - 1,
                    "checks": [
                        f"{check.get('rule')}:{check.get('level')}"
                        for check in verdict.get("checks", [])
                    ],
                }
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
                self._emit({"type": "task", "task_id": task_id, "status": "SUCCEEDED"})
                return task_id, "SUCCEEDED"
            # Inspector FAIL：携带建议重做
            task = {**task, "_redo_suggestion": verdict.get("suggestion")}
        ctx.results[task_id] = {
            **result,
            "status": "failed",
            "error": f"Inspector 连续 {max_redos} 次未通过：" + str(verdict.get("suggestion", "")),
            "error_class": "EMPTY_RESULT",
        }
        self._emit({"type": "task", "task_id": task_id, "status": "FAILED"})
        return task_id, "FAILED"

    def _classify_state(
        self, ctx: RunContext
    ) -> tuple[str, bool, dict[str, Any] | None]:
        """运行状态分类：success / partial / degraded（区分设计内降级与异常）。"""
        states = ctx.task_states
        tasks = (ctx.task_list or {}).get("tasks", [])
        failed = [tid for tid, state in states.items() if state in ("FAILED", "SKIPPED")]
        if ctx.wall_clock_timeout:
            # v1.2：墙钟超时属于运行级降级（恢复与回滚设计 §4.2）
            ctx.degraded_reason = "wall_clock_timeout"
            return "degraded", True, self._failure_info(ctx, failed)
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
        """关键失败：关闭部分结果时；或用户问题中明确提到的字段缺失（如 TC-03）。

        v1.2：优先消费结构化 missing_columns 字段，不再从 error 文本反解析。
        """
        allow_partial = bool(
            ctx.config.get("execution", {}).get("allow_partial_results", True)
        )
        if not allow_partial:
            return True
        for tid in failed:
            result = ctx.results.get(tid, {})
            if result.get("error_class") != "MISSING_COLUMN":
                continue
            missing_names = set(result.get("missing_columns") or [])
            if not missing_names:
                # 兼容无结构化字段的旧结果
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
        # 字段兜底：real 模式 LLM 常把 error/suggestion 输出成 null，
        # 直接透传会让 FailureInfo 的 pydantic 校验抛异常（把降级路径变成 run_error）
        return {
            "error_class": first.get("error_class") or "UNKNOWN",
            # 子进程 stderr 里带绝对路径与库内部行号：报告正文只收一句摘要，
            # 原文仍在 task 产物与 transcript 里（事实层不删，展示层脱敏）
            "error": display_error(first.get("error") or "任务执行失败"),
            "suggestion": first.get("suggestion") or "请检查列名或数据源",
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
                "clarify": ctx.clarify,
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
        ctx.review_ran = True
        for _ in range(max_rounds):
            payload = json.dumps(
                {
                    "report_path": ctx.report["report_path"],
                    "question": ctx.question,
                    "results": {str(k): v for k, v in ctx.results.items()},
                    "degraded": ctx.report.get("degraded", False),
                    "sections": ctx.report.get("sections"),
                    "dataset_rows": (ctx.schema_profile or {}).get("row_count"),
                },
                ensure_ascii=False,
            )
            review = json.loads(
                self.agents["critic"]
                .run(ctx, self._request(ctx, "critic", "review_report", payload))
                .content
            )
            infra = review.get("infrastructure_error")
            if infra:
                # 评审器自己没跑成（LLM 不可用、预算耗尽）。这条既不并进 critic_pass，也不触发重写：
                # 重写修不好一个没接通的评审器，只会把剩余预算烧在无效重试上。#13 原来的形态是
                # 把它折成一条 severity=low 的**内容** issue ⇒ 运维级失败长得像内容判红，
                # 而运行照样以 success 收口——门禁对这类失败完全失明。
                ctx.critic_passed = False
                ctx.review_infra_error = str(infra)[:300]
                if ctx.transcript is not None:
                    ctx.transcript.write(
                        {
                            "event": "review_unavailable",
                            "reason": ctx.review_infra_error,
                            "note": "语义评审未完成：不折算成内容结论，也不触发自愈重写",
                        }
                    )
                return
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
            "token_usage": getattr(ctx.budget, "token_stats", None),
            "replan_used": ctx.replan_used,
            "clarify": ctx.clarify,
            # M2-3：派发前 join 预检的逐任务判定（含基数与被拒原因），数字可追回键列计数
            "join_preflight": ctx.join_preflight,
            "task_states": ctx.task_states,
            "degraded_reason": ctx.degraded_reason,
            "chart_success": chart_success if chart_attempted else None,
            "critic_pass": ctx.critic_passed,
            # "评审跑没跑"与"评审判没判红"是两件事（#13）。只有 critic_pass 一栏时，
            # 一次 LLM 预算耗尽的运行与一次"报告真有问题"的运行长得一模一样。
            "review_ran": ctx.review_ran,
            "review_infra_error": ctx.review_infra_error,
            # 数据集行数是报告"审计范围 N 条"这类结论数字的确定性出处，评估器据此核对
            "dataset_rows": (ctx.schema_profile or {}).get("row_count"),
            # 多源报告头部会逐张表报行数（"auth.csv 282 行、assets.csv 24 行…"）。
            # 这些数由画像器真读出来，属证据；不落到 evaluation.json 的话，
            # 追溯率会把"报告引用了每张表的行数"判成造数——那是量具的洞，不是系统的洞。
            "dataset_tables": [
                {"source_file": table.get("source_file"), "row_count": table.get("row_count")}
                for table in ((ctx.schema_profile or {}).get("tables") or [])
            ],
            # 逐任务选图结果：M4 的"关掉 skill 后指标要能动"就量这个字段。
            # 只记结论数字不够，得记到"每个任务画了什么"这一粒度，否则差在哪没人说得清。
            "chart_types": {str(k): v.get("chart_type") for k, v in ctx.figures.items()},
            "chart_notes": {
                str(k): (v.get("note") or "") for k, v in ctx.figures.items() if v.get("chart_type") == "none"
            },
            # 事实层要能回答"这次跑的是哪个场景包、哪几规则"：只有 run_config 里的 pack
            # 文件 hash 是不够的，而 hash 又只有跑批才写（CLI/Web 不写 run_config.json）。
            "pack": (
                {
                    "name": ctx.pack.name,
                    "version": ctx.pack.version,
                    "rule_ids": [rule.id for rule in ctx.pack.rules],
                    "required_columns": list(ctx.pack.required_columns),
                }
                if ctx.pack is not None
                else None
            ),
            "skills": (ctx.skills.summary() if ctx.skills is not None else None),
            "mcp": (ctx.mcp.summary() if ctx.mcp is not None else None),
            "external_evidence": ctx.external_evidence,
            "results": {str(k): v for k, v in ctx.results.items()},
        }
        (ctx.outputs_dir / "evaluation.json").write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_degraded_report(
        self, ctx: RunContext, failure_info: dict[str, Any]
    ) -> None:
        """LLM/运行级失败时写出可读的降级报告，避免"只留下 json 无报告"。"""
        report_path = ctx.outputs_dir / "report.md"
        # 失败原因走 display_error：这一段是会被截图、被转发的正文。原始 traceback
        # 仍然完整留在 transcript 的 run_failed_traceback 事件里（事实层不删东西）。
        shown = display_error(failure_info.get("error", ""))
        text = (
            "# 数据分析报告（未完成）\n\n"
            f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"- 失败原因：{shown}\n"
            f"- 错误分类：{failure_info.get('error_class', 'UNKNOWN')}\n"
            f"- 建议：{failure_info.get('suggestion', '')}\n"
        )
        report_path.write_text(text, encoding="utf-8")
        ctx.report = {
            "report_path": str(report_path),
            "degraded": True,
            "failure_info": {**failure_info, "error": shown},
            "summary": "分析未完成：" + shown[:100],
        }

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
            # v1.2：transcript 全量记录（事实层，不进 prompt），不再截断
            ctx.transcript.write(
                {
                    "run_id": ctx.run_id,
                    "sender": "orchestrator",
                    "receiver": receiver,
                    "kind": kind,
                    "content": content,
                }
            )
        self._emit(
            {
                "type": "message",
                "sender": "orchestrator",
                "receiver": receiver,
                "kind": kind,
            }
        )
        return message
