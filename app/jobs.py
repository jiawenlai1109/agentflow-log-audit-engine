"""JobManager：线程池执行 + 每 job 事件缓冲（SSE 数据源），线程安全。

这一版管住三件 100 并发下会出事的事：

1. **worker 数可配**。以前 `max_workers` 写死 2：100 个 job 进来，约 91 个在排队
   （P0 实测峰值非终态 job = 93，见 `.appdata/load_before_100.json`）。分析阶段绝大多数
   时间是等 LLM 上游返回，属于 I/O 等待，2 个线程等于自己掐住自己的吞吐。
   但**线程不是免费的**：登录那类 CPU 活（一次 PBKDF2 实测 104.8ms）不吃这个参数的好处，
   而每个 run 内部还有 `max_concurrency` 路并发 ⇒ 真正的上游并发是
   `WORKER_CONCURRENCY × execution.max_concurrency`，那一层由 P4 的闸门管，不在这里假装解决。
2. **事件缓冲必须有界**。`_events` 以前只增不减：一个长跑进程 + 100 用户迟早把它撑爆。
   回收已完成且超过 `EVENT_KEEP_DONE` 个的 job；被回收后**明确报"事件已过窗口"**，
   静默返回空列表会被前端读成"这次运行没有任何过程"——那是又一次把"没数据"说成"没问题"。
3. **队列深度要可见**。前端拿得到"在跑几个、排着几个"，才谈得上显示排队位置；
   拿不到的时候用户只会觉得"点了没反应"。
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


def default_worker_concurrency() -> int:
    """默认仍是 2 —— 这是量出来的，不是保守。

    P0 的两轮对照（同一把尺，`scripts/load_test.py`，100 用户 × 1 轮）：

    | worker | 作业端到端 p50 | 登录 p95 | 上传 p50 | 提交 p50 |
    |---|---|---|---|---|
    | 2（`load_before_100.json`） | 9.4s | 8977.9ms | 3177.6ms | 1396.5ms |
    | 8（`load_after_workers8.json`） | **1.4s** | **14378.3ms** | **6812.4ms** | **3622.5ms** |

    加 worker 确实解开了队列（93 个排队 → 端到端快 6.7 倍），但它把排队时间**搬进了请求路径**：
    单进程里 8 个 run × 每 run 内部 3 路 = 24 个线程与 HTTP 线程抢 CPU/GIL，登录/上传/提交
    全线恶化 2–4 倍，还出现 2 次连接被 reset。所以默认值不能改——**这个旋钮要在
    "API 进程与 worker 进程分开"之后才有意义**（P2/P5）。分开之前把它调到 8，
    等于用 100 个用户的等待时间换 100 个作业的完成时间。
    """
    return max(1, int(os.getenv("WORKER_CONCURRENCY", "2")))


class JobManager:
    def __init__(
        self,
        max_workers: int | None = None,
        *,
        keep_done: int | None = None,
        events_per_job: int | None = None,
        sink: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        # sink 是"事件顺便落库"的钩子（生产上是 app.eventlog.append_event）。放在这里而不是
        # 每个调用点各写一遍：漏一处，那条流就还是只活在内存里。
        self.sink = sink
        self.sink_failures = 0
        self.max_workers = max_workers if max_workers is not None else default_worker_concurrency()
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self._lock = threading.Lock()
        # OrderedDict + 完成即淘汰：dict 的插入顺序在并发下不等于完成顺序，
        # 所以"淘汰最老的"要看 _finished 这条队列，不能看 keys() 的第一项。
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._done: dict[str, bool] = {}
        self._running: set[str] = set()
        self._queued: list[str] = []
        self._finished: "OrderedDict[str, None]" = OrderedDict()
        self.keep_done = int(keep_done if keep_done is not None else os.getenv("EVENT_KEEP_DONE_JOBS", "200"))
        self.events_per_job = int(
            events_per_job if events_per_job is not None else os.getenv("EVENTS_PER_JOB_CAP", "500")
        )
        # 有界：回收过谁必须还记得（否则"is_done 变 False"就是在谎报"这个 job 没跑完"），
        # 但绝不能为此留一个只增不减的 set —— 100 用户长跑进程会把它撑爆。
        self.evicted: "deque[str]" = deque(maxlen=max(16, self.keep_done))

    # ------------------------------------------------------------------ 提交与执行

    def submit(self, job_id: str, fn: Callable[[], None]) -> None:
        with self._lock:
            self._events.setdefault(job_id, [])
            self._queued.append(job_id)
        self._executor.submit(self._wrap, job_id, fn)

    def _wrap(self, job_id: str, fn: Callable[[], None]) -> None:
        with self._lock:
            if job_id in self._queued:
                self._queued.remove(job_id)
            self._running.add(job_id)
        try:
            fn()
        finally:
            with self._lock:
                self._running.discard(job_id)
                self._done[job_id] = True
                self._finished[job_id] = None
                self._evict_locked()

    def _evict_locked(self) -> None:
        """只淘汰已完成且超额的 job。在跑与排队的永不淘汰（它们还要被读）。"""
        while len(self._finished) > self.keep_done:
            oldest, _ = self._finished.popitem(last=False)
            self._events.pop(oldest, None)
            self._done.pop(oldest, None)
            self.evicted.append(oldest)

    # ------------------------------------------------------------------ 事件

    def publish(self, job_id: str, event: dict[str, Any]) -> None:
        if self.sink is not None:
            try:
                self.sink(job_id, event)
            except Exception:  # noqa: BLE001 - 落库失败要降级可见，但不能打死运行
                with self._lock:
                    self.sink_failures += 1
        with self._lock:
            buffer = self._events.setdefault(job_id, [])
            if len(buffer) < self.events_per_job:
                buffer.append(event)
            elif len(buffer) == self.events_per_job:
                # 到顶就停住不再收，并留一条"被截断"的事件：
                # 前端不能在同一条流上悄悄少看几段过程
                buffer.append({"type": "events_truncated", "cap": self.events_per_job})

    def snapshot(self, job_id: str, index: int) -> tuple[list[dict[str, Any]], int, bool]:
        """返回 `(本页事件, 总条数, 是否已过窗口)`。第三个值不许省——静默的空=谎报。"""
        with self._lock:
            if job_id not in self._events and job_id in self.evicted:
                return [], 0, True
            events = list(self._events.get(job_id, []))
        return events[index:], len(events), False

    def is_done(self, job_id: str) -> bool:
        """事件被回收不等于"这个 job 没跑完"：那会把完成态谎报成未知态。"""
        with self._lock:
            return bool(self._done.get(job_id)) or job_id in self.evicted

    def buffered(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._events

    # ------------------------------------------------------------------ 队列可见性

    def depth(self) -> dict[str, int]:
        with self._lock:
            return {
                "workers": self.max_workers,
                "running": len(self._running),
                "queued": len(self._queued),
            }
