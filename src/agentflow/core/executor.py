"""代码执行后端：ExecutionBackend 抽象 + LocalBackend（本地子进程，不依赖沙箱）。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 静态预扫描拒绝的高风险模式（安全与隔离设计 §5.1）
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"\bos\.system\s*\(", "os.system"),
    (r"\bsubprocess\b", "subprocess"),
    (r"\beval\s*\(", "eval"),
    (r"\bexec\s*\(", "exec"),
    (r"\b__import__\b", "__import__"),
    (r"\bsocket\b", "socket"),
    (r"\brequests\b", "requests"),
    (r"\burllib\.request\b", "urllib.request"),
    (r"\bopen\s*\(\s*['\"][A-Za-z]:", "open(绝对路径)"),
    (r"\bopen\s*\(\s*['\"]/", "open(绝对路径)"),
]


def static_scan(code: str) -> list[str]:
    """静态扫描生成代码，返回命中的危险模式列表（空列表 = 通过）。"""
    hits: list[str] = []
    for pattern, label in FORBIDDEN_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            hits.append(label)
    return hits


def _tail(text: str, lines: int = 200) -> str:
    lines_list = text.splitlines()
    if len(lines_list) <= lines:
        return text
    return "\n".join([f"[前 {len(lines_list) - lines} 行已截断]"] + lines_list[-lines:])


@dataclass
class ExecutionOutcome:
    """子进程执行结果。"""

    success: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    violations: list[str] = field(default_factory=list)


class ExecutionBackend(ABC):
    """执行后端抽象：LocalBackend（本地）/ DockerBackend（可选，Phase 5 实现）。"""

    @abstractmethod
    def execute(
        self,
        code: str,
        work_dir: str | Path,
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecutionOutcome:
        """执行代码并返回结果。"""


class LocalBackend(ExecutionBackend):
    """本地子进程执行：python -I、cwd 绑定任务目录、env 白名单、超时、stdout 截断。"""

    # 允许透传的环境变量白名单（不传任何密钥）
    ALLOWED_ENV_KEYS = {
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "LANG",
        "PYTHONIOENCODING",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }

    def __init__(self, python: str | None = None) -> None:
        self.python = python or sys.executable

    def _build_env(self, extra: dict[str, str]) -> dict[str, str]:
        env = {key: os.environ[key] for key in self.ALLOWED_ENV_KEYS if key in os.environ}
        env.update(extra)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def execute(
        self,
        code: str,
        work_dir: str | Path,
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecutionOutcome:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        violations = static_scan(code)
        if violations:
            return ExecutionOutcome(
                success=False,
                stderr=f"静态扫描拒绝：{', '.join(violations)}",
                violations=violations,
            )

        script = work_dir / "script.py"
        script.write_text(code, encoding="utf-8")
        run_env = self._build_env(env or {})
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [self.python, "-I", str(script)],
                cwd=str(work_dir),
                env=run_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return ExecutionOutcome(
                success=proc.returncode == 0,
                returncode=proc.returncode,
                stdout=_tail(proc.stdout or ""),
                stderr=_tail(proc.stderr or ""),
                duration_seconds=round(time.monotonic() - start, 3),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionOutcome(
                success=False,
                stdout=_tail((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
                stderr="执行超时（已终止子进程）",
                duration_seconds=round(time.monotonic() - start, 3),
                timed_out=True,
            )
        except OSError as exc:
            return ExecutionOutcome(
                success=False,
                stderr=f"无法启动子进程：{exc}",
                duration_seconds=round(time.monotonic() - start, 3),
            )
