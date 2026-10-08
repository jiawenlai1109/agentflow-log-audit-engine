"""并发闸门：把"同时向上游打开几路"这件事收在一个地方。

为什么要有这一层（P4 的实测背书，2026-10-08）：真实形态下 100 个用户同时点分析，
数学上是 `作业并发 × 引擎内部 DAG 并发` = 100 × 3 = **300 路**同时打网关。
而这台网关的实测形状是：**16 路一波全绿（0 拒绝），32 路一波出现 6 次 HTTP 429**。
没有闸门时，我们从"干净档"直接跳到 300——上游被打爆之后，用户看到的不是"排队中"，
而是一堆失败的作业。

三条口径：

1. **上限的出处必须是实测**。按 `环境变量 > 预检缓存里这台站/这个型号的干净档 > 保守占位`
   三级解析，**每一级都记在 `limit_source` 里**。占位值不冒充实测：它出现在每个读数中，
   谁看到都能立刻知道"这个数字没人量过"，而不是以为配额就是那个数。
2. **排队要可见，不许伪装成慢**。等过阈值就发一条 `llm_gate_wait` 事件（等了多久、
   当时几路在飞、几个在等）；等到超时是**一种独立的失败形状**（`LLMGateTimeoutError`），
   带着"配额 N 路、我等了 M 秒"进作业失败原因，而不是被传输层退避循环悄悄吃掉。
3. **一次真实请求占一个槽，退避期间也占着**。放掉再抢回来看起来更省槽位，实际是把
   "排队"变成"抢锁内耗"：两次抢之间别人插进来，谁都可能永远排不上。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from agentflow.core.stats import percentile

# 没有实测读数时的保守占位。**这不是配额**，是"我不知道上限时愿意先付的代价"：
# 宁可排队，也不要 100 个用户的作业一起变成 429。
UNMEASURED_FALLBACK_LIMIT = 4
# 等待上限：排不上就失败。跑一次分析动辄几十秒，为一次调用等 5 分钟已经很长了，
# 再长就是让作业无限期吊着——那比一次明确的失败更坏。
DEFAULT_WAIT_TIMEOUT_S = 300.0
# 等多久发一条留痕事件（毫秒）：0.2s 以内是正常抖动，不值得刷屏。
DEFAULT_NOTICE_MS = 200.0
# 分位数只算最近这么多条等待样本。全留就是一个无声增长的列表（`JobManager._events` 的
# 老账：长跑进程 + 100 用户迟早把它撑爆）。所以读数里窗口大小与"是否截断"一起给——
# **"这段窗口的形状"不许被读成"全程的形状"**，均值与最大值则全程算。
WAIT_WINDOW = 2000
# 排队时一次最多睡这么久（秒）。为什么不直接睡到 `wait_timeout_s`：
# 2026-10-08 变异复测把释放路径上的 `notify()` 摘掉之后，这一片测试 **600 秒没返回**——
# 症状是"槽位明明空了，等待者还在睡剩下的 300 秒"。那种缺陷最难查：它不是红，是停顿，
# 而且只在"谁改了释放路径忘了叫醒"之后才现形。切成 50ms 一小段，代价从"睡满整条超时"
# 降到"最多多等 50ms"；50ms 相对一次上游调用可以忽略，而 `notify` 仍然是快路径。
DEFAULT_WAIT_POLL_S = 0.05


def _positive_int(raw: str | None) -> int | None:
    """环境变量只接受正整数。写 `0`、`abc`、留空都算"没配"，不当成"闸门锁死"。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


class LLMGateTimeoutError(RuntimeError):
    """排不上槽位。带结构化成因，让"排队排爆"与"上游报错"在失败原因里分得开。"""

    def __init__(self, message: str, *, waited_ms: int, gate: dict[str, Any]) -> None:
        super().__init__(message)
        self.waited_ms = waited_ms
        self.gate = gate


