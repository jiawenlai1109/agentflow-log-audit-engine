"""事件落库与 SSE 续读（P2 的第二块砖：事件先脱离进程，worker 才搬得动）。

P0 量到的两条红在这里收：重连不认 `Last-Event-ID`（**重放 3 条**），以及缓冲回收后只能回
"已过窗口"（`.appdata/load_before_100.json` 的 `sse` 一栏）。判据不是"能读到事件"，
而是**读过的位置不会被忘掉**。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import eventlog
from app import config as app_config
from app.db import execute, init_db, query_one
from app.jobs import JobManager
from app.main import app
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
