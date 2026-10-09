"""跑一个 job：把"执行一次分析"从 HTTP 路由里的闭包搬出来，供两种宿主调用。

原来那段逻辑长在 `app/routers/jobs.py` 的 `submit_analysis` 内部闭包里：参数（问题、
数据源、场景包、批准链）只活在那个进程的内存里。所以"换进程跑"做不到，而 P0 的读数就是
它的账单——杀掉进程后 28 个 job 永远停在非终态。搬出来之后：本进程的 dispatcher 线程与
`scripts/worker.py` 调用的是**同一份代码**，没有第二套执行语义可以漂移。

两条口径：

- **事件仍然一处发**：`eventlog.append_event`（经 `manager.publish` 的 sink）+ 进度写库。
  进度写库这条不能省：SSE 判断"流可以关了"看的是库里的状态。
- **异常按形状分类**，不是一律 `failed`：LLM 预算耗尽与代码报错要分开，前者是运维问题
  （#13 的立场），后者会来自我修复循环。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from app import access, config, queueing
from app.jobs import JobManager
# `run_analysis` 在模块顶层导入，不在函数里延迟导：执行路径搬到这一份代码之后，
# 全局只剩**一个**可替换的跑批缝隙。缝隙有两处（这里 + routers/jobs.py）迟早出现
# "改了 A 处桩、跑的是 B 处"，而受理层本来就已经 import 了 pipeline，延迟没有省下什么。
from agentflow.pipeline import run_analysis

logger = logging.getLogger("agentflow.worker")

PHASE_PROGRESS: dict[str, int] = {
    "plan": 10,
    "explore": 25,
    "execute": 55,
    "review": 75,
    "report": 90,
}

# ---------------------------------------------------------------- 部署形态（P5-3）

WEB_DISPATCH_ENV = "WEB_DISPATCH"
_ON = {"", "1", "true", "on", "yes", "web", "both"}
_OFF = {"0", "false", "off", "no", "external", "worker"}


def web_dispatch_enabled() -> bool:
    """受理进程要不要**自己认领作业**。默认 on = 今天这套能直接跑的形状（一个进程就够）。

    为什么默认是"关不掉也行"：这个仓库现在就是单进程在跑，把默认改成 off 等于要求用户改环境
    才能让系统动起来——那是把形态切换的代价推给他。分进程部署是**加**一个 worker 进程并把
    Web 那侧的认领关掉，两步都是显式的。

    认不出来的值一律按 on 处理并打一条 warning：**静默把执行关掉**的表现是"作业永远排队、
    接口一切正常"，那是最难查的一类故障；宁可多打一行日志。
    只在调用时读环境变量：形态是部署的事，不是 import 时刻的事实（与路径同一口径）。
    """
    raw = (os.getenv(WEB_DISPATCH_ENV) or "").strip().lower()
    if raw in _ON:
        return True
    if raw in _OFF:
        return False
    logger.warning(
        "%s=%r 认不出来，按 on 处理（这个进程仍会认领作业）。可接受：on/1/true/yes 或 off/0/false/external",
        WEB_DISPATCH_ENV,
        raw,
    )
    return True


def dispatch_form() -> dict[str, Any]:
    """这个进程的执行形态，给运维读数用（`/api/jobs` 的 `dispatch` 字段、SSE 首帧与启动日志）。

    `claim_loops` 是**本进程**的认领循环数：off 时它是 0（不是 `WORKER_CONCURRENCY` 那个数）。
    不报这个就会把"这个进程有几个循环"读成"平台有几个循环"——分进程部署下两者差着进程数。

    `llm_gate_scope` 明写"每进程各一份"：这是这个形态**已知没做**的那一格（不是配额系统，
    上游并发总量在分进程后是 `limit × 调用方进程数`）。写进读数而不是藏在注释里，是因为
    "认得但不生效"的口径一旦没人说，运维就会把两个进程各 16 路读成"平台还是 16 路"——
    而那台网关实测 32 路要出 429。显式收敛的办法是每进程设 `LLM_MAX_CONCURRENCY=limit/进程数`。
    """
    enabled = web_dispatch_enabled()
    loops = queueing.default_worker_concurrency() if enabled else 0
    return {
        "dispatch_in_web": enabled,
        "shape": "单进程（受理 + 执行）" if enabled else "分进程（Web 只受理，执行在外部 worker）",
        "claim_loops": loops,
        "llm_gate_scope": "per_process",
        "note": (
            "本进程自己认领作业；要拆成独立 worker：起 `python scripts/worker.py` 并把本进程设成 "
            f"{WEB_DISPATCH_ENV}=off"
            if enabled
            else "本进程不认领作业。若没有任何外部 worker 在跑，作业会一直停在队列里——"
            "这是配置意图，不是故障；`queue.running` 会一直是 0"
        ),
    }


def _publish(manager: JobManager, job_id: str, event: dict[str, Any]) -> None:
    manager.publish(job_id, event)


def resolve_sources(spec: dict[str, Any], user_id: int) -> Any:
    """把 spec 里的**引用**换成可读的数据源，归属校验在读取时重做一遍。

    spec 里存的是 `bundle:<id>` / `dataset:<id>`，不是绝对路径：把路径写进 jobs 表
    等于多留一份可外泄的位置信息（#15 的口径），而 worker 换机器时那条路径也不再成立。
    """
    from fastapi import HTTPException

    from app.routers.bundles import load_bundle_for_analysis

    ref = str(spec.get("source_ref") or "")
    if ref.startswith("bundle:"):
        bundle_id = ref.split(":", 1)[1]
        try:
            return load_bundle_for_analysis(bundle_id, {"id": user_id})
        except HTTPException:
            raise
    if ref.startswith("dataset:"):
        # 读取时重做归属校验，而且用的是**同一条**判据（access）：认领作业的人可能来自
        # 另一个进程，而"提交时选得到、跑的时候说不是你的"就是两处各写一遍迟早分叉的样子。
        # 传进判据的是**作业行里的 user_id**，不是"当前请求者"——作业属于谁就在为谁读数据。
        from app import access

        row = access.dataset_row({"id": user_id}, int(ref.split(":", 1)[1]))
        if not row:
            raise RuntimeError("数据集不存在或不属于该用户")
        return str(row["path"])
    if ref.startswith("path:"):
        # 会话续轮的兼容形状（sessions 表存的就是路径）。新入口不走这条路。
        return ref.split(":", 1)[1]
    raise RuntimeError(f"spec 里没有可识别的数据源引用：{ref!r}")


def execute_job(claim: dict[str, Any], manager: JobManager) -> str:
    """跑掉一个已认领的 job，返回它的终态。"""
    job_id = str(claim["job_id"])
    user_id = int(claim["user_id"])
    worker = str(claim["worker"])
    spec = queueing.get_spec(claim)
    sources = resolve_sources(spec, user_id)
    # 一次运行的产物落在**作业行自己那条企业树**下（`outputs/org/<id>/<run_id>/`）。
    # 用的是库里的 org_id，不是"当前请求者"的企业：认领这个 job 的进程可能压根没有请求者
    # （scripts/worker.py），而"跑在谁的树上"必须与"这条 job 属于哪家企业"是同一个答案，
    # 否则共享判据读到的报告与文件实际所在的地方会对不上。
    org_id = int(claim.get("org_id") or access.UNASSIGNED_ORG)
    session_id = spec.get("session_id") or None
    sessions_root = None
    if session_id:
        session_org = access.session_org(user_id, str(session_id))
        if session_org is None:
            # 排队期间会话被删了（提交与认领之间的那段时间）。这里退回作业自己那棵树里
            # 新建一份空上下文——与改造前引擎的行为一致（它也是 mkdir 一个新目录），
            # 但把这条分支写在明处：会话树的位置由**会话行**决定，行没了才轮到作业行。
            session_org = org_id
        sessions_root = config.sessions_root(session_org)
    last = {"status": "failed", "run_id": None, "error": None}

    def on_event(event: dict[str, Any]) -> None:
        _publish(manager, job_id, event)
        if event.get("type") == "phase":
            queueing.note_progress(job_id, PHASE_PROGRESS.get(str(event.get("phase")), 50))
        if event.get("type") == "done":
            last["status"] = str(event.get("status"))
            last["run_id"] = event.get("run_id")

    lease_stop = threading.Event()

    def keep_lease() -> None:
        while not lease_stop.wait(20.0):
            if not queueing.heartbeat(job_id, worker):
                logger.warning("job %s 的租约被收走，本次运行结果不再回写", job_id)
                return

    lease = threading.Thread(target=keep_lease, name=f"lease-{job_id}", daemon=True)
    lease.start()
    try:
        result = run_analysis(
            question=str(spec.get("question") or ""),
            sources=sources,
            mode=str(spec.get("mode") or "mock"),
            outputs_root=config.org_outputs_root(org_id),
            session_id=session_id,
            sessions_root=sessions_root,
            on_event=on_event,
            pack=spec.get("pack"),
            mcp_approvals=spec.get("mcp_approvals") or None,
            run_origin=spec.get("run_origin") or None,
        )
        status = str(result.get("status") or last["status"])
        queueing.finish(job_id, worker, status, run_id=result.get("run_id") or last["run_id"])
        return status
    except Exception as exc:  # noqa: BLE001 - 一次运行失败不该带走宿主
        reason = f"{type(exc).__name__}: {str(exc)[:400]}"
        queueing.finish(job_id, worker, "failed", error=reason)
        _publish(manager, job_id, {"type": "error", "error": str(exc)[:500]})
        logger.warning("job %s 失败：%s", job_id, reason)
        return "failed"
    finally:
        lease_stop.set()


def reclaim_at_boot() -> list[str]:
    """启动时立刻收一次过期租约。不等 sweeper 的第一轮（15s），因为那 15 秒里
    用户看到的就是一群永远 running 的 job。"""
    return queueing.reclaim_expired(limit=200)


class Dispatcher:
    """N 个认领循环。线程版给 Web 进程用，`scripts/worker.py` 用同一个类换进程。"""

    def __init__(self, manager: JobManager, workers: int | None = None, *, poll_s: float = 1.0) -> None:
        self.manager = manager
        self.workers = workers if workers is not None else queueing.default_worker_concurrency()
        self.poll_s = poll_s
        self.worker = queueing.worker_id()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sweeper: threading.Thread | None = None
        self._life = threading.Lock()
        self._started = False

    def start(self) -> None:
        """幂等：lifespan 起一次、提交路径再叫一次，不会开出两份认领循环。

        开两份就是同一个 job 被自己的两个线程抢——队列看起来更快，实际是重复计费。
        """
        with self._life:
            if self._started:
                return
            self._started = True
            # 必须先清停止标志再开线程：这是个模块级单例，一次 stop() 之后
            # 线程会永远"起跑即退出"——测试之间、以及任何不退出进程的重启都会踩到。
            # （test_mock_profit 那条超时就是这么来的。）
            self._stop.clear()
            self._start_locked()

    def _start_locked(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(target=self._loop, name=f"dispatcher-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self._sweeper = threading.Thread(target=self._reclaim_loop, name="lease-sweeper", daemon=True)
        self._sweeper.start()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        with self._life:
            self._started = False
        self._stop.set()
        queueing.notify()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads = []

    def _loop(self) -> None:
        while not self._stop.is_set():
            claim = queueing.claim(self.worker)
            if claim is None:
                queueing.wait_for_work(self.poll_s)
                continue
            try:
                execute_job(claim, self.manager)
            except Exception as exc:  # noqa: BLE001 - 宿主不被单个 job 打死
                logger.error("认领的 job 没跑完：%s: %s", type(exc).__name__, exc)
                queueing.finish(str(claim["job_id"]), self.worker, "failed", error=str(exc)[:400])

    def _reclaim_loop(self) -> None:
        while not self._stop.wait(15.0):
            recovered = queueing.reclaim_expired()
            if recovered:
                logger.warning("租约过期，退回队列：%s", ", ".join(recovered))
