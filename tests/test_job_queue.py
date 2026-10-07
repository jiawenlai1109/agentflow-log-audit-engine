"""P2 的第一步：队列从"写死 2 个线程"变成"可配、可见、缓冲有界"。

P0 的读数说得很直白（`.appdata/load_before_100.json`）：100 个 job 进来，峰值 **93 个非终态**，
其中约 91 个在排队——`JobManager(max_workers=2)` 是写死的。而一次分析的绝大部分时间是
**等 LLM 上游**，不是算。所以先把这三件事钉住：

1. worker 数由 `WORKER_CONCURRENCY` 决定，并且**路由真的用了它**（"参数存在"≠"接在线路上"，
   这条立场来自 #13 与 review 下限那次的教训）；
2. 队列深度查得到（`depth()`），并顺着 `JobOut.queue` 与 SSE 第一帧回到用户眼前；
3. 事件缓冲有界：每 job 有条数上限、已完成的 job 会被回收——但回收之后 `is_done` 仍要说真话，
   且重连要显式报"事件已过窗口"，不许静默返回空。

不解决的问题也写在这里：**这一步不解决登录 CPU**（一次 PBKDF2 实测 104.8ms 是另一条瓶颈，
降它是安全口径变更），也**不代表上游扛得住** 8×3=24 路并发——那由 P4 的闸门与配额管。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.jobs import JobManager, default_worker_concurrency


def _wait_until(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- ① worker 数可配


def test_worker_concurrency_defaults_above_two(monkeypatch):
    """默认值本身就是一个决定：2 是单机 demo 的形状，不是"多数时间在等上游"的形状。"""
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    # 默认 2 不是"还没优化"，是对照量出来的：单进程里调到 8 会把 p95 登录从 8977.9ms 推到
    # 14378.3ms（见 app/jobs.py 的表）。要改这个数，先改部署形态（API 与 worker 分进程）。
    assert default_worker_concurrency() == 2


def test_worker_concurrency_env_and_floor(monkeypatch):
    monkeypatch.setenv("WORKER_CONCURRENCY", "16")
    assert default_worker_concurrency() == 16
    # 配 0 或负数不能把执行器变成"永不执行"——那比慢得多严重
    monkeypatch.setenv("WORKER_CONCURRENCY", "0")
    assert default_worker_concurrency() == 1


def test_router_uses_the_knob_not_a_hardcoded_two():
    """接线判据：路由里那个 manager 的 worker 数必须来自同一个口径。

    只测 `JobManager` 不够——它默认值再大，`app/routers/jobs.py` 里再写死一次 `max_workers=2`
    就一切照旧。这条断言就是钉住"别在第二个地方重新写死"。
    """
    from app.routers import jobs as jobs_router

    assert jobs_router.manager.max_workers == default_worker_concurrency()


# ---------------------------------------------------------------- ② 队列可见


def test_depth_reports_running_and_queued_separately():
    """2 个 worker + 3 个任务 ⇒ running=2、queued=1。

    （第一版这里用 `Barrier(3)` 当"卡住 worker"的工具，结果主线程自己成了第三个 party，
    屏障提前触发、第三个任务复位后又等满超时——**用同步原语当断言**是我不该再犯的错，
    现在换成显式计数 + 一个事件，等待上限也放宽到 10s，让它慢但不脆。）
    """
    manager = JobManager(max_workers=2, keep_done=50)
    release = threading.Event()
    inside = {"n": 0}
    guard = threading.Lock()

    def held() -> None:
        with guard:
            inside["n"] += 1
        release.wait(timeout=10)

    for index in range(3):
        manager.submit(f"job_{index}", held)
    assert _wait_until(lambda: inside["n"] == 2, timeout_s=8.0), inside
    depth = manager.depth()
    assert depth["workers"] == 2 and depth["running"] == 2 and depth["queued"] == 1, depth

    release.set()
    assert _wait_until(
        lambda: manager.depth()["running"] == 0 and manager.depth()["queued"] == 0, timeout_s=8.0
    ), manager.depth()
    assert inside["n"] == 3, "排队的那个始终没被执行"


def test_queue_depth_reaches_the_api_shape():
    """`JobOut.queue` 不是装饰字段：`_owned_job` 每次都要带上深度，否则前端拿不到位置。"""
    from app.db import execute, query_one
    from app.routers.jobs import _owned_job, manager
    from app.security import hash_password

    uid = execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        ("queue-shape-user", hash_password("pw")),
    )
    execute(
        "INSERT INTO jobs (job_id, user_id, question, mode, status) VALUES (?, ?, ?, ?, 'pending')",
        ("job_queue_shape", uid, "测试队列可见性", "mock"),
    )
    row = _owned_job("job_queue_shape", {"id": uid})
    assert row["queue"] == manager.depth(), row
    assert {"workers", "running", "queued"} <= set(row["queue"]), row["queue"]
    # 别人的 job 一律 404：加了 queue 字段不能顺手把归属过滤放宽
    with pytest.raises(Exception) as raised:
        _owned_job("job_queue_shape", {"id": 999_999})
    assert getattr(raised.value, "status_code", None) == 404
    execute("DELETE FROM jobs WHERE job_id = ?", ("job_queue_shape",))
    execute("DELETE FROM users WHERE id = ?", (uid,))
    assert query_one("SELECT * FROM jobs WHERE job_id = 'job_queue_shape'") is None


# ---------------------------------------------------------------- ③ 事件缓冲有界


def test_event_buffer_is_capped_and_says_so():
    manager = JobManager(max_workers=1, keep_done=10, events_per_job=2)
    for index in range(5):
        manager.publish("job_cap", {"type": "phase", "index": index})
    events, total, expired = manager.snapshot("job_cap", 0)
    assert total == 3, events  # 2 条真实事件 + 1 条"我截断了"
    assert events[-1]["type"] == "events_truncated", events
    assert not expired


def test_finished_events_are_evicted_without_lying_about_completion():
    """回收的是缓冲，不是事实：job 仍然算跑完了，但重连必须被告知"已过窗口"。"""
    manager = JobManager(max_workers=1, keep_done=1, events_per_job=50)
    for index in range(4):
        manager.submit(f"job_{index}", lambda: None)
    assert _wait_until(lambda: all(manager.is_done(f"job_{i}") for i in range(4)))

    events, total, expired = manager.snapshot("job_0", 0)
    assert expired is True and events == [], (events, total, expired)
    assert manager.is_done("job_0") is True, "缓冲回收把完成态谎报成未知态"
    # 最近 keep_done 个仍在窗口内
    assert manager.snapshot("job_3", 0)[2] is False


def test_eviction_never_touches_running_or_queued_jobs():
    manager = JobManager(max_workers=1, keep_done=1)
    gate = threading.Event()
    manager.submit("job_slow", gate.wait)
    for index in range(4):
        manager.submit(f"job_done_{index}", lambda: None)
    assert not manager.is_done("job_slow")
    assert manager.snapshot("job_slow", 0)[2] is False, "在跑的 job 不许被淘汰"
    gate.set()
    assert _wait_until(lambda: manager.is_done("job_slow"))
