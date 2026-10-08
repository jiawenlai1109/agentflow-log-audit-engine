"""敏感写入口的限流（P4-3）：登录、建号这类口子不能让人拿请求刷。

挡的是两件事，分开计数才挡得住：

- **撞同一个账号**（口令字典 / 撞库）：按**用户名**计一笔。
- **拿一批用户名各试一次**（枚举）：按**来源**计一笔，额度比单账号宽得多，
  但它会在扫几十个账号时先响——只按用户名计的限流对枚举完全没有成本。

四条口径，都是这片里容易被含混过去的：

1. **这是每个进程各自的数，不是全平台的**。计数器住在进程内存里，Web 拆成几个受理进程，
   实际允许的速率就是 `限额 × 进程数`。这条不自曝就会被读成"已经限到 10 次/分钟了"——
   所以 `snapshot()` 里带着 `scope="per_process"`，而要做成平台级的得把计数挪进库里
   （一张 `auth_attempts` 表 + 一条迁移），那是单独一片，不在这儿偷偷做半份。
2. **固定窗口**。窗口边界上最坏能放过两倍额度。选它而不是滑动窗口加权，是因为这条误差
   方向已知、可写进测试；令牌桶要额外两个参数，换来的精度在这里用不上。
3. **被限流不许产生副作用**：429 走的是"还没开始干活"那一步，登录不查库、建号不写行。
   留一行半成品再拒，等于让攻击者用被拒的请求把表撑大。
4. **内存要有顶**。每个见过的 (桶,主体) 一条时间线，不设上限就是"长跑进程 + 撞库"
   必然撑爆的那类形状（`JobManager._events` 的老账）。所以条数封顶、逐出最早的那些，
   而**逐出这件事自己在读数里说**（`evictions`），不让"我们只记得最近 5 万个"被读成
   "最近没人来过"。

限额从环境变量读（`*_RATE_LIMIT` / `*_RATE_WINDOW_S`），运维能在不发版的情况下调；
写坏的值（0、负数、非数字）按"用默认值"处理并留痕，与闸门那条 `LLM_MAX_CONCURRENCY` 同一个规矩：
一个看着能配、实际会把登录掐死的旋钮比没有更坏。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger("agentflow.accept")

# 记账条数的硬顶：一条时间线最长就是窗口内的请求数，撑不到多大，但键的个数没有边界
# （枚举攻击制造的正是"没见过这么多用户名"）。撞到顶就逐出最早登记的那些键。
MAX_KEYS = int(os.getenv("RATE_LIMIT_MAX_KEYS", "50000"))


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int((raw or "").strip())
    except (TypeError, ValueError):
        return default
    if value <= 0:
        logger.warning("限流配置 %r 不是正整数，按默认值 %s 处理", raw, default)
        return default
    return value


# 默认额度：单账号每分钟 10 次（正常人手滑两三次远用不到），单来源每分钟 60 次
# （一个 NAT 后面坐着一个 SOC 班组，60 次是"这拨人在上班"而不是"有人在扫"）。
# 建号：单个管理员每小时 20 个（批量开号是真的需求，但一秒 20 个不是）。
LOGIN_PER_USER = _positive_int(os.getenv("LOGIN_RATE_LIMIT_PER_USER", "10"), 10)
LOGIN_PER_SOURCE = _positive_int(os.getenv("LOGIN_RATE_LIMIT_PER_SOURCE", "60"), 60)
ACCOUNTS_PER_ACTOR = _positive_int(os.getenv("ACCOUNT_CREATE_RATE_LIMIT", "20"), 20)
WINDOW_S = _positive_int(os.getenv("LOGIN_RATE_WINDOW_S", "60"), 60)
ACCOUNTS_WINDOW_S = _positive_int(os.getenv("ACCOUNT_CREATE_RATE_WINDOW_S", "3600"), 3600)

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque[float]] = {}
_evictions = 0


def source_of(request: Request | None) -> str:
    """这次请求从哪儿来的。取不到就归到一个显式的"unknown"桶，**不是跳过限流**。

    反向代理之后 `request.client.host` 是代理地址，所有真实客户端会挤在同一个桶里——
    那种部署形态下这个维度要换成运维配的头部，属于形态问题，不该在这里靠猜（也不该因为
    猜不准就把这道闸关掉）。
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "unknown"


