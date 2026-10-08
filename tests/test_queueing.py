"""队列的不变量：认领必须原子、租约必须能收回、收回必须有上限。

这四条都是 P0 那条读数（杀掉进程后 **28 个 job 永远停在非终态**）的正面对治。
判据刻意不测"跑得快不快"，只测"谁说谎"：`running` 不代表有人在跑，除非它带着一个
还活着的 worker 与没过期的租约。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app import queueing
from app import config as app_config
from app.db import execute, init_db, query_one
from app.runner import resolve_sources
from app.security import hash_password


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _user(username: str) -> int:
    execute("DELETE FROM users WHERE username = ?", (username,))
    return execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password("pw")),
    )


def _job(job_id: str, user_id: int, status: str = "pending", **columns: object) -> None:
    execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    cols = {"status": status, **columns}
    names = ", ".join(["job_id", "user_id", "question", "mode", *cols])
    marks = ", ".join("?" * (4 + len(cols)))
    execute(
        f"INSERT INTO jobs ({names}) VALUES ({marks})",
        (job_id, user_id, "队列不变量回归", "mock", *cols.values()),
    )


def _clean(job_id: str, *user_ids: int) -> None:
    execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
    execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    for uid in user_ids:
        execute("DELETE FROM users WHERE id = ?", (uid,))


# ---------------------------------------------------------------- ① 认领必须原子


def test_two_workers_cannot_claim_the_same_job():
    uid = _user("claim-a-user")
    _job("job_claim", uid, "queued", spec=json.dumps({"question": "x", "source_ref": ""}))
    try:
        first = queueing.claim("worker-alpha")
        assert first and first["job_id"] == "job_claim", first
        second = queueing.claim("worker-beta")
        assert second is None, "第二个认领者抢到了同一个 job ⇒ 一次分析会被跑两遍"
        row = query_one("SELECT worker, status FROM jobs WHERE job_id = 'job_claim'")
        assert row["worker"] == "worker-alpha" and row["status"] == "running", row
    finally:
        execute("UPDATE jobs SET worker=NULL WHERE job_id='job_claim'")
        _clean("job_claim", uid)


def test_claim_is_empty_when_nothing_is_queued():
    assert queueing.claim("worker-lonely") is None


# ---------------------------------------------------------------- ② 租约与崩溃恢复


def test_expired_lease_returns_the_job_to_the_queue():
    uid = _user("lease-a-user")
    _job(
        "job_lease",
        uid,
        "running",
        worker="worker-dead",
        lease_expires_at=(datetime.now() - timedelta(seconds=30)).isoformat(sep=" ", timespec="seconds"),
    )
    try:
        assert queueing.reclaim_expired() == ["job_lease"]
        row = query_one("SELECT status, attempts, worker FROM jobs WHERE job_id = 'job_lease'")
        assert row["status"] == "queued" and row["attempts"] == 1, row
        assert row["worker"] is None, "收回后还留着死掉 worker 的名字"
    finally:
        _clean("job_lease", uid)


def test_lease_within_the_window_is_not_reclaimed():
    uid = _user("lease-b-user")
    _job(
        "job_live",
        uid,
        "running",
        worker="worker-alive",
        lease_expires_at=(datetime.now() + timedelta(minutes=5)).isoformat(sep=" ", timespec="seconds"),
    )
    try:
        assert queueing.reclaim_expired() == [], "还在租约期内就被人收走 ⇒ 两个 worker 会同时跑一个 job"
        assert query_one("SELECT worker FROM jobs WHERE job_id = 'job_live'")["worker"] == "worker-alive"
    finally:
        _clean("job_live", uid)


def test_crash_loop_is_bounded_and_then_says_why():
    """一个稳定崩溃的 job 不能把队列变成永动机：到次数上限就判失败并写清原因。"""
    uid = _user("loop-a-user")
    stale = (datetime.now() - timedelta(seconds=5)).isoformat(sep=" ", timespec="seconds")
    _job("job_loop", uid, "running", worker="worker-crash", lease_expires_at=stale, attempts=queueing.MAX_ATTEMPTS - 1)
    try:
        assert queueing.reclaim_expired() == []
        row = query_one("SELECT status, error FROM jobs WHERE job_id = 'job_loop'")
        assert row["status"] == "failed", row
        assert "丢了这活" in (row["error"] or ""), row
    finally:
        _clean("job_loop", uid)


def test_finish_releases_the_claim():
    uid = _user("fin-a-user")
    _job("job_fin", uid, "queued", spec="{}")
    try:
        claim = queueing.claim("worker-fin")
        assert claim
        queueing.finish("job_fin", "worker-fin", "success", run_id="run_x")
        row = query_one("SELECT status, worker, lease_expires_at, run_id FROM jobs WHERE job_id='job_fin'")
        assert row["status"] == "success" and row["worker"] is None, row
        assert row["lease_expires_at"] is None and row["run_id"] == "run_x", row
    finally:
        _clean("job_fin", uid)


def test_finish_from_the_wrong_worker_changes_nothing():
    """`WHERE worker=?` 是认领协议的一部分：过期的旧 worker 不能把新认领者的结果盖掉。"""
    uid = _user("fin-b-user")
    _job("job_mine", uid, "running", worker="worker-new", spec="{}")
    try:
        queueing.finish("job_mine", "worker-stale", "failed", error="不该生效")
        row = query_one("SELECT status, worker FROM jobs WHERE job_id='job_mine'")
        assert row["status"] == "running" and row["worker"] == "worker-new", row
    finally:
        _clean("job_mine", uid)


# ---------------------------------------------------------------- ②′ 并发认领与续租


def test_concurrent_claimers_take_a_job_exactly_once(monkeypatch):
    """认领必须原子：这条判据用的是**真并发**，不是顺序调用两次。

    第一版（`test_two_workers_cannot_claim_the_same_job`）是顺序 claim 两次，第二次拿到 None
    就判通过。把认领的 UPDATE 谓词摘掉之后它**照样全绿**——顺序执行时第二个认领者的候选
    SELECT 已经看不见 `queued` 行了，"两个人同时读到同一行"这条路径根本没被走到。
    这就是"红在断言上 ≠ 红在脚手架上"的另一面：**走到判据的路径没走到，绿就不算数**。
    所以这里让几个认领者在同一时刻进 claim()，同一个 job 只允许出现一个非 None。
    """
    import threading

    uid = _user("race-user")
    winners: list[list[str]] = []
    overlaps: list[int] = []
    real_query_one = queueing.query_one
    try:
        for round_index in range(5):
            job_id = f"job_race_{round_index}"
            # 每一轮先清干净：claim 取候选是按 id 排序的，上一轮留下的 queued 行会被这一轮的
            # 认领者先抢走（第一版就是这么红的——它红在脚手架上，不是红在判据上）。
            execute("DELETE FROM jobs WHERE job_id LIKE 'job_race_%'")
            _job(job_id, uid, "queued", spec=json.dumps({"question": "x", "source_ref": ""}))
            barrier = threading.Barrier(4)
            taken: list[tuple[str, str]] = []
            reads: list[str] = []
            lock = threading.Lock()

            def counted(sql, params=()):
                row = real_query_one(sql, params)
                if "status='queued'" in sql and row:  # 只数"候选行被读到"那一次
                    with lock:
                        reads.append(str(row["job_id"]))
                return row

            monkeypatch.setattr(queueing, "query_one", counted)

            def racer(name: str) -> None:
                barrier.wait()  # 同一时刻放开，候选 SELECT 才可能读到同一行
                claim = queueing.claim(name)
                if claim is not None:
                    with lock:
                        taken.append((name, str(claim["job_id"])))

            threads = [threading.Thread(target=racer, args=(f"w{round_index}-{i}",)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            monkeypatch.setattr(queueing, "query_one", real_query_one)
            winners.append([name for name, _ in taken])
            overlaps.append(len(reads))
            row = query_one("SELECT worker FROM jobs WHERE job_id = ?", (job_id,))
            assert len(taken) <= 1, f"第 {round_index} 轮有 {len(taken)} 个认领者拿到同一个 job：{taken}"
            assert all(claimed == job_id for _, claimed in taken), taken
            assert row["worker"] in [name for name, _ in taken] or taken == [], (row["worker"], taken)
        assert any(w for w in winners), "五轮里一次都没抢到 ⇒ 这条判据没在测认领，只是在测超时"
        # 这条是"判据自己证明自己走到了那条路"：四个认领者里至少有两个在同一条 queued 行上
        # 撞过面，否则"不许双拿"根本没被检验过，绿也只说明线程没跑起来。
        assert max(overlaps) >= 2, f"五轮没有一轮真的撞在一起（每轮读到候选的次数：{overlaps}）"
    finally:
        monkeypatch.setattr(queueing, "query_one", real_query_one)
        execute("DELETE FROM jobs WHERE job_id LIKE 'job_race_%'")
        _clean("unused", uid)


def test_heartbeat_from_a_lost_worker_does_not_extend_the_lease():
    """续租也只认"现在这个认领者"，而且**不许把别人的租约推到未来**。

    只看返回值是不够的：`heartbeat()` 是"改一行 + 回读确认"两步，把 UPDATE 的 worker 谓词
    摘掉之后回读仍然能报 False，而租约已经被旧 worker 偷偷续上了——那会让一个已经死透的
    认领者永远不被收回。判据落在库里的列上，不落在函数说了什么上。
    """
    uid = _user("hb-user")
    _job("job_hb", uid, "running", worker="worker-new")
    try:
        assert queueing.heartbeat("job_hb", "worker-new") is True
        held = query_one("SELECT lease_expires_at FROM jobs WHERE job_id='job_hb'")["lease_expires_at"]
        stale = queueing.heartbeat("job_hb", "worker-stale")
        row = query_one("SELECT worker, lease_expires_at FROM jobs WHERE job_id='job_hb'")
        assert stale is False, "旧认领者的续租被当成了有效"
        assert row["worker"] == "worker-new", row
        assert row["lease_expires_at"] == held, f"租约被不是认领者的进程续上了：{held} → {row['lease_expires_at']}"
    finally:
        _clean("job_hb", uid)


# ---------------------------------------------------------------- ③ 深度从库里读


def test_stats_count_from_the_database_not_the_process():
    uid = _user("stats-user")
    _job("job_s1", uid, "queued")
    _job("job_s2", uid, "running", worker="worker-x")
    try:
        stats = queueing.stats()
        assert stats["queued"] >= 1 and stats["running"] >= 1, stats
        assert stats["workers"] == queueing.default_worker_concurrency(), stats
    finally:
        for job_id in ("job_s1", "job_s2"):
            execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        _clean("unused", uid)


# ---------------------------------------------------------------- ④ spec 只存引用


def test_sources_resolve_rejects_other_users_datasets():
    """换成分布式执行之后，归属校验必须在**读取时**重做一遍，不能信提交时已经查过。"""
    owner = _user("ds-owner")
    other = _user("ds-other")
    execute("DELETE FROM datasets WHERE filename = 'queue_ref.csv'")
    ds_id = execute(
        "INSERT INTO datasets (user_id, filename, path, columns) VALUES (?, ?, ?, ?)",
        (owner, "queue_ref.csv", "/somewhere/queue_ref.csv", "[]"),
    )
    try:
        assert resolve_sources({"source_ref": f"dataset:{ds_id}"}, owner) == "/somewhere/queue_ref.csv"
        with pytest.raises((RuntimeError, Exception)) as raised:
            resolve_sources({"source_ref": f"dataset:{ds_id}"}, other)
        assert raised.value.__class__.__name__ in {"RuntimeError", "HTTPException"}, raised.value
        with pytest.raises(RuntimeError):
            resolve_sources({"source_ref": "oss://bucket/nope"}, owner)
    finally:
        execute("DELETE FROM datasets WHERE id = ?", (ds_id,))
        _clean("none", owner, other)


def test_enqueue_writes_the_spec_and_clears_the_previous_claim():
    uid = _user("spec-user")
    _job("job_spec", uid, "failed", worker="worker-old", lease_expires_at="2026-01-01 00:00:00")
    try:
        queueing.enqueue("job_spec", {"question": "再来一次", "source_ref": ""})
        row = query_one("SELECT status, worker, spec FROM jobs WHERE job_id='job_spec'")
        assert row["status"] == "queued" and row["worker"] is None, row
        assert json.loads(row["spec"])["question"] == "再来一次", row
    finally:
        _clean("job_spec", uid)


def test_worker_column_exists_on_the_legacy_path():
    """旧库靠 _ADDED_COLUMNS 补列。补漏了不会报错在建表时，会在**第一次认领**时——
    所以这条直接查库，确认运行时真的有这三列（模型里写了不等于库里已存在）。"""
    conn = sqlite3.connect(str(app_config.db_path()))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()
    assert {"spec", "attempts", "worker", "lease_expires_at", "trace_id"} <= columns, sorted(columns)


def test_accept_writes_a_claimable_row_with_one_statement():
    """受理必须是**一步**写完的可认领行：状态、spec、归属一起落。

    原来分两步（先 `pending` 再 UPDATE 成 `queued`），那个窗口里的行既没人能认领
    （claim 只认 queued）又被深度算进"排队"，用户看到的就是一个永远不动的数字。
    """
    uid = _user("accept-user")
    try:
        queueing.accept(
            job_id="job_accept",
            user_id=uid,
            org_id=7,
            question="一步受理",
            mode="mock",
            session_id=None,
            pack=None,
            spec={"question": "一步受理", "source_ref": "dataset:1"},
        )
        row = query_one("SELECT status, spec, worker, user_id, org_id, attempts FROM jobs WHERE job_id='job_accept'")
        assert row["status"] == "queued" and row["worker"] is None, row
        assert row["user_id"] == uid and int(row["attempts"] or 0) == 0, row
        # 企业归属在受理这一行就盖下去：没归属的作业行会被共享读默认拒绝，
        # 同企业的人就看不见彼此跑过什么（P3 的接线靠这一列）
        assert int(row["org_id"]) == 7, row
        assert json.loads(row["spec"])["source_ref"] == "dataset:1", row
    finally:
        _clean("job_accept", uid)


def test_a_pending_row_is_stale_not_queued():
    """`pending` 单独一栏报出来，不折进 `queued`。

    开发机那座库上实测堆着 57 条这种行（P0 压测留下的），折进 queued 之后接口一直在说
    "有 57 个在排队"，而认领者一条都取不到。分开报是"不许把没人负责伪装成还在跑"的同一条纪律。
    """
    uid = _user("stale-user")
    before = queueing.stats()
    _job("job_stale", uid, "pending", spec="")
    try:
        stats = queueing.stats()
        assert stats["stale_pending"] == before["stale_pending"] + 1, (before, stats)
        assert stats["queued"] == before["queued"], "pending 折进 queued 就是给用户一个永远不动的数字"
        grabbed = queueing.claim("stale-probe-worker")
        # 会话库是共享的：如果这里捞到了别的用例留下的 queued 行，原样放回去——
        # 本用例只判一件事：job_stale 不被任何人认领。
        if grabbed is not None and grabbed["job_id"] != "job_stale":
            execute(
                "UPDATE jobs SET status='queued', worker=NULL, lease_expires_at=NULL WHERE job_id=?",
                (grabbed["job_id"],),
            )
        assert (grabbed or {}).get("job_id") != "job_stale", "没人认领的行被报成了在排队"
        row = query_one("SELECT status, worker FROM jobs WHERE job_id='job_stale'")
        assert row["status"] == "pending" and row["worker"] is None, row
    finally:
        _clean("job_stale", uid)
