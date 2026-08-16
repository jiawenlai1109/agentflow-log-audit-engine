"""JobManager：线程池执行 + 每 job 事件缓冲（SSE 数据源），线程安全。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class JobManager:
    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._done: dict[str, bool] = {}

    def submit(self, job_id: str, fn: Callable[[], None]) -> None:
        with self._lock:
            self._events.setdefault(job_id, [])
        self._executor.submit(self._wrap, job_id, fn)

    def _wrap(self, job_id: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        finally:
            with self._lock:
                self._done[job_id] = True

    def publish(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.setdefault(job_id, []).append(event)

    def snapshot(self, job_id: str, index: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            events = list(self._events.get(job_id, []))
        return events[index:], len(events)

    def is_done(self, job_id: str) -> bool:
        with self._lock:
            return self._done.get(job_id, False)