def check(name: str, subject: str, *, limit: int, window_s: int, now: float | None = None) -> dict[str, Any]:
    """记一笔并回答"这次还放不放"。放不放与拒的理由都在这一个函数里，不留第二处判断。"""
    global _evictions
    moment = time.monotonic() if now is None else now
    key = (name, subject)
    with _lock:
        hits = _hits.get(key)
        if hits is None:
            if len(_hits) >= MAX_KEYS:
                # 逐出最早登记的那批键：dict 自 3.7 起保持插入顺序，所以 `keys()` 的前若干项
                # 就是"最久没被记过的那批主体"。被逐出的键下一次访问会重新起一条时间线，
                # 表现是那个人又能试 limit 次——这是"有顶"换来的代价，写在 `evictions` 里可见。
                stale = list(_hits.keys())[: max(1, len(_hits) // 10)]
                for stale_key in stale:
                    _hits.pop(stale_key, None)
                    _evictions += 1
            # 一条时间线最多记 `limit + 1` 笔：额度用完后继续刷不需要更长的历史，
            # 而"每键存全部命中"就是那条无声增长的列表。因此 `attempts` 的语义是
            # "窗口内记到的笔数（到 limit+1 封顶）"，不是"这一分钟总共来了多少次"。
            hits = deque(maxlen=max(4, limit + 1))
            _hits[key] = hits
        while hits and moment - hits[0] >= window_s:
            hits.popleft()
        allowed = len(hits) < limit
        # 被拒的那一次也记：否则"一直刷"在计数上是 0 次，运维看到的反而是干净读数
        hits.append(moment)
        return {
            "allowed": allowed,
            "attempts": len(hits),
            "limit": limit,
            "window_s": window_s,
            # `ceil` 而不是 `int(...)+1`：后者在刚用满的那一刻会报出比窗口还长的数（61 > 60），
            # 客户端照着等就白等一分钟——读数不许说它自己都做不到的承诺。
            "retry_after": max(1, math.ceil(window_s - (moment - hits[0]))) if not allowed else 0,
        }


def guard(
    name: str,
    subject: str,
    *,
    limit: int,
    window_s: int,
    request: Request | None = None,
    reason: str = "尝试次数过多",
) -> dict[str, Any]:
    """超限就抛 429 + `Retry-After`，并留一行 warning。放行时返回那笔计数（给测试与读数用）。"""
    verdict = check(name, subject, limit=limit, window_s=window_s)
    if verdict["allowed"]:
        return verdict
    logger.warning(
        "限流拒绝：桶=%s 主体=%s 计数=%s/%s 窗口=%ss 来源=%s",
        name,
        subject,
        verdict["attempts"],
        limit,
        window_s,
        source_of(request),
    )
    raise HTTPException(
        status_code=429,
        # 文案只给"该怎么做"（稍后再来 + Retry-After），不给"额度是多少"：
        # 限额与当前计数进了响应，就等于把预算交给正在试探的人去贴着上限刷。
        # 那两个数字在上一行日志里，运维查得到，攻击者查不到。
        detail=f"{reason}：这一段时间内的次数已经用完，请 {verdict['retry_after']} 秒后再试。",
        headers={"Retry-After": str(verdict["retry_after"])},
    )


def snapshot() -> dict[str, Any]:
    """当前有几个桶在被记、最热的桶多热、逐出过多少键。

    `scope="per_process"` 是这条读数的一部分：不提它，运维就会把"每进程 10 次"读成"每平台 10 次"。
    """
    with _lock:
        hot = max(_hits.values(), key=len, default=None)
        return {
            "scope": "per_process",
            "keys": len(_hits),
            "max_keys": MAX_KEYS,
            "evictions": _evictions,
            "hottest": len(hot) if hot is not None else 0,
            "limits": {
                "login_per_user": LOGIN_PER_USER,
                "login_per_source": LOGIN_PER_SOURCE,
                "account_create_per_actor": ACCOUNTS_PER_ACTOR,
                "window_s": WINDOW_S,
                "account_window_s": ACCOUNTS_WINDOW_S,
            },
        }


def reset() -> None:
    """清账。用例之间必须各算各的——这是进程级状态，串味的症状是"上一个用例的失败把这一个挡在门外"。"""
    global _evictions
    with _lock:
        _hits.clear()
        _evictions = 0
