"""P6-1：`trace_id` 生在受理第一跳，而且**只生在一处**。

这一片要守住的不是"有个字段"，而是三件会各自漂移的事：

1. **一个生成点**。两个创建作业的入口（`/api/analyze` 与会话续轮）都汇到 `submit_analysis`；
   如果各写一条生成逻辑，"凭一个 id 查回这次分析"就只对一半的请求成立——和幂等键当初
   必须装在 `queueing.accept` 而不是装在路由里是同一个理由。
2. **入站那个串是客户端给的**。形状不对就不能收：换行与控制字符进了日志行，
   一条日志就变成两条（日志注入）；超长如果"截断到列宽就收下"，两个不同的上游 id 会撞成
   同一条链路，那是**假关联**，比没有 trace 更糟。所以这两族各有一条用例。
3. **它不参与判定**。这里没有一条用例是"按 trace 查到别人的作业"——归属判据只有
   `app/access.py` 那一份，`trace_id` 只是留痕。

测试里那一串 `SHAPE` 是从外部抄的一份尺子，不是复用模块的判据：如果哪天模块的白名单放宽了，
这条用例要红着告诉人"外部看到的规则变了"。
"""

from __future__ import annotations

import concurrent.futures
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, query_one
from app.main import app
from app.models import Job
from agentflow.core import trace

SHAPE = re.compile(r"[A-Za-z0-9_-]{1,32}")
CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    init_db()
    return tmp_path


