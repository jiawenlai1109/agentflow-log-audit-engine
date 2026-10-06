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
        # 型号降级链的逐次记录与"本次实际服务过的型号集合"：跨批次比较 real 数字之前，
        # 必须先能排除"其实是换了型号"这个变量（I3 归因）。见 core/llm.py 的 complete()。
        self.fallbacks: list[dict[str, Any]] = []
        self.models_used: list[str] = []
        # 真实 HTTP 请求次数。`used` 记的是**逻辑调用**（Agent 层每次计 1），
        # 而降级链与提额重试会让真实请求多于逻辑调用——这两个数必须分开记，
        # 否则"换了型号还多打了一次"会从成本指标上隐身。
        self.http_attempts = 0

    def note_empty_content(self, detail: dict[str, Any]) -> None:
        with self._lock:
            self.empty_content.append(detail)

    def note_http_attempt(self) -> None:
        with self._lock:
            self.http_attempts += 1

    def note_fallback(self, detail: dict[str, Any]) -> None:
        with self._lock:
            self.fallbacks.append(detail)

    def note_model_used(self, model: str) -> None:
        """顺序保留（第一次出现的位置），值去重——留痕要能看出这次跑的是哪个主型号。"""
        with self._lock:
            if model not in self.models_used:
                self.models_used.append(model)

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
