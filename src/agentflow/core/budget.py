"""线程安全的 LLM 调用预算计数器（默认上限 30 次，v1.2：计数点 = 每次 API 调用 + token 统计）。"""

from __future__ import annotations

import threading
from typing import Any


class BudgetCounter:
    def __init__(self, limit: int = 30) -> None:
        self.limit = limit
        self._used = 0
        self._tokens: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()
        # 空正文（思考档吃满预算）的逐次记录：real 批次归因要用，见 core/llm.py 的
        # EmptyContentError。不记正文，只记形状。
        self.empty_content: list[dict[str, Any]] = []

    def note_empty_content(self, detail: dict[str, Any]) -> None:
        with self._lock:
            self.empty_content.append(detail)

    def spend(self, n: int = 1) -> bool:
        """尝试消费 n 次调用额度；超限返回 False。"""
        with self._lock:
            if self._used + n > self.limit:
                return False
            self._used += n
            return True

    def add_tokens(self, agent: str, prompt_tokens: int, completion_tokens: int) -> None:
        """按 Agent 维度累计 token 用量（来自响应 usage 字段）。"""
        with self._lock:
            entry = self._tokens.setdefault(agent, {"prompt": 0, "completion": 0})
            entry["prompt"] += int(prompt_tokens)
            entry["completion"] += int(completion_tokens)

    @property
    def token_stats(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {agent: dict(entry) for agent, entry in self._tokens.items()}

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._used)
