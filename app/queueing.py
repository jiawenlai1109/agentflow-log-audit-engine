"""队列的真相放在库里：`queued → running(带 worker 与租约) → 终态`。

为什么不是"先上 Redis Stream"：P0 量到的两个问题——100 个 job 里约 91 个在排队、
杀掉进程后 28 个 job **永远停在非终态**——都不是"消息传得不够快"，而是
"没有一个人能回答这个 job 归谁、它死了没有"。所以真相必须在库里；Redis 至多当
"有活了"的提醒（`QUEUE_NOTIFY=redis` 时用它，没 Redis 也不影响正确性）。

三条不能弯的口径：

1. **认领必须原子**。先 `SELECT` 再 `UPDATE` 不算认领——两个 worker 会同时读到同一行。
   真正的判据是那条 `UPDATE ... WHERE status='queued' AND worker IS NULL` 的**影响行数**：
   改动 1 行的人才算抢到，改动 0 行的必须退开。这一条有用例（两个认领者抢一个 job）。
2. **租约会过期，过期要能收回，但不能无限收回**。崩溃的 worker 留下的 `running` 行
   退回队列，`attempts` +1；超过上限就判 `failed` 并写清原因。没有上限的话，
   一个稳定崩溃的 job 会把队列变成永动机。
3. **降级要说人话**。`degraded_reason`/`error` 里写"上一个 worker 丢了这活"，
   而不是留一个永远不动的 `running`——那是把"没人负责"伪装成"还在跑"。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.db import execute, query, query_one

TERMINAL = ("success", "partial", "degraded", "failed", "error", "cancelled")
MAX_ATTEMPTS = int(os.getenv("JOB_MAX_ATTEMPTS", "3"))
LEASE_SECONDS = int(os.getenv("JOB_LEASE_SECONDS", "90"))
# 队列的认领必须跨用户（共享队列的全部意义），所以这一族的 SQL 不带 user_id 谓词。
# 那不是把归属关掉，而是把它挪到两个真正管得住的地方：
#   ① 读数据源时用行里的 user_id 重校验（app/runner.resolve_sources）；
#   ② 面向用户的每一条读路径仍带谓词（_owned_job / SSE / reports）。
# 标记一律**写在 SQL 字面量里**，不当常量拼在前面：tests/test_auth.py 的结构守卫读的是
# 语句本身，`_MARK + "SELECT ..."` 那种拼法它看不见，于是"豁免"变成了"这条用例没管"。
# 写进字面量还有个副作用是想要的：出现在 sqlite 日志里时，"这条 SQL 跨用户"是明着的。


def worker_id() -> str:
    """`主机名:进程号:随机尾`——同一台机器起多个 worker 也不会撞名字。"""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _now() -> datetime:
    return datetime.now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(sep=" ", timespec="seconds")


def set_spec(job_id: str, spec: dict[str, Any]) -> None:
    """把"这个 job 到底要干什么"写进行里。

    没有这一列，"从库里捞一个 job 出来跑"根本做不到——参数只活在提交它的那个进程的
    闭包里，那正是 P0 里 28 个 job 卡死的原因。
    """
    execute(
        "/*queue-internal*/ UPDATE jobs SET spec = ? WHERE job_id = ?",
        (json.dumps(spec, ensure_ascii=False, default=str), job_id),
    )


def get_spec(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("spec") or "{}")
    except json.JSONDecodeError:
        return {}


def accept(
    *,
    job_id: str,
    user_id: int,
    question: str,
    mode: str,
    session_id: str | None,
    pack: str | None,
    spec: dict[str, Any],
) -> None:
    """受理 = **一条 INSERT 就写完可认领状态**。

    原来分两步（先插 `pending`，再 UPDATE 成 `queued` 并写 spec），中间那个窗口里的行
    又没人能认领（claim 只认 `queued`）、又被深度算进"排队"，于是崩溃或老数据留下的
    `pending` 会永远显示成"有人在等"。一步写完，窗口就不存在了。
    """
    execute(
        "INSERT INTO jobs (job_id, user_id, question, mode, session_id, pack, status, spec) "
        "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
        (job_id, user_id, question, mode, session_id, pack, json.dumps(spec, ensure_ascii=False, default=str)),
    )
    notify()


def enqueue(job_id: str, spec: dict[str, Any]) -> None:
    """把一个**已存在**的行退回队列：清掉上一次的认领者与租约。"""
    execute(
        "/*queue-internal*/ UPDATE jobs SET status='queued', spec=?, worker=NULL, lease_expires_at=NULL "
        "WHERE job_id=?",
        (json.dumps(spec, ensure_ascii=False, default=str), job_id),
    )
    notify()


def claim(worker: str, lease_s: int = LEASE_SECONDS) -> dict[str, Any] | None:
    """认领下一个 job。抢到返回行，没抢到返回 None（包括"看起来有但被人抢先"）。"""
    candidate = query_one(
        "/*queue-internal*/ SELECT job_id, user_id, spec, attempts FROM jobs "
        "WHERE status='queued' ORDER BY id ASC LIMIT 1"
    )
    if not candidate:
        return None
    lease_until = _iso(_now() + timedelta(seconds=lease_s))
    changed = execute(
        "/*queue-internal*/ UPDATE jobs SET status='running', worker=?, lease_expires_at=?, progress=1 "
        "WHERE job_id=? AND status='queued' AND worker IS NULL",
        (worker, lease_until, candidate["job_id"]),
    )
    # `changed` 是 lastrowid，不是行数——认领必须用"改动了几行"判，所以这里回读确认。
    owner = query_one(
        "/*queue-internal*/ SELECT worker, status FROM jobs WHERE job_id = ?", (candidate["job_id"],)
    )
    if not owner or owner.get("worker") != worker or owner.get("status") != "running":
        return None
    return {**candidate, "worker": worker, "lease_expires_at": lease_until}


def heartbeat(job_id: str, worker: str, lease_s: int = LEASE_SECONDS) -> bool:
    """续租。返回 False = 租约已被别人收走，这次运行该停。"""
    execute(
        "/*queue-internal*/ UPDATE jobs SET lease_expires_at=? WHERE job_id=? AND worker=?",
        (_iso(_now() + timedelta(seconds=lease_s)), job_id, worker),
    )
    row = query_one("/*queue-internal*/ SELECT worker FROM jobs WHERE job_id = ?", (job_id,))
    return bool(row) and row.get("worker") == worker


def finish(job_id: str, worker: str, status: str, run_id: str | None = None, error: str | None = None) -> None:
    execute(
        "/*queue-internal*/ UPDATE jobs SET status=?, run_id=?, error=?, finished_at=?, progress=100, "
        "worker=NULL, lease_expires_at=NULL WHERE job_id=? AND worker=?",
        (status, run_id, error, _now().isoformat(sep=" ", timespec="seconds"), job_id, worker),
    )


def reclaim_expired(limit: int = 20) -> list[str]:
    """崩溃恢复：租约过期的 running 退回队列；到次数上限就判失败并说清原因。"""
    stamp = _iso(_now())
    rows = query(
        "/*queue-internal*/ SELECT job_id, attempts FROM jobs WHERE status='running' "
        "AND lease_expires_at IS NOT NULL AND lease_expires_at < ? ORDER BY id ASC LIMIT ?",
        (stamp, limit),
    )
    recovered: list[str] = []
    for row in rows:
        attempts = int(row.get("attempts") or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            execute(
                "/*queue-internal*/ UPDATE jobs SET status='failed', attempts=?, worker=NULL, "
                "lease_expires_at=NULL, finished_at=?, error=? WHERE job_id=? AND status='running'",
                (
                    attempts,
                    stamp,
                    f"连续 {attempts} 次被 worker 领取后没有跑完（上一个 worker 丢了这活），不再重试",
                    row["job_id"],
                ),
            )
            continue
        execute(
            "/*queue-internal*/ UPDATE jobs SET status='queued', attempts=?, worker=NULL, "
            "lease_expires_at=NULL WHERE job_id=? AND status='running'",
            (attempts, row["job_id"]),
        )
        recovered.append(str(row["job_id"]))
    if recovered:
        notify()
    return recovered


def stats() -> dict[str, int]:
    """队列深度从库里读，不读本进程的簿记——多进程部署下后者只看得见自己那几个。

    `pending` **不折进 `queued`**：claim 只认 `queued`，把没人能认领的行报成"在排队"
    等于给用户一个永远不动的数字（P0 那座开发机上就有 57 条这种行）。分开印，运维才看得见。
    """
    rows = query("/*queue-internal*/ SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    counts = {str(row["status"]): int(row["n"]) for row in rows}
    return {
        "workers": default_worker_concurrency(),
        "running": counts.get("running", 0),
        "queued": counts.get("queued", 0),
        "stale_pending": counts.get("pending", 0),
    }


def default_worker_concurrency() -> int:
    """默认 2 是量出来的，不是保守：单进程里调到 8 会把登录 p95 从 8977.9ms 推到
    14378.3ms（对照见 `app/jobs.py` 与本目录 `.appdata/load_*.json`）。
    **分进程部署之后**这个数才是每个 worker 各自的数，那时往上加才不抢受理层的 CPU。
    """
    return max(1, int(os.getenv("WORKER_CONCURRENCY", "2")))


# ------------------------------------------------------------------ 唤醒通道（可选）

_condition = threading.Condition()
_redis = None


def _redis_channel():
    """Redis 只当"有活了"的门铃：收不到顶多多等一轮轮询，不影响正确性。

    没配 `REDIS_URL` 就完全不用它——测试与 CI 不该为了跑通队列而要求一台 Redis。
    """
    global _redis
    if _redis is not None:
        return _redis
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        _redis = False
        return None
    try:
        import redis

        client = redis.Redis.from_url(url)
        client.ping()
        _redis = client
    except Exception:  # noqa: BLE001 - 门铃连不上就退回轮询，但要说一声
        _redis = False
    return _redis if _redis is not False else None


def notify() -> None:
    with _condition:
        _condition.notify_all()
    client = _redis_channel()
    if client is not None:
        try:
            client.publish("agentflow:jobs", "1")
        except Exception:  # noqa: BLE001 - 同上：门铃失败不等于没活
            pass


def wait_for_work(timeout_s: float) -> None:
    with _condition:
        _condition.wait(timeout=timeout_s)


def note_progress(job_id: str, percent: int) -> None:
    """进度写库。放这儿而不是 runner.py，是为了让"跨用户碰 jobs 的 SQL"只住在一个文件里
    ——归属守卫要封的是条数，分散在两个文件就没法封。
    """
    execute(
        "/*queue-internal*/ UPDATE jobs SET progress=? WHERE job_id=? AND status='running'",
        (percent, job_id),
    )
