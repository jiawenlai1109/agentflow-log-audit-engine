"""事件落库与 SSE 续读（P2 的第二块砖：事件先脱离进程，worker 才搬得动）。

P0 量到的两条红在这里收：重连不认 `Last-Event-ID`（**重放 3 条**），以及缓冲回收后只能回
"已过窗口"（`.appdata/load_before_100.json` 的 `sse` 一栏）。判据不是"能读到事件"，
而是**读过的位置不会被忘掉**。
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import eventlog
from app import config as app_config
from app.db import execute, init_db, query_one
from app.jobs import JobManager
from app.main import app
from app.routers import jobs as jobs_route
from app.security import hash_password


@pytest.fixture(autouse=True)
def _schema():
    init_db()


def _job(owner_id: int, job_id: str = "job_evt", status: str = "running") -> None:
    execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    # 事件也清掉：seq 是从"这座库里的最大值"续的，留着上一轮的行就是把上一轮的痕迹
    # 算进本轮的断言——那是一条会被环境左右、而不是被代码左右的用例。
    execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
    execute(
        "INSERT INTO jobs (job_id, user_id, question, mode, status) VALUES (?, ?, ?, ?, ?)",
        (job_id, owner_id, "事件落库回归", "mock", status),
    )


def _user(username: str = "evt-user") -> int:
    execute("DELETE FROM users WHERE username = ?", (username,))
    return execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password("pw")),
    )


def _clean(job_id: str = "job_evt", uid: int | None = None) -> None:
    execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
    execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    if uid:
        execute("DELETE FROM users WHERE id = ?", (uid,))


# ---------------------------------------------------------------- 落库本身


def test_seq_is_monotonic_and_read_after_does_not_replay():
    """续读的语义就是"别把我读过的再给我一遍"。这条直接对着 P0 的重放读数。"""
    uid = _user()
    _job(uid)
    try:
        for index in range(4):
            seq = eventlog.append_event("job_evt", {"type": "phase", "index": index})
            assert seq == index + 1, seq
        assert eventlog.last_seq("job_evt") == 4
        cursor = 0
        first = eventlog.read_after("job_evt", cursor)
        cursor = first[-1][0]
        assert [seq for seq, _ in first] == [1, 2, 3, 4], first
        # 读到 4 之后再读：只剩新追加的，一条不重
        eventlog.append_event("job_evt", {"type": "done"})
        second = eventlog.read_after("job_evt", cursor)
        assert [seq for seq, _ in second] == [5], second
        assert second[0][1]["type"] == "done"
    finally:
        _clean(uid=uid)


def test_unique_seq_is_enforced_in_the_database():
    """并发"先读后写"迟早撞车；唯一约束让它报错而不是悄悄覆盖——所以这条要能在库上查出来。"""
    conn = sqlite3.connect(str(app_config.db_path()))
    names = {row[1] for row in conn.execute("PRAGMA index_list(job_events)")}
    conn.close()
    assert "uq_job_events_seq" in names, names


def test_two_threads_never_share_a_seq():
    """seq 由一条 INSERT 自己算。两个线程同时写 20 条，必须得到 20 个不同的序号。"""
    import threading

    uid = _user()
    _job(uid)
    try:
        def writer() -> None:
            for _ in range(10):
                eventlog.append_event("job_evt", {"type": "tick"})

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        seqs = [row[0] for row in eventlog.read_after("job_evt", 0)]
        assert len(seqs) == 20, seqs
        assert len(set(seqs)) == 20, f"序号重复（并发写撞车）：{sorted(seqs)}"
    finally:
        _clean(uid=uid)


def test_unreadable_payload_is_reported_not_swallowed(tmp_path):
    """库里躺着一行读不出来的 JSON ⇒ 必须显式标成损坏，不能当"这条不存在"。"""
    uid = _user()
    _job(uid)
    try:
        execute(
            "INSERT INTO job_events (job_id, seq, kind, payload) VALUES (?, ?, ?, ?)",
            ("job_evt", 1, "phase", "{不是 JSON"),
        )
        event = eventlog.read_after("job_evt", 0)[0][1]
        assert event["type"] == "event_corrupted", event
    finally:
        _clean(uid=uid)


def test_manager_sink_lands_events_in_the_db():
    """`JobManager` 的 sink 是真接上的：publish 一次，库里就多一行。"""
    uid = _user()
    _job(uid)
    manager = JobManager(sink=eventlog.append_event)
    try:
        manager.publish("job_evt", {"type": "phase", "phase": "plan"})
        rows = eventlog.read_after("job_evt", 0)
        assert [event["phase"] for _seq, event in rows] == ["plan"], rows
        assert manager.sink_failures == 0
    finally:
        _clean(uid=uid)


def test_sink_failure_does_not_kill_the_run():
    """落库失败是观测面降级，不是运行失败——但必须记账，不许静默。"""
    def broken(_job_id: str, _event: dict) -> int:
        raise sqlite3.OperationalError("database is locked")

    manager = JobManager(sink=broken)
    manager.publish("job_x", {"type": "phase"})
    assert manager.sink_failures == 1, manager.sink_failures
    assert manager.snapshot("job_x", 0)[0], "落库失败把内存缓冲也带走了 ⇒ 前端彻底看不见过程"


# ---------------------------------------------------------------- SSE 的续读语义


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = "Bearer " + response.json()["token"]


def _frames(body: str) -> list[dict]:
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_sse_replays_nothing_after_the_clients_last_id():
    """带 `Last-Event-ID: 2` 重连 ⇒ 只该拿到 3/4 两帧，外加终止帧。"""
    uid = _user()
    _job(uid)
    for index in range(1, 5):
        eventlog.append_event("job_evt", {"type": "phase", "index": index})
    execute("UPDATE jobs SET status = 'success' WHERE job_id = 'job_evt'", ())
    client = TestClient(app)
    try:
        _login(client, "evt-user")
        fresh = client.get("/api/jobs/job_evt/events")
        assert fresh.status_code == 200, fresh.text
        assert [e.get("index") for e in _frames(fresh.text) if e.get("index")] == [1, 2, 3, 4]
        resumed = client.get("/api/jobs/job_evt/events", headers={"Last-Event-ID": "2"})
        indexes = [e.get("index") for e in _frames(resumed.text) if e.get("index")]
        assert indexes == [3, 4], indexes
        assert "id: 3" in resumed.text and "id: 1" not in resumed.text, resumed.text[:400]
        assert _frames(resumed.text)[-1]["type"] == "job_status"
    finally:
        client.close()
        _clean(uid=uid)


def _read_stream(client: TestClient, job_id: str, within_s: float = 10.0) -> str:
    """在限定时间内把进度流读完，读不完就**报红**。

    为什么不直接 `client.get(...).text`：那种写法下"流永不结束"这种缺陷的表现是整片测试挂住，
    而挂住不算抓到（P4 那次一条位点把测试卡了 600 秒，最后只能靠超时才知道它"被抓了"）。
    红要落在断言上：这里落的是"10 秒内它自己结束了没有"。
    """
    holder: dict[str, str] = {}

    def _reader() -> None:
        try:
            holder["body"] = client.get(f"/api/jobs/{job_id}/events").text
        except Exception as exc:  # noqa: BLE001 - 带回主线程再判，不在子线程里静默
            holder["error"] = f"{type(exc).__name__}: {exc}"[:200]

    reader = threading.Thread(target=_reader, name=f"stream-{job_id}", daemon=True)
    reader.start()
    reader.join(timeout=within_s)
    assert not reader.is_alive(), (
        f"{within_s}s 内这条流没自己结束——那正是「降级那条分支从不查库、也从不退出」的形状"
    )
    assert "error" not in holder, holder["error"]
    return holder.get("body", "")


def test_a_stream_that_never_got_events_still_ends_when_the_job_is_terminal():
    """一条"事件从没落库"的流也必须自己结束——那条降级分支以前**从不查库，也从不退出**。

    复现路径是摆好的而不是竞态：作业行直接建成 `running`、库里一行事件都没有，然后把它改成
    终态再读流。改造前这条流每 0.5s 空转一次、永不结束（`continue` 那一支绕过了状态查询）。
    P5-3 那条 SSE 用例是靠竞态撞上它的——那种用例本身不稳，所以这里给一条前置条件确定的；
    而这条缺陷在 P5-2 之后变贵了：前端现在会自动重连，一条永不结束的流就是一条无限重连。
    """
    uid = _user()
    _job(uid, status="running")
    execute("UPDATE jobs SET status = 'success' WHERE job_id = 'job_evt'", ())
    client = TestClient(app)
    try:
        _login(client, "evt-user")
        body = _read_stream(client, "job_evt")
        frames = _frames(body)
        assert [frame["type"] for frame in frames[:2]] == ["queue", "events_not_persisted"], frames[:3]
        assert frames[-1] == {"type": "job_status", "status": "success"}, frames[-3:]
    finally:
        client.close()
        _clean(uid=uid)


def test_the_degraded_buffer_does_not_replay_the_same_events():
    """降级那条路也要**只发一次**：缓冲游标每次传 0，就是每轮把同一段过程重发一遍。

    把 sink 摘掉是让"库里没有事件、缓冲里却有"这个状态**摆出来**而不是等竞态；作业先留非终态，
    1.2s 之后由计时器落终态 ⇒ 这条流至少走两轮降级，重放与否才看得见。
    """
    uid = _user()
    _job(uid, status="running")
    client = TestClient(app)
    original_sink = jobs_route.manager.sink
    jobs_route.manager.sink = None
    try:
        for index in range(3):
            jobs_route.manager.publish("job_evt", {"type": "phase", "index": index})
        _login(client, "evt-user")
        flipper = threading.Timer(
            1.2, lambda: execute("UPDATE jobs SET status = 'success' WHERE job_id = 'job_evt'", ())
        )
        flipper.start()
        try:
            frames = _frames(_read_stream(client, "job_evt", within_s=15.0))
        finally:
            flipper.cancel()
        indices = [frame.get("index") for frame in frames if frame.get("type") == "phase"]
        assert indices == [0, 1, 2], f"过程事件被重发了：{indices}"
    finally:
        jobs_route.manager.sink = original_sink
        with jobs_route.manager._lock:
            jobs_route.manager._events.pop("job_evt", None)
        client.close()
        _clean(uid=uid)


def test_sse_frame_ids_come_from_the_store_not_from_position():
    """`id:` 必须是库里的 seq。用位置当 id，一次重排就把用户送回过去或抛到未来。"""
    uid = _user()
    _job(uid)
    eventlog.append_event("job_evt", {"type": "phase", "index": 1})
    execute("UPDATE jobs SET status = 'failed' WHERE job_id = 'job_evt'", ())
    client = TestClient(app)
    try:
        _login(client, "evt-user")
        body = client.get("/api/jobs/job_evt/events").text
        assert "id: 1\n" in body, body[:300]
        assert query_one("SELECT seq FROM job_events WHERE job_id = 'job_evt'")["seq"] == 1
    finally:
        client.close()
        _clean(uid=uid)