class ConcurrencyGate:
    """计数式闸门 + 自带读数。

    读数为什么挂在闸门上而不是让调用方自己算：`max_inflight`、`wait_p95_ms`、`timeouts`
    是这套机制**有没有在工作**的证据。没有它们，闸门就是一个"看起来加上了、其实没人验证过"
    的开关——那正是本项目为"只测函数没测接线"付过学费的形状。

    为什么自己实现而不用 `threading.BoundedSemaphore`：闸门的值要能被读出来（当前在飞几个、
    几个在等、上限是多少、上限从哪来）。`Semaphore` 的 `_value` 是私有实现细节，读它等于
    把"队列深度"建立在别人随时会改的内部字段上——而深度是给运维看的，不许猜。
    """

    def __init__(
        self,
        limit: int,
        *,
        source: str = "explicit",
        wait_timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
        notice_ms: float = DEFAULT_NOTICE_MS,
        wait_window: int = WAIT_WINDOW,
        wait_poll_s: float = DEFAULT_WAIT_POLL_S,
        on_wait: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.limit = max(1, int(limit))
        self.source = source
        self.wait_timeout_s = float(wait_timeout_s)
        self.notice_ms = float(notice_ms)
        self.wait_window = max(1, int(wait_window))
        self.wait_poll_s = max(0.001, float(wait_poll_s))
        self._on_wait = on_wait
        # `RLock` 不是随手写的：排不上槽位那一条路要在**持锁状态下**取一份读数（失败原因里
        # 要带当时的深度）。换成普通 Lock 就是自己锁死自己——这个形状第一次就撞上了，
        # 症状是"跑一次带闸门的测试永久卡住"。
        self._condition = threading.Condition(threading.RLock())
        # 留痕的默认出口（调用方没传 sink 时用）；生产路径上 sink 是逐个槽位传进来的，
        # 因为"这次调用该把事件发给谁"是调用方的事，闸门不该认识 run。
        self.on_wait = on_wait
        self._inflight = 0
        self._waiting = 0
        self._max_inflight = 0
        self._waits: deque[float] = deque(maxlen=self.wait_window)
        self._takes = 0  # 取槽总次数（含没等的）
        self._wait_count = 0  # **真的排过队**的次数
        self._wait_total_ms = 0.0
        self._wait_max_ms = 0.0
        self._timeouts = 0
        self.created_at = time.time()

    # ---------------------------------------------------------------- 读数
    def snapshot(self) -> dict[str, Any]:
        """当前状态 + 累计读数。SSE 首帧、`/api/jobs` 的 queue 块与 evaluation.json 共用。

        分位数走 `core.stats`，与负载压测、上游并发探针**同一把尺**：
        "排队时间变长了"要能和"作业端到端变长了"放进同一张表里比。
        """
        with self._condition:
            waits = sorted(self._waits)
            inflight = self._inflight
            waiting = self._waiting
            highest = self._max_inflight
            timeouts = self._timeouts
            limit = self.limit
            count = self._wait_count
            takes = self._takes
            total = self._wait_total_ms
            worst = self._wait_max_ms
        return {
            "limit": limit,
            "limit_source": self.source,
            "inflight": inflight,
            "waiting": waiting,
            "max_inflight": highest,
            # `takes` = 取过几次槽；`waits` = 其中**真的排过队**的次数。
            # 两者一开始被我合成一个数，于是"10 次取槽、每次都没等"会被读成"排了 10 次队"。
            "takes": takes,
            "waits": count,
            "timeouts": timeouts,
            # 全程算的两个（便宜且不会撑内存）
            "wait_mean_ms": round(total / count, 1) if count else 0.0,
            "wait_max_ms": round(worst, 1),
            # 窗口算的两个：截断与否写在读数里，不让人把窗口读成全程
            "wait_p50_ms": round(percentile(waits, 50), 1),
            "wait_p95_ms": round(percentile(waits, 95), 1),
            "wait_window_size": len(waits),
            "wait_window_capacity": self.wait_window,
            "wait_window_truncated": count > len(waits),
        }

    def reset_counters(self) -> None:
        """清累计量。跑批之间要一份干净读数时用（同一批不许沾上上一批的等待）。"""
        with self._condition:
            self._waits.clear()
            self._takes = 0
            self._wait_count = 0
            self._wait_total_ms = 0.0
            self._wait_max_ms = 0.0
            self._timeouts = 0
            self._max_inflight = self._inflight

    # ---------------------------------------------------------------- 取槽
    @contextmanager
    def slot(
        self,
        *,
        sink: Callable[[dict[str, Any]], None] | None = None,
        model: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """拿一个"在飞"的槽位；拿不到就等到超时。

        `sink` 由**调用方**给（谁发这次调用，谁就知道该把"排队了"说给谁听）。
        闸门不去认识 run、也不搞线程登记表：客户端实例本来就跟着这次调用走，
        登记表则要每一层都记得登记/清除——忘一处的症状是事件静默消失。
        """
        entered = time.monotonic()
        blocked = False
        with self._condition:
            self._waiting += 1
            deadline = entered + self.wait_timeout_s
            while self._inflight >= self.limit:
                blocked = True  # 见过一次"槽已满"才算排过队；`waited_ms > 0` 不算——它连微秒级抖动都算
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._waiting -= 1
                    self._timeouts += 1
                    waited_ms = int((time.monotonic() - entered) * 1000)
                    snapshot = self.snapshot()
                    raise LLMGateTimeoutError(
                        f"上游配额已满（{snapshot['limit']} 路全在飞，"
                        f"另有 {snapshot['waiting']} 个在等，我等了 {waited_ms}ms；"
                        f"上限来源={snapshot['limit_source']}）",
                        waited_ms=waited_ms,
                        gate=snapshot,
                    )
                self._condition.wait(min(remaining, self.wait_poll_s))
            self._waiting -= 1
            self._inflight += 1
            self._max_inflight = max(self._max_inflight, self._inflight)
            inflight_now = self._inflight
            waiting_now = self._waiting

        waited_ms = round((time.monotonic() - entered) * 1000, 1)
        with self._condition:
            self._takes += 1
            if blocked:
                # 只有真的排过队才进样本池：一池 0 会把 p95 拉成"看起来不排队"，
                # 而"取槽次数"另有 `takes` 记着，两个数不混。
                self._waits.append(waited_ms)
                self._wait_count += 1
                self._wait_total_ms += waited_ms
                self._wait_max_ms = max(self._wait_max_ms, waited_ms)
        notice = sink or self._on_wait
        if waited_ms >= self.notice_ms and notice is not None:
            try:
                # 留痕在**拿到槽位之后**发：一条事件写失败不该让这次调用少一个槽
                notice(
                    {
                        "type": "llm_gate_wait",
                        "model": model,
                        "waited_ms": waited_ms,
                        "inflight": inflight_now,
                        "waiting": waiting_now,
                        "limit": self.limit,
                        "limit_source": self.source,
                    }
                )
            except Exception:  # noqa: BLE001 - 推送失败不许影响真实调用
                pass
        try:
            yield {"waited_ms": waited_ms, "inflight": inflight_now}
        finally:
            with self._condition:
                self._inflight -= 1
                self._condition.notify()


# ------------------------------------------------------------------ 解析与单例


def limit_and_source(
    env: dict[str, str] | None = None,
    measured: int | None = None,
) -> tuple[int, str]:
    """三级解析上限。单独成一个函数，是因为"这个数是量出来的还是蒙的"必须能被测试钉住。"""
    environ = env if env is not None else os.environ
    explicit = _positive_int(environ.get("LLM_MAX_CONCURRENCY"))
    if explicit:
        # 运维显式写的数优先于实测：他知道自己在干什么（也可能知道我们不知道的那台站的规矩）
        return explicit, "env"
    if measured and measured > 0:
        return int(measured), "measured"
    return UNMEASURED_FALLBACK_LIMIT, "unmeasured_fallback"


_gate: ConcurrencyGate | None = None
_gate_lock = threading.Lock()


def configure_for_run(limit: int | None = None, *, source: str | None = None) -> ConcurrencyGate:
    """显式换掉进程级闸门：应用层读到实测读数之后开闸，测试也走这里。"""
    global _gate
    with _gate_lock:
        resolved, resolved_source = (limit or UNMEASURED_FALLBACK_LIMIT), (source or "explicit")
        _gate = ConcurrencyGate(resolved, source=resolved_source)
        return _gate


def get_gate() -> ConcurrencyGate:
    """拿进程级闸门。第一次拿的时候按"env > 实测 > 占位"定上限。

    这里不读预检缓存：读缓存是**策略**（哪个型号、多新算新鲜、跨主机怎么办），
    由应用层在开闸时把结果递进来（`configure_for_run`）。引擎只管"几路在飞"，
    不去猜"这台站配了几路"。
    """
    global _gate
    with _gate_lock:
        if _gate is None:
            limit, source = limit_and_source()
            _gate = ConcurrencyGate(limit, source=source)
        return _gate


def snapshot() -> dict[str, Any]:
    """没建过闸门也要报一个读数：SSE 首帧可能早于第一次真实调用。"""
    with _gate_lock:
        if _gate is None:
            limit, source = limit_and_source()
            return ConcurrencyGate(limit, source=source).snapshot()
        return _gate.snapshot()
