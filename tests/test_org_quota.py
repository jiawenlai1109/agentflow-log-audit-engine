"""P4-2：企业配额与按企业的公平认领。

两片判据分开测，因为它们坏起来是两种样子：

- **公平调度**坏 = 队列仍然工作，只是某一家企业批量提交时别人排到最后 —— 表现是"同事三秒
  后能看到报告"变成"明天能看到"，没人会报缺陷，只有读数里能看到。所以这里直接验认领顺序。
- **配额**坏 = 要么该拒的没拒（一家企业吃满上游），要么不该拒的拒了（用户提交不上）。
  后者更贵，所以"没设配额 = 一条都不拦"和"0 与负数 = 配错而不是锁死"这两条各占一条用例。

真实提交这条路必须走接口：`org_id` 是从登录态推出来的（`access.primary_org`），
如果只用函数级用例，"路由把企业号传错了"这一类缺陷就测不着——而它表现成
"A 企业的作业花掉了 B 企业的配额"，是最难查的那种。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import quota
from app.db import execute, init_db, query_one
from app.main import app
from app.queueing import claim, org_usage

CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n"

RUNNING_LEASE = "9999-12-31 23:59:59"  # 租约远在未来：别的用例与 reclaim 都不会碰这行


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    init_db()
    return tmp_path


def _org(slug: str) -> int:
    execute("DELETE FROM organizations WHERE slug = ?", (slug,))
    return execute("INSERT INTO organizations (slug, name) VALUES (?, ?)", (slug, slug.title()))


def _job(job_id: str, org_id: int, status: str = "queued", *, worker: str | None = None, created: str | None = None) -> None:
    """直接落一行作业。清掉同名行再插，用例之间不互相污染。"""
    execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    if created:
        execute(
            "INSERT INTO jobs (job_id, user_id, org_id, question, mode, status, worker, lease_expires_at, spec, created_at) "
            "VALUES (?, 1, ?, '配额用例', 'mock', ?, ?, ?, '{}', ?)",
            (job_id, org_id, status, worker, RUNNING_LEASE if status == "running" else None, created),
        )
        return
    execute(
        "INSERT INTO jobs (job_id, user_id, org_id, question, mode, status, worker, lease_expires_at, spec) "
        "VALUES (?, 1, ?, '配额用例', 'mock', ?, ?, ?, '{}')",
        (job_id, org_id, status, worker, RUNNING_LEASE if status == "running" else None),
    )


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


# ---------------------------------------------------------------- 公平认领


def test_claim_gives_the_org_with_fewer_running_jobs_first(env):
    """两家企业各有待领作业时，先给"在跑的那个最少"的那家——哪怕它提交得更晚。

    这里刻意让 org A 的两行 id 更小（纯 FIFO 会先领它们），org B 只有一行且 id 最大：
    如果调度退回 FIFO，B 那个作业要等 A 的两个都跑完，而 A 正在批量提交。
    """
    alpha, beta = _org("fair-alpha"), _org("fair-beta")
    _job("job_fair_a1", alpha, "running", worker="w1:1:aaa")
    _job("job_fair_a2", alpha)  # 更早提交（id 比 b1 小）
    _job("job_fair_b1", beta)  # 更晚提交，但它那家企业此刻一个在跑的都没有

    taken = claim("w2:1:bbb")
    assert taken and taken["job_id"] == "job_fair_b1", f"公平调度没生效，领到的是 {taken}"


def test_claim_breaks_ties_by_submission_order(env):
    """在跑数打平的时候退回 FIFO：公平是"别被一家企业吃光"，不是" reverse 提交顺序"。"""
    alpha, beta = _org("tie-alpha"), _org("tie-beta")
    _job("job_tie_a1", alpha)
    _job("job_tie_b1", beta)
    assert (claim("w3:1:ccc") or {}).get("job_id") == "job_tie_a1"


def test_one_org_cannot_hold_the_whole_queue(env):
    """一家企业把在跑的位置占满之后，另一家的待领作业仍然领得到（不是等前者跑完）。"""
    alpha, beta = _org("hog-alpha"), _org("hog-beta")
    for index in range(3):
        _job(f"job_hog_a{index}", alpha, "running", worker=f"w{index}:1:xxx")
    _job("job_hog_b0", beta)
    taken = claim("w9:1:ddd")
    assert (taken or {}).get("job_id") == "job_hog_b0", taken


# ---------------------------------------------------------------- 配额判定（函数层）


def test_no_quota_row_blocks_nothing(env):
    """没谈过配额 = 一条都不拦，且**不发一个"看起来合理"的默认上限**。"""
    org = _org("plain-org")
    _job("job_plain", org, "running", worker="w1:1:ppp")
    assert quota.quota_for(org) == {}
    assert quota.decide(org, usage=org_usage(org))["allowed"] is True


def test_concurrent_limit_counts_queued_and_running_together(env):
    """"同时几个"包含排在队里的：只算在跑的话，批量提交 50 个的作业会全部瞬间进队列，
    上游与 worker 还是被吃满——那条判据就没拦住它要拦的东西。

    判据是 `已占位 >= 上限` 就拒：上限 2、此刻已经占了 2 个坑，第 3 个提交进来就是 3 个，
    所以它在受理时就被拒，而不是变成一行"排队中"的作业。
    """
    org = _org("active-org")
    quota.set_quota(org, {"limit_concurrent_jobs": 2})
    _job("job_act_1", org, "running", worker="w1:1:q1")
    assert quota.decide(org, usage=org_usage(org))["allowed"] is True
    _job("job_act_2", org)  # 排队中的那个也占位
    decision = quota.decide(org, usage=org_usage(org))
    assert decision["allowed"] is False and decision["limit"] == "limit_concurrent_jobs", decision
    assert decision["used"] == 2 and decision["cap"] == 2, decision
    assert "排队 + 在跑都算" in decision["reason"], decision["reason"]


def test_jobs_per_day_counts_only_today(env):
    """每日用量按本地日界重置：昨天的成功作业不许占今天的名额。"""
    org = _org("daily-org")
    quota.set_quota(org, {"limit_jobs_per_day": 2})
    _job("job_day_old", org, "success", created="2026-01-01 08:00:00")
    _job("job_day_1", org, "success")
    _job("job_day_2", org, "partial")
    usage = org_usage(org)
    assert usage["today"] == 2 and usage["active"] == 0, usage
    decision = quota.decide(org, usage=usage)
    assert decision["allowed"] is False and decision["limit"] == "limit_jobs_per_day", decision
    assert "今天已经提交了 2 个" in decision["reason"], decision["reason"]


def test_a_zero_cap_is_a_misconfiguration_not_a_lock(env):
    """上限写 0 不是"禁止这家企业提交"，是配错了：必须说成人话，而不是静默拦死或静默放行。"""
    org = _org("zero-org")
    execute(
        "INSERT INTO org_quotas (org_id, limit_concurrent_jobs) VALUES (?, 0)",
        (org,),
    )
    decision = quota.decide(org, usage={"active": 0, "today": 0})
    assert decision["allowed"] is False and decision["kind"] == "quota_misconfigured", decision
    assert "配错" in decision["reason"], decision["reason"]


def test_unenforced_limits_say_so_instead_of_looking_live(env):
    """表里有四列，只有两列参与判定。设了不判的那列不能看起来像生效了。"""
    org = _org("unenforced-org")
    quota.set_quota(org, {"limit_llm_calls_per_day": 1})
    assert quota.decide(org, usage={"active": 99, "today": 99})["allowed"] is True
    caps = quota.capabilities()
    assert "limit_llm_calls_per_day" in caps["unenforced"] and "limit_concurrent_jobs" in caps["enforced"]
    assert "不会生效" in caps["note"], caps


def test_patch_keeps_columns_that_were_not_sent(env):
    """写配额是 PATCH：漏传一列就把它清成"不设限"，表现是配额忽然失效而没人改过。"""
    org = _org("patch-org")
    quota.set_quota(org, {"limit_concurrent_jobs": 5})
    quota.set_quota(org, {"limit_jobs_per_day": 10})
    row = quota.quota_for(org)
    assert row["limit_concurrent_jobs"] == 5, row
    assert row["limit_jobs_per_day"] == 10, row
    # 显式传 null 才是清除——与"没提这一列"是两件事
    quota.set_quota(org, {"limit_concurrent_jobs": None})
    assert quota.quota_for(org)["limit_concurrent_jobs"] is None
    assert quota.quota_for(org)["limit_jobs_per_day"] == 10


def test_negative_cap_never_reaches_the_table(env):
    """写入口当场拒掉非正整数：进库之后它变成一条谁也说不清的判定分支。"""
    org = _org("negative-org")
    with pytest.raises(ValueError):
        quota.set_quota(org, {"limit_jobs_per_day": -1})


# ---------------------------------------------------------------- 受理那条路（接口层）


def _member(admin: TestClient, slug: str, username: str) -> int:
    org_id = _org(slug)
    created = admin.post("/api/users", json={"username": username, "password": "long-enough-123", "org": slug})
    assert created.status_code == 200, created.text
    return org_id


def _upload(client: TestClient) -> int:
    response = client.post("/api/datasets", files={"file": ("login_auth.csv", CSV.encode(), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_refused_submission_is_429_with_a_reason_and_no_job_row(env):
    """超配额 = 429 + `Retry-After` + 说清"几家几个"，而且**库里不留这行**。

    不留行是这条路径的性质：没跑成的事不该变成一个看起来合法的失败作业计数——
    那会把"配额拦住"混进"作业跑挂了"，而 P4 的退出判据要的恰恰是失败计数不涨。

    提交人是**企业成员本人**，不是 admin：配额判的是调用者的企业，用 admin 提交会判到
    未归属（org 0）那棵树上去，那条路下面单独测。
    """
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        org_id = _member(client, "quota-member", "quota_alice")
        assert client.patch(f"/api/orgs/{org_id}/quota", json={"limit_concurrent_jobs": 1}).status_code == 200

    with TestClient(app) as alice:
        _login(alice, "quota_alice", "long-enough-123")
        dataset_id = _upload(alice)
        _job("job_quota_busy", org_id, "running", worker="w1:1:busy")
        before = query_one("SELECT COUNT(*) AS n FROM jobs")["n"]

        refused = alice.post(
            "/api/analyze", json={"question": "失败登录次数最高的用户", "dataset_id": dataset_id, "mode": "mock"}
        )
        assert refused.status_code == 429, refused.text
        assert int(refused.headers["Retry-After"]) >= 1, dict(refused.headers)
        detail = refused.json()["detail"]
        assert "同时最多 1 个作业" in detail and "此刻已有 1 个" in detail, detail
        assert query_one("SELECT COUNT(*) AS n FROM jobs")["n"] == before, "被拒的提交不许留 jobs 行"


def test_quota_is_judged_on_the_callers_org_not_a_number_in_the_request(env):
    """企业号从登录态推出来：B 企业的人提交，占的是 B 的名额，与别家企业设的配额无关。"""
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        alpha = _member(client, "judge-alpha", "judge_a")
        beta = _org("judge-beta")
        client.patch(f"/api/orgs/{beta}/quota", json={"limit_concurrent_jobs": 1})
        _job("job_judge_beta_busy", beta, "running", worker="w1:1:bbb")

    with TestClient(app) as member:
        _login(member, "judge_a", "long-enough-123")
        dataset_id = _upload(member)
        # alpha 一条配额都没设，且它自己一个在跑的都没有：该放行
        first = member.post(
            "/api/analyze", json={"question": "失败登录次数最高的用户", "dataset_id": dataset_id, "mode": "mock"}
        )
        assert first.status_code == 200, first.text
        row = query_one("SELECT org_id FROM jobs WHERE job_id = ?", (first.json()["job_id"],))
        assert row["org_id"] == alpha, "作业行的企业归属必须由登录态决定，而不是请求里带的数"


def test_only_admin_can_set_a_quota(env):
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        org_id = _member(client, "writer-org", "writer_alice")
        _login(client, "writer_alice", "long-enough-123")  # 同一个客户端换身份，避免起第二个 app
        refused = client.patch(f"/api/orgs/{org_id}/quota", json={"limit_concurrent_jobs": 1})
        assert refused.status_code == 403, refused.text
        assert quota.quota_for(org_id).get("limit_concurrent_jobs") is None, "被拒的写不许留下半个值"


def test_quota_route_rejects_unknown_org_and_bad_shapes(env):
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        org_id = _org("shape-org")
        missing = client.patch("/api/orgs/424242/quota", json={"limit_concurrent_jobs": 2})
        assert missing.status_code == 404, missing.text
        zero = client.patch(f"/api/orgs/{org_id}/quota", json={"limit_concurrent_jobs": 0})
        assert zero.status_code == 422, zero.text
        typo = client.patch(f"/api/orgs/{org_id}/quota", json={"limit_concurrent_job": 2})
        assert typo.status_code == 422, typo.text
        assert quota.quota_for(org_id) == {}, "形状错的请求一个值都不该写进去"

