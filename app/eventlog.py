"""job 事件落库：让"这次运行发生过什么"不依赖任何一个进程的寿命。

为什么先做这一件再拆 worker：分析要搬到独立进程，前提就是**事件不能只活在 Web 进程的
内存 dict 里**——否则 worker 写的东西页面上读不到，而 P0 已经量到现状：
重连不认 `Last-Event-ID`、**重放 3 条**，且缓冲回收后只能回一句"已过窗口"
（`.appdata/load_before_100.json` 的 `sse` 一栏）。

三条口径：

1. **seq 由一条 INSERT 自己算**（`SELECT COALESCE(MAX(seq),0)+1`），不在应用里数。
   并发下"先读后写"必然撞车；而 `(job_id, seq)` 上有唯一约束，撞了会**报错而不是悄悄覆盖**——
   这一条是刻意的：宁可红，不要看起来绿。
2. **payload 只存事件本身**，不存正文大字段。事件流是给"进行到哪一步"看的，
   产物另有出处；把结果塞进事件会让这张表成为第二个事实源，两边迟早不一致。
3. **写库失败不许把运行带崩**：事件是观测面，运行是主路。但失败必须**留在内存缓冲里**，
   前端退化成今天的进程内流而不是"什么都没有"——降级要留痕，静默降级是撒谎。
"""

from __future__ import annotations

import json
from typing import Any

from app.db import execute, query, query_one

EVENT_COLUMNS = ("job_id", "seq", "kind", "payload")


def append_event(job_id: str, event: dict[str, Any]) -> int | None:
    """追加一条事件，返回它的 `seq`；写不进去返回 None（调用方保留内存缓冲并记一次降级）。"""
    kind = str(event.get("type") or "event")[:32]
    payload = json.dumps(event, ensure_ascii=False, default=str)
    try:
        row_id = execute(
            "INSERT INTO job_events (job_id, seq, kind, payload) "
            "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ? FROM job_events WHERE job_id = ?",
            (job_id, kind, payload, job_id),
        )
    except Exception:  # noqa: BLE001 - 观测面写失败不能打死运行，但必须被看见
        return None
    if row_id is None:
        return None
    # seq 回读而不是猜：`INSERT..SELECT` 的 lastrowid 是表 id，把那两个当同一件事
    # 就是"返回值的意思错了"——第一个用例就是这么抓到它的。
    row = query_one("SELECT seq FROM job_events WHERE id = ?", (row_id,))
    return int(row["seq"]) if row else None


def read_after(job_id: str, after_seq: int) -> list[tuple[int, dict[str, Any]]]:
    """读 `after_seq` 之后的事件，按 seq 升序。这是 `Last-Event-ID` 的实现。"""
    rows = query(
        "SELECT seq, payload FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq ASC",
        (job_id, int(after_seq or 0)),
    )
    out: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            event = json.loads(row.get("payload") or "{}")
        except json.JSONDecodeError:
            # 库里躺着读不出来的行 = 有别的写法在写这张表。当成空事件交出去就是撒谎，
            # 明确标一条损坏事件，页面上看得见，排查时也指得出来是哪一行。
            event = {"type": "event_corrupted", "seq": row.get("seq")}
        out.append((int(row["seq"]), event if isinstance(event, dict) else {"type": "event_not_object"}))
    return out


def last_seq(job_id: str) -> int:
    rows = query("SELECT COALESCE(MAX(seq), 0) AS m FROM job_events WHERE job_id = ?", (job_id,))
    return int(rows[0]["m"]) if rows else 0


def first_event_at(job_id: str) -> str | None:
    """这次运行的**第一条事件**落库时间（P6-4 用它算"排了多久"）。

    为什么不用 `jobs` 上加一列 `claimed_at`：这条表本来就按 `(job_id, seq)` 存了每一次
    可见的时刻，而"排到什么时候开始动"的第一手证据就是第一条事件——再加一列就是给同一件事
    第二个答案，两边迟早分叉（认领协议那侧已经有 `worker`/`lease_expires_at` 了）。
    返回 None 是**有意义的缺席**：这条作业一条事件都还没有（还在排队，或压根没人认领），
    调用方不许把它读成"等了 0 秒"。
    """
    row = query_one(
        "SELECT created_at FROM job_events WHERE job_id = ? ORDER BY seq ASC LIMIT 1", (job_id,)
    )
    return row.get("created_at") if row else None


def kind_counts(job_id: str) -> dict[str, int]:
    """这条作业的事件按类型数一遍（聚合在 SQL 里做，不把整段历史拉进 Python）。"""
    rows = query("SELECT kind, COUNT(*) AS n FROM job_events WHERE job_id = ? GROUP BY kind", (job_id,))
    return {str(row["kind"]): int(row["n"]) for row in rows}


def payloads_of_kind(job_id: str, kind: str) -> list[dict[str, Any]]:
    """某一类事件的 payload（P6-4 只把 `llm_gate_wait` 这一类拉出来算上游形状）。

    解不开的那一行**不静默丢**：交出一条 `event_corrupted`，与 `read_after` 同一口径——
    "库里躺着读不出来的行"说明有别的写法在写这张表。
    """
    rows = query("SELECT payload FROM job_events WHERE job_id = ? AND kind = ? ORDER BY seq ASC", (job_id, kind))
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            event = json.loads(row.get("payload") or "{}")
        except json.JSONDecodeError:
            out.append({"type": "event_corrupted", "kind": kind})
            continue
        out.append(event if isinstance(event, dict) else {"type": "event_not_object"})
    return out


def has_events(job_id: str) -> bool:
    return last_seq(job_id) > 0


__all__ = ["EVENT_COLUMNS", "append_event", "read_after", "last_seq", "has_events"]
