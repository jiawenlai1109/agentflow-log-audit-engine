"""链路标识只有一份：生成、形状校验、绑定、跨线程携带、子进程交接（P6）。

为什么单独立一个文件（口径参照 `app/access.py` 与 `app/paths.py`）：`trace_id` 要同时被
Web 受理层、队列、引擎、LLM 闸门和沙箱子进程读到。任何一处自己 `uuid4()` 一份，
"凭一个 id 查回整条链路"就退化成"看运气查到哪一段"——而 P0 那笔账（一次运行失败只能
靠时间戳在两份日志里对齐）就是这么来的。

四个 id 不许互相冒充：

- `trace_id`：**一次提交**的链路标识，受理第一跳生成，从此不改；
- `job_id`：作业行标识。幂等重放时同一个 `job_id` 会被多次提交，库里那条始终留第一次的 trace；
- `run_id`：引擎那次运行的目录名，重规划/重试不换行，换作业才换；
- `worker` / `claimed_by`：**进程**标识，回答"在哪个进程跑的"，与"哪条链路"是两个问题（P5-3 定过）。

`trace_id` 只作留痕，**不参与任何判定**。代码里出现"按 trace 放行/按 trace 归属"就是越界：
它是客户端可以自带、也可能被网关改写的一个串，归属判据只有 `app/access.py` 那一份。
"""

from __future__ import annotations

import contextvars
import functools
import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from uuid import uuid4

ENV_KEY = "TRACE_ID"  # 交给沙箱子进程的那一格（executor 的白名单按它放行）
HEADER = "X-Trace-Id"  # 入站可沿用：网关已经起过一条链路时不该在受理层断掉
MAX_LEN = 32  # 上限就是 jobs.trace_id 那一列的长度（String(32)）：超长的 id 落库会被静默截断
_SHAPE = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_LEN}}}")  # 白名单：字母数字与 - _ ，换行/空格/控制字符一律不收

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    """生成一条链路标识。全局唯一，长度正好落在列宽内。"""
    return uuid4().hex[:MAX_LEN]


def is_usable(value: Any) -> bool:
    """形状对不对。这里就是唯一一份判据，调用方不复述规则。"""
    return isinstance(value, str) and bool(_SHAPE.fullmatch(value))


def adopt(inbound: Any) -> tuple[str, bool]:
    """决定这次提交用哪条 trace：入站形状对就沿用，否则新建。

    返回 `(生效的 id, 是否沿用了入站)`。第二个值是给调用方的**出口**：入站带了一个不能用的 id
    时，受理层必须能说出"我另起了一条"，而不是让客户端以为自己那条贯穿到底了——
    静默改写别名的链路标识，和静默截断是同一类谎。

    为什么不"截断到 32 就收下"：两个不同的上游 id 截到同样长度会撞成一条，于是两条无关链路
    在报表里变成一条。宁可另起一条新的（诚实：这条链路从受理层开始）。
    """
    text = str(inbound).strip() if isinstance(inbound, str) else ""
    if is_usable(text):
        return text, True
    return new_trace_id(), False


def current() -> str | None:
    """本线程/本任务当前绑定的 trace。没有就是 None——调用方不许拿空串冒充"没绑"。"""
    return _current.get()


def describe() -> str:
    """给日志用的一格。没绑时是 `-`，不是空串：空串在日志里读起来像"这条没有链路"的省略。"""
    return current() or "-"


@contextmanager
def bind(value: str | None) -> Iterator[None]:
    """把 trace 绑到当前执行上下文。形状不对就当**没有**（绑成 None），而不是留着上一次的。

    为什么是"清掉"而不是"不动"：认领循环是长命线程，一个循环会连着跑好几个作业。
    如果某个作业的 spec 里没 trace 就保留上一个作业的值，日志就会把两次运行写成同一条链路——
    那是假关联，比没 trace 更糟。宁可让人看见"这一条没绑"。
    """
    token = _current.set(value if is_usable(value) else None)
    try:
        yield
    finally:
        _current.reset(token)


def wrap(callable: Callable[..., Any]) -> Callable[..., Any]:
    """把"此刻的上下文"带进另一个线程。

    `ThreadPoolExecutor.submit` **不会**自动复制 contextvar（只有 asyncio 的任务才会）。
    `_execute_dag` 那棵树是线程池跑的，所以任务单元必须在提交处包一次——否则 trace 走到
    并发执行那一层就断，而断掉的地方正是最需要它的时候（工具调用与子进程都在那里发生）。
    """
    captured = contextvars.copy_context()
    return functools.partial(captured.run, callable)


__all__ = [
    "ENV_KEY",
    "HEADER",
    "MAX_LEN",
    "adopt",
    "bind",
    "current",
    "describe",
    "is_usable",
    "new_trace_id",
    "wrap",
]
