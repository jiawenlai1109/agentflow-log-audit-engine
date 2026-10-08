"""P3 的企业维度：同企业共享数据集与报告，跨企业一律不可见，删除仍只认造它的人。

判据只有一份实现（`app/access.py`），这一份文件测的是**它的效果**，不是它的写法：
每条都从 HTTP 走，因为"函数返回对的谓词"不等于"每个入口都拼上了那条谓词"——
本项目已经为"只测函数没测接线"付过一次学费。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import execute, query_one
from app.main import app
from app.security import hash_password

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"

CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n2026-09-05T01:03:03Z,admin,10.0.0.8,ok\n"


def _user(username: str, password: str) -> int:
    execute("DELETE FROM users WHERE username = ?", (username,))
    return execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password(password)),
    )


def _org(slug: str) -> int:
    execute("DELETE FROM organizations WHERE slug = ?", (slug,))
    return execute("INSERT INTO organizations (slug, name) VALUES (?, ?)", (slug, slug.upper()))


def _member(user_id: int, org_id: int, role: str = "member") -> None:
    execute(
        "INSERT OR REPLACE INTO memberships (user_id, org_id, role) VALUES (?, ?, ?)",
        (user_id, org_id, role),
    )


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def _upload_dataset(client: TestClient, name: str = "login_auth.csv") -> int:
    response = client.post("/api/datasets", files={"file": (name, CSV.encode("utf-8"), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _wait_job(client: TestClient, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("pending", "queued", "running"):
            return job
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} 超时未完成")


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    """产物与包目录指到临时位置：与生产同一套配置方式（环境变量），不是 monkeypatch 属性副本。"""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    from app.db import init_db

    init_db()
    return tmp_path


def test_dataset_is_shared_inside_the_org_but_not_outside(scratch):
    """同企业成员在列表里能看见彼此的数据集；外企业看不见，也读不到。"""
    alpha, beta = _org("alpha"), _org("beta")
    owner_a = _user("a_owner", "pw-a-owner")
    peer_a = _user("a_peer", "pw-a-peer")
    other_b = _user("b_owner", "pw-b-owner")
    _member(owner_a, alpha)
    _member(peer_a, alpha)
    _member(other_b, beta)

    with TestClient(app) as client:
        _login(client, "a_owner", "pw-a-owner")
        dataset_id = _upload_dataset(client)

        _login(client, "a_peer", "pw-a-peer")
        ids = [row["id"] for row in client.get("/api/datasets").json()]
        assert dataset_id in ids, "同企业成员没看见彼此的共享数据集"

        _login(client, "b_owner", "pw-b-owner")
        ids = [row["id"] for row in client.get("/api/datasets").json()]
        assert dataset_id not in ids, "跨企业看见了别人的数据集"
        # 直接拿 id 去提交分析：越权一律 404，不给"存在但没权限"这种可枚举的差异
        response = client.post(
            "/api/analyze", json={"question": "登录审计", "dataset_id": dataset_id, "mode": "mock"}
        )
        assert response.status_code == 404, response.text


def test_upload_stamps_the_org(scratch):
    """上传必须当场盖上企业归属：没归属的行共享读默认拒绝，等于"接了线但没人能共享"。"""
    alpha = _org("alpha")
    uid = _user("stamp_user", "pw-stamp")
    _member(uid, alpha)
    with TestClient(app) as client:
        _login(client, "stamp_user", "pw-stamp")
        dataset_id = _upload_dataset(client)
    row = query_one("SELECT user_id, org_id FROM datasets WHERE id = ?", (dataset_id,))
    assert row["user_id"] == uid and row["org_id"] == alpha, row


def test_unassigned_org_is_owner_only(scratch):
    """`org_id = 0`（没企业）只有自己看得见——默认拒绝，不是默认放行。"""
    loner = _user("loner", "pw-loner")
    colleague = _user("colleague", "pw-colleague")
    alpha = _org("alpha")
    _member(colleague, alpha)  # colleague 有企业，loner 没有
    with TestClient(app) as client:
        _login(client, "loner", "pw-loner")
        dataset_id = _upload_dataset(client)
        _login(client, "colleague", "pw-colleague")
        assert [row["id"] for row in client.get("/api/datasets").json()] == []
        row = query_one("SELECT org_id FROM datasets WHERE id = ?", (dataset_id,))
        assert row["org_id"] == 0, row


def test_a_dirty_membership_to_zero_cannot_make_unassigned_rows_shared(scratch):
    """"未归属企业"这道闸门防的是脏数据，而这条判据必须**真的挡得住**，不是装饰。

    如果哪天有人写进一条 `memberships(user, org_id=0)`（迁移脚本、手工修数据都会出这种行），
    只看"org_id 在我的成员关系里"就会把所有未归属的资源共享出去。`org_id <> 0` 就是为了
    这一行也不成立。这也是 MO2 那个变异最初"全绿"的原因：当前数据形状下它观察不到，
    所以补了这条能观察到的用例。
    """
    loner = _user("dirty_loner", "pw-dirty-loner")
    peer = _user("dirty_peer", "pw-dirty-peer")
    with TestClient(app) as client:
        _login(client, "dirty_loner", "pw-dirty-loner")
        dataset_id = _upload_dataset(client)
    execute("INSERT INTO memberships (user_id, org_id, role) VALUES (?, 0, 'member')", (peer,))
    with TestClient(app) as client:
        _login(client, "dirty_peer", "pw-dirty-peer")
        ids = [row["id"] for row in client.get("/api/datasets").json()]
        assert dataset_id not in ids, "一条 org_id=0 的成员关系把「未归属」变成了「人人可见」"


def test_delete_is_owner_only_even_inside_the_org(scratch):
    """可见 ≠ 可删：同企业成员删别人的数据集按"不存在"处理，而且那一行确实还在。"""
    alpha = _org("alpha")
    owner = _user("del_owner", "pw-del-owner")
    peer = _user("del_peer", "pw-del-peer")
    _member(owner, alpha)
    _member(peer, alpha)
    with TestClient(app) as client:
        _login(client, "del_owner", "pw-del-owner")
        dataset_id = _upload_dataset(client)

        _login(client, "del_peer", "pw-del-peer")
        assert dataset_id in [row["id"] for row in client.get("/api/datasets").json()]
        response = client.delete(f"/api/datasets/{dataset_id}")
        assert response.status_code == 404, "同企业成员删掉了别人的数据集"
        assert query_one("SELECT id FROM datasets WHERE id = ?", (dataset_id,)) is not None

        _login(client, "del_owner", "pw-del-owner")
        assert client.delete(f"/api/datasets/{dataset_id}").status_code == 200
        assert query_one("SELECT id FROM datasets WHERE id = ?", (dataset_id,)) is None


def test_reports_and_runs_are_shared_but_sessions_are_not(scratch):
    """报告/运行历史是企业内共享的；会话是个人的——这条区分是刻意的，不是漏了。"""
    alpha = _org("alpha")
    author = _user("rep_author", "pw-rep-author")
    peer = _user("rep_peer", "pw-rep-peer")
    _member(author, alpha)
    _member(peer, alpha)
    with TestClient(app) as client:
        _login(client, "rep_author", "pw-rep-author")
        dataset_id = _upload_dataset(client)
        session = client.post("/api/sessions", json={"title": "我的对话", "dataset_id": dataset_id}).json()
        job = client.post(
            "/api/analyze", json={"question": "对2026-09-05的登录日志做安全审计", "dataset_id": dataset_id, "mode": "mock"}
        ).json()
        finished = _wait_job(client, job["job_id"])
        assert finished["status"] in ("success", "partial", "degraded"), finished
        run_id = finished["run_id"]

        _login(client, "rep_peer", "pw-rep-peer")
        runs = [row["run_id"] for row in client.get("/api/runs").json()]
        assert run_id in runs, "同企业成员的历史列表里没有彼此的报告"
        report = client.get(f"/api/reports/{run_id}")
        assert report.status_code == 200, report.text
        mine = [row for row in client.get("/api/runs").json() if row["run_id"] == run_id]
        assert mine and mine[0]["is_mine"] is False, "列表里分不清是谁跑的"
        # 共享读会把"只给 owner 的字段"也一起共享出去，所以列表里不许出现服务器路径或别人的 id
        body = client.get("/api/runs").json()
        assert all("report_path" not in row and "user_id" not in row for row in body), body[:1]
        # 会话不共享：peer 的列表里没有 author 的会话；直接访问它的消息与删除都按"不存在"处理
        assert client.get("/api/sessions").json() == []
        assert client.get(f"/api/sessions/{session['session_id']}/messages").status_code == 404
        assert client.delete(f"/api/sessions/{session['session_id']}").status_code == 404
        assert query_one(
            "SELECT session_id FROM sessions WHERE session_id = ?", (session["session_id"],)
        ) is not None, "协作者虽然拿到 404，但那一行不能被动过"


def test_events_stream_respects_the_org_scope(scratch):
    """事件流也要带同一条判据：不能"能建流就能看任何 job 的终态"。"""
    alpha, beta = _org("alpha"), _org("beta")
    author = _user("sse_author", "pw-sse-author")
    peer = _user("sse_peer", "pw-sse-peer")
    outsider = _user("sse_out", "pw-sse-out")
    _member(author, alpha)
    _member(peer, alpha)
    _member(outsider, beta)
    with TestClient(app) as client:
        _login(client, "sse_author", "pw-sse-author")
        dataset_id = _upload_dataset(client)
        job_id = client.post(
            "/api/analyze", json={"question": "登录审计", "dataset_id": dataset_id, "mode": "mock"}
        ).json()["job_id"]

        _login(client, "sse_peer", "pw-sse-peer")
        assert client.get(f"/api/jobs/{job_id}").status_code == 200
        with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
            assert stream.status_code == 200

        _login(client, "sse_out", "pw-sse-out")
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert client.get(f"/api/jobs/{job_id}/events").status_code == 404
