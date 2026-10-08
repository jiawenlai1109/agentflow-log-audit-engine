"""P4-3：敏感写入口的限流。

两片分开测：`app/ratelimit.py` 的**计数规则**（窗口、键、逐出）用显式时间戳测，
接口的**形状**（429、`Retry-After`、被拒不产生副作用）走真实请求。

为什么两条都要：只测函数层，"路由忘了调闸"就测不着（P4 这一族已经为
"闸门装在一处、别处没装"付过学费）；只测接口层，窗口边界与逐出这种"要等一分钟才会坏"
的形状就只能靠真等。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.db import execute, init_db, query_one
from app.main import app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    init_db()
    return tmp_path


def _login(client: TestClient, username: str, password: str):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    if response.status_code == 200:
        # 登录成功就把身份带上：下面几条用例要拿这个会话去建号，
        # "登录"与"用这个登录态"分两步写的测试，坏起来的样子是 401 而不是被测性质没成立。
        client.headers["Authorization"] = f"Bearer {response.json()['token']}"
    return response


# ---------------------------------------------------------------- 计数规则（不靠真等）


def test_the_window_reopens_and_a_stale_hit_does_not_count():
    """窗口过去之后同一主体重新有额度；过期那些次不再占位。"""
    for offset in range(ratelimit.LOGIN_PER_USER):
        verdict = ratelimit.check("login_user", "alice", limit=ratelimit.LOGIN_PER_USER, window_s=60, now=1000.0 + offset)
        assert verdict["allowed"] is True, offset
    denied = ratelimit.check("login_user", "alice", limit=ratelimit.LOGIN_PER_USER, window_s=60, now=1059.0)
    assert denied["allowed"] is False and denied["retry_after"] >= 1, denied
    reopened = ratelimit.check("login_user", "alice", limit=ratelimit.LOGIN_PER_USER, window_s=60, now=1061.0)
    assert reopened["allowed"] is True, reopened


def test_refused_attempts_are_still_counted():
    """被拒的那一次也要记账：不记的话一直在刷的攻击者在计数上是 0 次，运维读数是干净的。"""
    now = 500.0
    first = ratelimit.check("probe", "subject", limit=2, window_s=60, now=now)
    second = ratelimit.check("probe", "subject", limit=2, window_s=60, now=now)
    third = ratelimit.check("probe", "subject", limit=2, window_s=60, now=now)
    assert (first["attempts"], second["attempts"]) == (1, 2) and first["allowed"] and second["allowed"]
    assert third["allowed"] is False, third
    assert third["attempts"] == 3, third  # 含"被拒的那一次"
    assert third["retry_after"] >= 1, third


def test_keys_are_capped_and_the_eviction_says_so(tmp_path, monkeypatch):
    """键数有硬顶，撞到顶逐出最早的键，而**逐出这件事在读数里自曝**。

    不设顶就是"长跑进程 + 有人拿一批用户名各试一次"必然撑爆的那类形状
    （`JobManager._events` 的老账）。这里把 `MAX_KEYS` 调小来逼出那条分支。
    """
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 4)
    for index in range(30):
        ratelimit.check("login_user", f"user{index}", limit=5, window_s=60, now=100.0 + index)
    snapshot = ratelimit.snapshot()
    assert snapshot["keys"] <= 4, snapshot
    assert snapshot["evictions"] > 0, snapshot
    assert snapshot["max_keys"] == 4, snapshot


def test_a_bad_config_value_falls_back_instead_of_locking_the_door(caplog):
    """限额写 0 / 非数字 = 用默认值并留痕，不当成"把登录彻底关掉"，也不当成"随便刷"。"""
    assert ratelimit._positive_int("0", 10) == 10
    assert ratelimit._positive_int("abc", 10) == 10
    assert ratelimit._positive_int("-5", 10) == 10
    assert ratelimit._positive_int("7", 10) == 7
    assert "按默认值" in caplog.text, caplog.text


def test_the_reading_names_its_scope():
    """读数必须自带 `scope=per_process`：不提它，"每进程 10 次"会被读成"全平台 10 次"。"""
    assert ratelimit.snapshot()["scope"] == "per_process"


def test_startup_log_announces_the_budgets(env, caplog):
    """启动日志里要说清额度与范围，`snapshot()` 这才算有真消费者。

    只有测试在读的读数就是"给运维看"的假出口——与"配额表建好了但没人查"是同一族：
    接口存在 ≠ 有人看它。这条用例同时是那个出口的证明（位点 PL15 拆掉这行会红）。
    """
    import logging

    with caplog.at_level(logging.INFO, logger="agentflow.web"), TestClient(app):
        pass
    text = caplog.text
    assert "入口限流" in text, text
    assert "每个进程各自的额度" in text, "范围不提，运维就会把每进程的数读成平台级的数"


# ---------------------------------------------------------------- 接口形状（真实请求）


def test_eleventh_login_attempt_on_one_account_is_refused(env):
    with TestClient(app) as client:
        statuses = [_login(client, "target_user", "wrong-password").status_code for _ in range(ratelimit.LOGIN_PER_USER)]
        assert set(statuses) == {401}, statuses  # 前 10 次是"口令错"，不是"你别再来"
        refused = _login(client, "target_user", "wrong-password")
        assert refused.status_code == 429, refused.text
        assert int(refused.headers["Retry-After"]) >= 1, dict(refused.headers)


def test_case_variants_do_not_each_get_a_fresh_budget(env):
    """`ADMIN` 与 `admin` 在限流上必须是同一个预算：否则每个大小写变体都有一条新的额度。"""
    with TestClient(app) as client:
        for index in range(ratelimit.LOGIN_PER_USER):
            name = "Admin" if index % 2 else "admin"
            _login(client, name, "wrong-password")
        refused = _login(client, "ADMIN", "wrong-password")
        assert refused.status_code == 429, refused.text


def test_sweeping_many_usernames_trips_the_source_budget(env):
    """拿一批用户名各试一次（枚举）由"来源"那道闸拦下，而不是每人一条新预算。"""
    with TestClient(app) as client:
        for index in range(ratelimit.LOGIN_PER_SOURCE):
            _login(client, f"swept_user_{index}", "wrong-password")
        refused = _login(client, "swept_user_last", "wrong-password")
        assert refused.status_code == 429, refused.text


def test_the_refusal_does_not_hand_out_the_budget(env):
    """429 的文案只给"该怎么做"（稍后再来），不给额度与已试次数：把 10 写进响应，
    就等于替正在试探的人标定上限——他只要贴着 9 次慢刷就能一直不被这道闸认出来。

    `Retry-After` 是**故意**留在那儿的（头里也有它）：那是"多久之后再试"，不是预算。
    顺带钉一条：这个数不许比窗口还长——报 61 秒而窗口只有 60 秒，客户端就白等一分钟。
    """
    with TestClient(app) as client:
        for _ in range(ratelimit.LOGIN_PER_USER):
            _login(client, "quiet_user", "wrong-password")
        refused = _login(client, "quiet_user", "wrong-password")
        assert refused.status_code == 429
        detail = refused.json()["detail"]
        retry_after = int(refused.headers["Retry-After"])
        assert str(ratelimit.LOGIN_PER_USER) not in detail, detail
        assert 1 <= retry_after <= ratelimit.WINDOW_S, retry_after


def test_a_correct_password_inside_the_budget_still_works(env):
    """限流不许把手滑两三次的人挡在门外——三次失败 + 一次成功必须还是 200。"""
    with TestClient(app) as client:
        for _ in range(3):
            assert _login(client, "admin", "nope").status_code == 401
        ok = _login(client, "admin", "admin")
        assert ok.status_code == 200, ok.text


def test_create_account_is_limited_and_refusal_leaves_no_row(env):
    """建号被拒时**不写行**：留一行半成品再拒，等于让被拒的请求把表撑大。"""
    with TestClient(app) as client:
        _login(client, "admin", "admin")
        created_before = query_one("SELECT COUNT(*) AS n FROM users")["n"]
        for index in range(ratelimit.ACCOUNTS_PER_ACTOR):
            response = client.post(
                "/api/users", json={"username": f"bulk{index}", "password": "long-enough-123"}
            )
            assert response.status_code == 200, response.text
        refused = client.post("/api/users", json={"username": "bulk_over", "password": "long-enough-123"})
        assert refused.status_code == 429, refused.text
        assert query_one("SELECT COUNT(*) AS n FROM users")["n"] == created_before + ratelimit.ACCOUNTS_PER_ACTOR
        assert query_one("SELECT id FROM users WHERE username = 'bulk_over'") is None


def test_the_limiter_runs_before_any_work_happens(env):
    """被 429 挡下时不查库、不算 PBKDF2：这条测的是"闸排在最早那一步"，不是"闸会拒"。

    量法：把 auth 模块里的 `query_one` 换成一个会记账的包装，被拒那一次必须一笔都不记。
    换完一定要**还原原来那个绑定**——`del` 掉只是把模块属性删了，下一次调用变成 NameError，
    那时红在脚手架上而不是红在断言上（本仓库为这类错记过纪律）。
    """
    import app.routers.auth as auth_module
    from app.db import query_one as db_query_one

    calls: list[str] = []
    original = auth_module.query_one

    def counting(sql, params=()):  # noqa: ANN001 - 与被替换函数的形状一致
        calls.append(sql)
        return db_query_one(sql, params)

    auth_module.query_one = counting
    try:
        with TestClient(app) as client:
            for _ in range(ratelimit.LOGIN_PER_USER):
                _login(client, "quiet_again", "wrong-password")
            assert calls, "假实现没收到任何查询，这条用例就没在测它声称测的东西"
            before = len(calls)
            refused = _login(client, "quiet_again", "wrong-password")
            assert refused.status_code == 429
            assert len(calls) == before, f"被拒之后还查了 {len(calls) - before} 次库：闸没排在最前面"
    finally:
        auth_module.query_one = original
