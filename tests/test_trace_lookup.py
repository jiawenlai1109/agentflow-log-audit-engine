"""P6-4：凭一个 id 查回这次分析——P6 的退出判据。

后端整改规划给这句话的原样是"谁提交的、排了多久、谁认领的、几次重试、上游返回了什么形状"。
这一份文件测的就是**这句话有没有真的成立**，以及三件容易在这种"聚合读数"里偷偷犯的事：

1. **`trace_id` 不是权限凭据**。带别人的串查回来必须与"这条链路不存在"是同一个 404——
   区分二者就是开一个枚举入口。归属判据只有 `app/access.py` 那一份，这里不写第二条查询。
2. **"没量到"不许写成 0**。排队时长在"一条事件都还没有"的时候是 `null`，不是 `0.0`：
   后者读起来是"排得真快"，而真相是"没人认领"。这是 P5 收尾那轮立下的那条口径在这一格的落地。
3. **一条链路可以挂多条作业行**。入站那个串是客户端自带的，同一个人复用同一个 id 连发两次
   （不带幂等键）就是两行。所以交回的是**列表**——把"一次提交 = 一行"这个假设藏进返回形状里，
   将来谁按 `jobs[0]` 读数就会悄悄丢掉另一半。

还有一条是这一片特有的：**内部进程标识不给普通读者**。`claimed_by` 是本机
`主机名:进程号:随机尾`（P5-3 立的口径，与撤 `report_path`、把别人的 `user_id` 换成 `is_mine`
同族），所以它是 admin 才拿得到的一格，而且响应里明写这一格的可见范围，不让读者猜为什么是 null。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import execute, init_db, query_one
from app.main import app
from app.security import hash_password

CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n2026-09-05T01:03:03Z,admin,10.0.0.8,ok\n"


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


def _submit(client: TestClient, dataset_id: int, trace: str) -> dict:
    response = client.post(
        "/api/analyze",
        json={"question": "失败登录次数最高的用户是谁？", "dataset_id": dataset_id, "mode": "mock"},
        headers={"X-Trace-Id": trace},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait_terminal(client: TestClient, job_id: str, within_s: float = 60.0) -> dict:
    """轮询到终态。**挂住不算抓到**：超时就把最后一份读数交出来红在断言上。"""
    deadline = time.monotonic() + within_s
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body.get("terminal"):
            return body
        time.sleep(0.4)
    raise AssertionError(f"{within_s}s 内没落到终态，最后一次读数：{body}")


def _peer(username: str, org_slug: str | None) -> None:
    """造一个读者。给 org 就挂成员关系（同企业 ⇒ 看得见彼此跑过什么），不给就是 org 0。"""
    execute("DELETE FROM users WHERE username = ?", (username,))
    user_id = execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password("peer-pass-1")),
    )
    if org_slug is None:
        return
    execute("DELETE FROM organizations WHERE slug = ?", (org_slug,))
    org_id = execute("INSERT INTO organizations (slug, name) VALUES (?, ?)", (org_slug, org_slug.upper()))
    for member_id in [int(user_id), int(_admin_id())]:
        execute(
            "INSERT OR REPLACE INTO memberships (user_id, org_id, role) VALUES (?, ?, ?)",
            (member_id, int(org_id), "member"),
        )


def _admin_id() -> int:
    return int(query_one("SELECT id FROM users WHERE username = 'admin'")["id"])


# ---------------------------------------------------------------- 判据那五格


def test_the_lookup_answers_the_five_questions_the_criterion_asks(env):
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        submitted = _submit(client, dataset_id, "lookup-full-1")
        _wait_terminal(client, submitted["job_id"])

        found = client.get("/api/trace/lookup-full-1")
        assert found.status_code == 200, found.text
        body = found.json()
        assert body["count"] == 1 and len(body["jobs"]) == 1, body
        entry = body["jobs"][0]

        # ① 谁提交的（自己的那一行不给别人的 user_id 当身份，给 is_mine + 经手人名）
        submission = entry["submission"]
        assert submission["is_mine"] is True and submission["actor_username"] == "admin", submission
        # ② 排了多久：值 + 算法都在读数里，不让读者猜是按哪两列减出来的
        timing = entry["timing"]
        assert timing["queue_wait_basis"].startswith("jobs.created_at"), timing
        assert isinstance(timing["queue_wait_seconds"], (int, float)), timing
        assert timing["queue_wait_seconds"] >= 0
        # 排队 ≤ 总时长。这条不是装饰：`first_event_at` 的取法从"最早一条"改成"最晚一条"时，
        # 数值照样 >=0、照样是个像样的秒数，只有跟总时长一比才露馅——**位点要有牙，
        # 就得让读数的相对关系也被钉住**，不然量出来的永远是"看起来合理"。
        assert timing["queue_wait_seconds"] <= timing["total_seconds"], timing
        # 端点也要钉住：这一格取的就是库里 `seq` 最小的那条事件的时间。
        # 这里不是再算一遍算法（那会抄一份判据），而是**取被比较的那个值**——
        # 有了它，"ORDER BY seq ASC" 被改成 DESC 就当场红。
        first_row = query_one(
            "SELECT created_at FROM job_events WHERE job_id = ? ORDER BY seq ASC LIMIT 1",
            (submitted["job_id"],),
        )
        assert first_row, "这条作业一条事件都没落库，前面那条排队时段的断言就是空的"
        assert timing["first_event_at"] == first_row["created_at"], (timing, first_row)
        # ③ 谁认领的（默认 admin 读者拿得到留痕列）④ 几次重试
        execution = entry["execution"]
        assert execution["claimed"] is True and execution["attempts"] == 0, execution
        assert execution["claimed_by"] and execution["claimed_by_scope"] == "admin_only", execution
        # ⑤ 上游返回了什么形状：闸门等待 + 产物侧的调用数
        upstream = entry["upstream"]
        assert upstream["gate_waits"] >= 0 and upstream["artifact"]["status"] == "present", upstream
        assert upstream["artifact"]["llm_calls"], upstream["artifact"]
        # 复用而不是重写：作业详情那一格与 `/api/jobs` 给的是同一份算法。
        # 这里**不挑终态是哪个**（单行样品的 mock 运行落在 partial 是正常的），挑的是"两处读数一致"——
        # 这一格的意义就在没有第二份实现，而不是这次跑得比上次好。
        detail = client.get(f"/api/jobs/{submitted['job_id']}").json()
        assert entry["detail"]["status"] == detail["status"], (entry["detail"], detail)
        assert entry["detail"]["terminal"] is True, entry["detail"]
        assert entry["detail"]["dispatch"] == detail["dispatch"]


def test_a_queued_job_says_the_wait_was_not_measured_rather_than_zero(env, monkeypatch):
    """没人认领的时候 `queue_wait_seconds` 必须是 null。

    写成 0.0 的表现不是"缺一个数"，而是"排得真快"——那是把一个合法数值当成"没测到"的替身，
    本项目已经为这一族付过四次账（`None 不许读成 pass`、"没采到 ≠ 0"、"没跑成不许印成没问题"）。
    """
    monkeypatch.setenv("WEB_DISPATCH", "off")  # 这个进程不认领，也不起外部 worker
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        submitted = _submit(client, dataset_id, "lookup-queued-1")

        found = client.get("/api/trace/lookup-queued-1").json()
        entry = found["jobs"][0]
        assert entry["execution"]["claimed"] is False, entry["execution"]
        assert entry["timing"]["queue_wait_seconds"] is None, entry["timing"]
        assert entry["timing"]["first_event_at"] is None, entry["timing"]
        # 上游那一格也得说清"为什么没有"，而不是留一个空 dict 让人自己猜
        assert entry["upstream"]["artifact"]["status"] == "no_run_id", entry["upstream"]


def test_one_trace_can_carry_more_than_one_job_row(env):
    """入站那个串是客户端自带的：同一个人复用同一个 id 不带幂等键，就是两行。

    交回列表而不是"那一条"——把"一次提交 = 一行"这个假设藏进返回形状里，
    将来谁按第一条读数，就会悄悄丢掉另一半。
    """
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        first = _submit(client, dataset_id, "lookup-two-rows")
        second = _submit(client, dataset_id, "lookup-two-rows")
        assert first["job_id"] != second["job_id"], (first, second)  # 没带键就是没有护栏
        for job in (first, second):
            _wait_terminal(client, job["job_id"])

        found = client.get("/api/trace/lookup-two-rows").json()
        assert found["count"] == 2, found
        assert {row["detail"]["job_id"] for row in found["jobs"]} == {first["job_id"], second["job_id"]}


# ---------------------------------------------------------------- 边界那三格


def test_another_users_trace_reads_as_absent_not_as_not_mine(env):
    """别人的链路查回来是 404，与"这条链路不存在"**同一个形状**。

    区分二者等于给一个枚举入口（作业详情那条路早就是这个口径）。
    而且必须带正向对照——同一条链路在提交者眼里查得到，否则这两个 404 可能只是在测
    "这条路由本来就是坏的"（前置条件是借来的那一族）。
    """
    _peer("lookup_stranger", org_slug=None)
    with TestClient(app) as owner:
        _login(owner)
        dataset_id = _dataset(owner)
        submitted = _submit(owner, dataset_id, "lookup-private-1")
        _wait_terminal(owner, submitted["job_id"])

    with TestClient(app) as stranger:
        _login(stranger, username="lookup_stranger", password="peer-pass-1")
        refused = stranger.get("/api/trace/lookup-private-1")
        assert refused.status_code == 404, refused.text
        # **这一句才是"归属判据落在列表查询上"的断言**，位点 PT1 就是靠它现形的：
        # 把查询里的 `access.scope` 谓词摘掉，逐行读数里那句 `_visible_job` 还会再拦一次，
        # 两边都是 404 ⇒ 光看状态码这条位点是**等价程序**。差别在来源——
        # "这条链路查不到"（列表那一步就拦住了）与"任务不存在"（把别人的行取出来之后才丢）
        # 不是一回事：前者是这条读路径的守卫，后者是顺带兜住的，谁兜谁将来一改就会翻。
        assert "链路" in refused.json()["detail"], refused.json()
        # 形状不对与查不到是两种红：422 是"你给的串不成话"，404 是"没有这条链路"
        also = stranger.get("/api/trace/lookup-does-not-exist")
        assert also.status_code == 404, also.text
        assert "链路" in also.json()["detail"], also.json()

    with TestClient(app) as back:
        _login(back)
        assert back.get("/api/trace/lookup-private-1").status_code == 200


def test_the_claimer_is_for_operators_only(env):
    """同企业的同事看得见这次运行，但拿不到本机 `主机名:进程号:尾`。

    这一格是运维的（P6 的判据要能答"谁认领的"），不是读者的：把内部进程标识交给普通读者，
    与当初撤掉 `report_path`、把别人的 `user_id` 换成 `is_mine` 是同一件事。
    可见性不靠"少给一条读数"实现——那一行的其余读数照给，只撤这一格，并且**明写为什么是 null**。
    """
    _peer("lookup_teammate", org_slug="lookup-org")
    with TestClient(app) as admin:
        _login(admin)
        dataset_id = _dataset(admin)
        submitted = _submit(admin, dataset_id, "lookup-claimer-1")
        _wait_terminal(admin, submitted["job_id"])

    with TestClient(app) as teammate:
        _login(teammate, username="lookup_teammate", password="peer-pass-1")
        found = teammate.get("/api/trace/lookup-claimer-1")
        assert found.status_code == 200, found.text  # 同企业共享：这行他看得见
        entry = found.json()["jobs"][0]
        assert entry["execution"]["claimed"] is True, entry["execution"]
        assert entry["execution"]["claimed_by"] is None, entry["execution"]
        assert entry["submission"]["is_mine"] is False, entry["submission"]


def test_a_malformed_trace_is_rejected_before_the_query(env):
    """形状不对就 422，不进查询：这一格不是自由文本。

    带空格/控制字符的串走 LIKE 或走日志都会带来一类没必要的问题；
    而"你给的串不成话"与"没有这条链路"必须是两种红，否则调不出是拼错了还是查不到。
    """
    with TestClient(app) as client:
        _login(client)
        for bad in ["has space", "a" * 33, ""]:
            response = client.get(f"/api/trace/{bad}")
            assert response.status_code in (404, 422), (bad, response.status_code, response.text)
        rejected = client.get("/api/trace/lookup%20with%20space")
        assert rejected.status_code == 422, rejected.text
        assert "形状不对" in rejected.json()["detail"], rejected.json()


def test_the_upstream_cell_says_which_mode_it_describes(env):
    """mock 模式那两格描述的是本地假客户端，不是真上游——读数不许冒充自己没测过的东西。"""
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        submitted = _submit(client, dataset_id, "lookup-mode-1")
        _wait_terminal(client, submitted["job_id"])
        upstream = client.get("/api/trace/lookup-mode-1").json()["jobs"][0]["upstream"]
    assert upstream["mode"] == "mock", upstream
    # 0 在这里是**量出来的 0**：mock 不打上游，所以一次闸门等待都没发生
    assert upstream["gate_waits"] == 0, upstream
    assert upstream["max_gate_wait_ms"] is None, upstream
# ---------------------------------------------------------------- 尺子那一侧


class _Response:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


class _Probe:
    """假客户端：只记"被请求了哪个 URL、带了谁的身份"，别的一律不管。"""

    def __init__(self, body: dict, status_code: int = 200) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._body = body
        self._status = status_code

    async def get(self, url: str, headers: dict | None = None):
        self.calls.append((url, headers or {}))
        return _Response(self._status, self._body)


def _seed_db(path, rows: list[tuple[str, str | None]]) -> None:
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, trace_id TEXT)")
    for job_id, trace in rows:
        conn.execute("INSERT INTO jobs (job_id, trace_id) VALUES (?, ?)", (job_id, trace))
    conn.commit()
    conn.close()


def test_the_harness_reads_the_chain_back_over_http(tmp_path, monkeypatch):
    """压测尺子那一格是**真 HTTP 上的退出判据**，所以它自己也要有守卫。

    这里不跑整轮压测（那要起服务、跑几十个作业），而是把取样函数单独 drive 一次：
    临时库里放一条带链路标识的作业行，喂一个假客户端，断言它请求的就是那一条链路，
    并且五格的读数各自落成了什么。尺子的读数落不进 json，等于下一轮没人能复核。
    """
    import asyncio
    from scripts.load_test import trace_lookup

    db = tmp_path / "load.db"
    _seed_db(db, [("job_live", "harness-chain-1")])
    monkeypatch.setenv("LOAD_DB", str(db))
    body = {
        "trace_id": "harness-chain-1",
        "count": 1,
        "jobs": [
            {
                "submission": {"actor_username": "load_u1"},
                "timing": {"queue_wait_seconds": 1.25},
                "execution": {"attempts": 0, "claimed_by": "host:1234:ab12cd"},
                "upstream": {"artifact": {"status": "present"}},
            }
        ],
    }
    probe = _Probe(body)
    reading = asyncio.run(trace_lookup(probe, "http://127.0.0.1:1", {"job_id": "job_live", "token": "tok"}))

    assert probe.calls and probe.calls[0][0].endswith("/api/trace/harness-chain-1"), probe.calls
    assert probe.calls[0][1]["Authorization"] == "Bearer tok", "取样必须带那个读者的身份，不然测的是匿名行为"
    assert reading["status_code"] == 200 and reading["rows_returned"] == 1, reading
    assert reading["who_submitted"] is True and reading["queue_wait_seconds"] == 1.25, reading
    assert reading["attempts"] == 0 and reading["upstream_status"] == "present", reading
    assert reading["claimer_field_present"] is True, reading


def test_the_harness_says_which_half_of_the_missing_it_is(tmp_path, monkeypatch):
    """查不到时分两种：**这条作业行上没有链路标识**（那是缺陷）与**这一轮没取样**（那是形状）。

    合成一句"没查到"就会把 P6-1 之前建的老行、以及"根本没带键的裸客户端形状"读成同一件事——
    与"0 有两种读法"同族。
    """
    import asyncio
    from scripts.load_test import trace_lookup

    db = tmp_path / "load2.db"
    _seed_db(db, [("job_old", None)])
    monkeypatch.setenv("LOAD_DB", str(db))

    no_trace = asyncio.run(trace_lookup(_Probe({}), "http://x", {"job_id": "job_old", "token": "t"}))
    assert no_trace["status_code"] is None and "没有链路标识" in no_trace["reason"], no_trace

    no_sample = asyncio.run(trace_lookup(_Probe({}), "http://x", {}))
    assert no_sample["skipped"] is True, no_sample


def test_the_wave_record_carries_the_trace_readout():
    """这一格要落进报告：不落盘的读数与"没测"在下一轮没法区分。"""
    source = (Path(__file__).resolve().parents[1] / "scripts" / "load_test.py").read_text(encoding="utf-8")
    assert '"trace_lookup": trace_read,' in source, "取样结果不进 json，下一轮就没人复核得了"
    assert "await trace_lookup(probe_client" in source, "取样必须走那一轮真 HTTP 的同一个客户端"
