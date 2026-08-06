"""线程安全的消息日志：transcript.jsonl（事实来源，只追加）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class TranscriptWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
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
