"""建号时指定企业（P3 剩下的那件"成员管理"，形态是用户 2026-10-08 选的）。

判据不在这里重复实现：这些用例验的是**接口有没有把归属正确地盖上去**，以及
"没企业的账号确实看不见别人的东西"——共享那条判据本身由 `tests/test_org_sharing.py` 钉。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import execute, init_db, query_one
from app.main import app

CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    init_db()
    return tmp_path


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def _org(slug: str) -> int:
    execute("DELETE FROM organizations WHERE slug = ?", (slug,))
    return execute("INSERT INTO organizations (slug, name) VALUES (?, ?)", (slug, slug.title()))


def _drop(username: str) -> None:
    row = query_one("SELECT id FROM users WHERE username = ?", (username,))
    if row:
        execute("DELETE FROM memberships WHERE user_id = ?", (row["id"],))
        execute("DELETE FROM users WHERE id = ?", (row["id"],))


def _create(client: TestClient, username: str, **body):
    _drop(username)
    return client.post("/api/users", json={"username": username, "password": "long-enough-pw-1", **body})


def test_only_admin_can_create_accounts(env):
    """建号是"谁能进平台"的入口：普通用户一律 403，企业内角色不参与判定。"""
    _org("alpha")
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        assert _create(client, "plain_user", org="alpha").status_code == 200
        _login(client, "plain_user", "long-enough-pw-1")
        response = client.post("/api/users", json={"username": "sneaky_one", "password": "long-enough-pw-1"})
        assert response.status_code == 403, response.text


def test_created_user_lands_in_the_named_org_and_can_see_shared_data(env):
    """建号当场指定的企业必须真的生效：新号能看见同企业共享的数据集。"""
    _org("alpha")
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        assert _create(client, "member_one", org="alpha").status_code == 200

        # 先由已有成员造一份共享数据（这里用 admin 自己入企业，避免再造一遍账号）
        uid = query_one("SELECT id FROM users WHERE username = 'admin'")["id"]
        org_id = query_one("SELECT id FROM organizations WHERE slug = 'alpha'")["id"]
        execute("INSERT OR REPLACE INTO memberships (user_id, org_id, role) VALUES (?, ?, 'admin')", (uid, org_id))
        client.post("/api/datasets", files={"file": ("login.csv", CSV.encode("utf-8"), "text/csv")})

        _login(client, "member_one", "long-enough-pw-1")
        me = client.get("/api/auth/me").json()
        assert [item["slug"] for item in me["orgs"]] == ["alpha"], me
        listed = [row["filename"] for row in client.get("/api/datasets").json()]
        assert "login.csv" in listed, "建号指定的企业没生效：新号看不见同企业共享的数据集"
        members = client.get("/api/users").json()
        assert {row["username"] for row in members} == {"admin", "member_one"}, members
        assert [row for row in members if row["username"] == "member_one"][0]["is_me"] is True


def test_account_without_org_stays_unassigned(env):
    """`org` 留空 = 未归属：能登录、能建自己的资源，但看不见别人的（默认拒绝）。"""
    _org("alpha")
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        created = _create(client, "no_org_user")
        assert created.status_code == 200 and created.json()["org_id"] == 0, created.text
        uid = query_one("SELECT id FROM memberships WHERE user_id = ?", (created.json()["id"],))
        assert uid is None, "没指定企业却长出成员关系：那是接口在替用户做主"

        _login(client, "no_org_user", "long-enough-pw-1")
        assert client.get("/api/auth/me").json()["orgs"] == []
        assert client.get("/api/users").json() == [], "无企业的人不该看到任何成员名单"


def test_rejections_are_specific(env):
    """形状、口令强度、重名、未知企业、拼错的键——五种拒法各给各的理由。"""
    _org("alpha")
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        assert _create(client, "weak_pw", password="short").status_code == 422
        assert client.post("/api/users", json={"username": "a", "password": "long-enough-pw-1"}).status_code == 422
        assert _create(client, "dup_user", org="alpha").status_code == 200
        assert client.post(
            "/api/users", json={"username": "dup_user", "password": "long-enough-pw-1"}
        ).status_code == 409, "重名必须报冲突，不能静默改口令或静默不建"
        missing_org = client.post(
            "/api/users", json={"username": "later_user", "password": "long-enough-pw-1", "org": "nope"}
        )
        assert missing_org.status_code == 422 and "alpha" in missing_org.json()["detail"]
        typo = client.post(
            "/api/users", json={"username": "typo_user", "password": "long-enough-pw-1", "orgn": "alpha"}
        )
        assert typo.status_code == 422, "拼错的键静默通过 = 管理员以为把人建进企业了，实际是未归属"
        _drop("typo_user")


def test_org_list_is_admin_only(env):
    """企业名单只给管理员：普通用户不需要知道平台上有多少租户。"""
    _org("alpha")
    _org("beta")
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        assert _create(client, "list_peer", org="alpha").status_code == 200
        assert {row["slug"] for row in client.get("/api/orgs").json()} >= {"alpha", "beta"}
        _login(client, "list_peer", "long-enough-pw-1")
        assert client.get("/api/orgs").status_code == 403


def test_login_works_for_created_account_with_the_chosen_password(env):
    """建号后能按给的口令登录（这条听上去像废话，但哈希写错只有在这一步才暴露）。"""
    _org("alpha")
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        _drop("fresh_user")
        assert client.post(
            "/api/users", json={"username": "fresh_user", "password": "fresh-pass-1234", "org": "alpha"}
        ).status_code == 200
    with TestClient(app) as client:
        deadline = time.time() + 5
        while time.time() < deadline:
            if client.post("/api/auth/login", json={"username": "fresh_user", "password": "fresh-pass-1234"}).status_code == 200:
                break
        else:
            raise AssertionError("新建账号登录不上")