def _login(client: TestClient, username: str = "admin", password: str = "admin") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def _dataset(client: TestClient) -> int:
    response = client.post("/api/datasets", files={"file": ("login_auth.csv", CSV.encode(), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _analyze(client: TestClient, dataset_id: int, **headers):
    return client.post(
        "/api/analyze",
        json={"question": "失败登录次数最高的用户是谁？", "dataset_id": dataset_id, "mode": "mock"},
        headers=headers,
    )


def _row(job_id: str) -> dict:
    return query_one("SELECT job_id, trace_id, spec FROM jobs WHERE job_id = ?", (job_id,))


# ---------------------------------------------------------------- 形状与沿用


def test_generated_ids_fit_the_column_that_has_to_hold_them():
    """生成值的长度必须等于列宽：窄了查不回来，宽了在 Postgres 上会被静默截断。

    断言等号而不是 `<=`，是因为两边只要有一个数被改动，另一处不会报错——它只会让
    "凭一个 id 查回整条链路"这句话在某些库上悄悄不成立。
    """
    generated = trace.new_trace_id()
    assert SHAPE.fullmatch(generated), generated
    assert len(generated) == trace.MAX_LEN
    assert trace.MAX_LEN == Job.__table__.c.trace_id.type.length


@pytest.mark.parametrize(
    "inbound,usable",
    [
        ("abc-123", True),
        ("A_b-9" + "x" * 26, True),  # 正好 32：列宽边界要真的过一遍
        ("x" * 33, False),  # 超一格就不收
        ("has space", False),
        ("line\nbreak", False),  # 日志注入那一格
        ("tab\tchar", False),
        ("semi;colon", False),
        ("quote'and\"double", False),
        ("中文标识", False),
        ("", False),
        ("   ", False),
        (None, False),
        (12345, False),  # 不是字符串就不猜它想表达什么
    ],
)
def test_only_a_well_shaped_inbound_trace_is_carried_forward(inbound, usable):
    effective, reused = trace.adopt(inbound)
    assert reused is usable
    assert SHAPE.fullmatch(effective), effective
    if usable:
        assert effective == str(inbound).strip()


def test_an_over_long_inbound_is_not_truncated_into_someone_elses_id():
    """截断到列宽看似宽容，实际是把两条无关链路合成一条。

    上游 A 的 40 位 id 与上游 B 的前 32 位相同的 id，截断后完全一样；报表上会显示成
    "这两次提交在同一条链路上"。宁可另起一条新链（诚实：这条从受理层开始）。
    """
    inbound = "t" * 32 + "abcdef"
    effective, reused = trace.adopt(inbound)
    assert reused is False
    assert effective != inbound[: trace.MAX_LEN]


# ---------------------------------------------------------------- 绑定与跨线程


def test_binding_replaces_and_restores_what_it_touched():
    with trace.bind("outer-chain"):
        assert trace.current() == "outer-chain"
        with trace.bind("inner-chain"):
            assert trace.current() == "inner-chain"
            assert trace.describe() == "inner-chain"
        assert trace.current() == "outer-chain"
    assert trace.current() is None
    # 没绑的时候 describe 给的是 "-"，不是空串：空串在日志里读起来像"这条被省略了"
    assert trace.describe() == "-"


def test_a_job_without_a_trace_does_not_inherit_the_previous_one():
    """长命认领线程会连着跑好几个作业。没 trace 的那个必须**清空**，不能留着上一个的。

    留着的表现是两次运行在日志里写成同一条链路——那是假关联，比"这一段没 trace"更难查。
    """
    with trace.bind("previous-job"):
        with trace.bind(None):
            assert trace.current() is None
        assert trace.current() == "previous-job"
        # 形状不对的值同样当"没有"处理，而不是原样绑进去
        with trace.bind("bad\nshape"):
            assert trace.current() is None


def test_wrap_carries_the_chain_into_a_thread_pool_and_a_bare_submit_does_not():
    """`ThreadPoolExecutor` 不复制 contextvar（只有 asyncio 的任务会）。

    两条都断言是刻意的：只断"`wrap` 带得过去"的话，将来有人把并发那一层换成普通线程、
    或者发现裸提交"碰巧"也带着值（同线程复用），这条下限就没人守了。
    `_execute_dag` 的任务单元必须在提交处包一次——工具调用与子进程都发生在那一层。
    """

    def read() -> str | None:
        return trace.current()

    with trace.bind("dashed-chain"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(read).result(timeout=5) is None  # 裸提交：断了才是对的
            assert pool.submit(trace.wrap(read)).result(timeout=5) == "dashed-chain"
        assert trace.current() == "dashed-chain"


# ---------------------------------------------------------------- 接口那条路


def test_a_submission_lands_the_same_trace_in_the_row_the_response_and_the_spec(env):
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        first = _analyze(client, dataset_id)
        assert first.status_code == 200, first.text
        issued = first.json()["trace_id"]
        assert issued and SHAPE.fullmatch(issued), first.json()

        row = _row(first.json()["job_id"])
        assert row["trace_id"] == issued
        # 库里那列是给运维反查用的，spec 那份是给执行侧绑回引擎用的：两个读者、一个值。
        # 一旦这两处能各自成立，"凭一个 id 查回整条链路"就变成看运气查到哪一段。
        assert json.loads(row["spec"])["trace_id"] == issued

        # 作业详情那条读路径也要带着它（用户报障时给的就是这一个串）
        detail = client.get(f"/api/jobs/{first.json()['job_id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["trace_id"] == issued


def test_a_second_submission_without_a_key_is_a_second_chain(env):
    """没带幂等键就是两次独立提交，各自的链路标识必须不同（不许"看起来一样"）。"""
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        one = _analyze(client, dataset_id).json()
        two = _analyze(client, dataset_id).json()
        assert one["job_id"] != two["job_id"]
        assert one["trace_id"] != two["trace_id"]


def test_a_well_shaped_inbound_trace_is_carried_all_the_way_to_the_row(env):
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        response = _analyze(client, dataset_id, **{"X-Trace-Id": "gateway-chain-77"})
        assert response.status_code == 200, response.text
        assert response.json()["trace_id"] == "gateway-chain-77"
        assert _row(response.json()["job_id"])["trace_id"] == "gateway-chain-77"


def test_an_unusable_inbound_trace_starts_a_fresh_chain_and_says_so(env, caplog):
    """入站那个串是客户端控制的：不收，而且要**说出来**。

    静默改写的话，客户端以为自己那条贯穿到底，运维按它查就永远查不到——
    这和"静默截断"是同一类谎，只是换了一个方向。
    样本用"带空格与分号"那一串：换行在模块层那条用例里已经测过，HTTP 头本身不收换行，
    在接口层硬造就是在测 httpx 而不是测被测系统。
    """
    injected = "bogus; shape with spaces"
    with caplog.at_level("WARNING", logger="agentflow.accept"):
        with TestClient(app) as client:
            _login(client)
            dataset_id = _dataset(client)
            response = _analyze(client, dataset_id, **{"X-Trace-Id": injected})
    assert response.status_code == 200, response.text
    issued = response.json()["trace_id"]
    assert issued != injected and SHAPE.fullmatch(issued), response.json()
    # 那条 warning 里不能把入站的原文抄进去：原文是客户端控制的，抄进来就等于
    # 让报障的人照着一条根本不存在的链路去查
    assert injected not in caplog.text
    assert "另起一条" in caplog.text


def test_a_replay_leaves_the_rows_first_trace_alone(env):
    """重放不新建行，也就不该把行上的链路改成第二次提交那条。

    库里第一次的 trace 才是"这次分析"的链路；第二次提交自己的串只出现在那一条 warning/日志里。
    这条用例盯的是"将来有人为了同步 trace 在 accept 里补一条 UPDATE"那种改法：
    它会让同一次分析的两条链路在报表里换人。
    """
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        headers = {"Idempotency-Key": "trace-replay"}
        first = _analyze(client, dataset_id, **headers, **{"X-Trace-Id": "trace-first"}).json()
        assert first["idempotency_replayed"] is False
        second = _analyze(client, dataset_id, **headers, **{"X-Trace-Id": "trace-second"}).json()
        assert second["idempotency_replayed"] is True, second
        assert second["job_id"] == first["job_id"]
        assert second["trace_id"] == "trace-first", second
        assert _row(first["job_id"])["trace_id"] == "trace-first"


def test_the_second_entry_also_starts_a_chain(env):
    """会话续轮是第二个创建作业的入口，它必须说同一种话。

    只在 `/api/analyze` 上接生成点，表现不是报错而是这条路上 `trace_id` 恒为 null——
    而响应里有这一格，读的人就会以为"这次运行没链路"，不是"这个入口没接"。
    """
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/datasets", files={"file": ("login_auth.csv", CSV.encode(), "text/csv")})
        assert created.status_code == 200, created.text
        session = client.post(
            "/api/sessions", json={"dataset_id": created.json()["id"], "title": "trace 第二入口"}
        )
        assert session.status_code == 200, session.text
        message = client.post(
            f"/api/sessions/{session.json()['session_id']}/messages",
            json={"question": "失败登录集中在哪个小时？", "mode": "mock"},
            headers={"X-Trace-Id": "session-chain-9"},
        )
        assert message.status_code == 200, message.text
        body = message.json()
        assert body["trace_id"] == "session-chain-9", body
        assert _row(body["job_id"])["trace_id"] == "session-chain-9"


def test_knowing_someone_elses_trace_does_not_open_their_job(env):
    """`trace_id` 是留痕，不是权限凭据。

    这条用例把那句话钉在执行路径上：B 知道 A 的 `job_id` 与 A 的链路标识，读回来仍然是 404。
    之所以不写成"扫源码里不许出现 `WHERE trace_id`"那种结构守卫——P6-4 就要加一条按 trace
    反查的读路径（带归属谓词），那种守卫会把正确的下一步报成违规。
    归属判据只有 `app/access.py` 那一份，这一格只是不给它开第二条路。
    """
    with TestClient(app) as owner:
        _login(owner)
        dataset_id = _dataset(owner)
        issued = _analyze(owner, dataset_id, **{"X-Trace-Id": "owner-chain-1"}).json()

        created = owner.post(
            "/api/users", json={"username": "trace_observer", "password": "observer-pass-1"}
        )
        assert created.status_code == 200, created.text

    with TestClient(app) as observer:
        _login(observer, username="trace_observer", password="observer-pass-1")
        # 带别人的 trace 也一样查不到——"不存在"与"别人的"本来就是同一个 404
        detail = observer.get(f"/api/jobs/{issued['job_id']}", headers={"X-Trace-Id": "owner-chain-1"})
        assert detail.status_code == 404, detail.text
        stream = observer.get(f"/api/jobs/{issued['job_id']}/events", headers={"X-Trace-Id": "owner-chain-1"})
        assert stream.status_code == 404, stream.text

    # 正向对照：同一条作业在提交者眼里查得到，而且报的就是那条链路。
    # 没有这一格，上面两个 404 可能只是"这条路由本来就是坏的"——那是借来的前置条件，不是拦住了越权。
    with TestClient(app) as back:
        _login(back)
        seen = back.get(f"/api/jobs/{issued['job_id']}")
        assert seen.status_code == 200, seen.text
        assert seen.json()["trace_id"] == "owner-chain-1", seen.json()
