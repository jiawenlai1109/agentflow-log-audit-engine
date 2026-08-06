"""线程安全的 LLM 调用预算计数器（默认上限 30 次）。"""

from __future__ import annotations

import threading


class BudgetCounter:
    def __init__(self, limit: int = 30) -> None:
        self.limit = limit
        self._used = 0
        self._lock = threading.Lock()

    def spend(self, n: int = 1) -> bool:
        """尝试消费 n 次调用额度；超限返回 False。"""
        with self._lock:
            if self._used + n > self.limit:
                return False
            self._used += n
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._used)
