"""线程安全的消息日志：transcript.jsonl（事实来源，只追加）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


from agentflow.core import trace


class TranscriptWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        # 链路标识盖在这一处，而不是十几个 `write()` 调用点（P6-2）。理由不是省事：
        # 漏一处的症状**不是报错**，而是"按 trace 查回来的记录缺一段"——那种缺口只有恰好去查的
        # 人会发现，而查的人已经是带着疑问来的。写在这里，任何新加的转录种类自动带上。
        # 没绑定就不写这一格（不写 `null`）：缺席读起来是"这条运行没有链路"，
        # 而把上一个作业的串留在这一行上是假关联。
        chain = trace.current()
        if chain and "trace_id" not in record:
            record = {**record, "trace_id": chain}
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()

    def __enter__(self) -> "TranscriptWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
