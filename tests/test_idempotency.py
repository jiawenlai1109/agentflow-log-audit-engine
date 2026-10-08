"""P5-1：幂等键。判据只有一条——同一个用户带同一个键，只有第一次真的建出作业行。

为什么这条必须在**库层**：受理层拆分（P5）之后客户端重试是设计的一部分，
而"先查一下有没有、没有再插"在两个并发重试面前一定错——两边同时读到"没有"，然后各插一行，
表现是同一次提问跑出两份报告、上游被多打一倍。所以下面专门有一条**真并发**用例，
不是顺序提交两次（顺序那种测不出竞态，P2 的认领用例已经为这句话付过学费）。

第二条入口（会话续轮）也在这份文件里：只在 `/api/analyze` 上装护栏，
等于"重试不会重复"这条承诺只对一半的请求成立。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import queueing
from app.db import execute, init_db, query_one
from app.main import app
from app.routers import jobs as jobs_route

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


def _analyze(client: TestClient, dataset_id: int, question: str = "失败登录次数最高的用户是谁？", key: str | None = None):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        "/api/analyze", json={"question": question, "dataset_id": dataset_id, "mode": "mock"}, headers=headers
    )


def _rows_with_key(key: str) -> int:
    row = query_one("SELECT COUNT(*) AS n FROM jobs WHERE idempotency_key = ?", (key,))
    return int(row["n"])


# ---------------------------------------------------------------- 接口那条路


def test_the_same_key_twice_creates_exactly_one_job(env):
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        first = _analyze(client, dataset_id, key="idem-single")
        assert first.status_code == 200, first.text
        assert first.json()["idempotency_replayed"] is False, first.json()

        second = _analyze(client, dataset_id, key="idem-single")
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["job_id"] == first.json()["job_id"], "重放必须回到同一条作业"
        assert body["idempotency_replayed"] is True, "重放这一格要如实报，不能伪装成一次新提交"
    assert _rows_with_key("idem-single") == 1


def test_no_key_means_no_guard_rather_than_one_shared_bucket(env):
    """没带键 = 没有护栏。两次相同提问照样建两行——不许把"没带"当成"带了同一个"。"""
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        first = _analyze(client, dataset_id)
        second = _analyze(client, dataset_id)
        assert first.json()["job_id"] != second.json()["job_id"]
        assert second.json()["idempotency_replayed"] is False
    assert _rows_with_key("idem-none") == 0


def test_a_replay_does_not_wake_the_workers(env, monkeypatch):
    """一行没建就不该叫醒认领者：重放也要 notify 的话，客户端重试风暴会直接变成唤醒风暴。"""
    wakeups: list[int] = []
    monkeypatch.setattr(queueing, "notify", lambda: wakeups.append(1))
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        _analyze(client, dataset_id, key="idem-quiet")
        after_first = len(wakeups)
        assert after_first >= 1, "第一次提交连一次唤醒都没有，这条用例就没在测它声称测的东西"
        _analyze(client, dataset_id, key="idem-quiet")
        assert len(wakeups) == after_first, "重放那次不该再叫醒"


def test_the_replayed_row_keeps_the_first_request(env):
    """重放不改写第一次的内容：同一键第二次换了问题文本，赢的仍然是第一次那行。

    这条钉的是"幂等 = 返回原结果"，不是"覆盖成最新的"。后者会让客户端以为自己重发的
    新参数生效了，而库里的作业还跑着老问题——两边对不上时最难查。
    """
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        first = _analyze(client, dataset_id, question="第一版问题", key="idem-keep")
        second = _analyze(client, dataset_id, question="第二版问题", key="idem-keep")
        assert second.json()["job_id"] == first.json()["job_id"]
    row = query_one("SELECT question FROM jobs WHERE job_id = ?", (first.json()["job_id"],))
    assert row["question"] == "第一版问题"


def test_second_entry_point_honours_the_same_key(env):
    """会话续轮是第二个创建作业的入口，它必须说同一种话。"""
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        created = client.post("/api/sessions", json={"title": "幂等续轮", "dataset_id": dataset_id})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        body = {"question": "失败登录集中在哪个小时？", "mode": "mock"}
        first = client.post(f"/api/sessions/{session_id}/messages", json=body, headers={"Idempotency-Key": "idem-turn"})
        second = client.post(f"/api/sessions/{session_id}/messages", json=body, headers={"Idempotency-Key": "idem-turn"})
        assert first.status_code == 200, first.text
        assert first.json()["idempotency_replayed"] is False
        assert second.json()["job_id"] == first.json()["job_id"]
        assert second.json()["idempotency_replayed"] is True
    assert _rows_with_key("idem-turn") == 1


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("", "空值不当成「没带」，客户端以为有护栏而实际没有"),
        ("   ", "纯空白同上"),
        ("has space", "键里不许有空格：两个键被洗成一个是静默改语义"),
        ("x" * 201, "超过列宽必须当场报，而不是等 INSERT 撞墙或悄悄截断"),
    ],
)
def test_a_bad_key_is_rejected_loudly(env, raw, why):
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        response = _analyze(client, dataset_id, key=raw)
        assert response.status_code == 422, (raw, response.text)
        assert "Idempotency-Key" in response.json()["detail"], response.text
    assert query_one("SELECT COUNT(*) AS n FROM jobs")["n"] == 0, f"被形状拦下的请求不该留下作业行：{why}"


def test_non_ascii_and_control_characters_are_rejected_by_the_shape():
    """这两类只能在函数层测——**因为 httpx 根本不让你发出去**（头部值必须是 ASCII）。

    这不是退让，是把测试放在正确的层：传输层拦住的形状，服务端那条规则仍然要有用例，
    因为不是所有客户端都走 httpx（curl、网关转发、别的语言）。判据只有一份，
    就是 `parse_idempotency_key`，所以这一条直接打那一份。
    """
    from fastapi import HTTPException

    for raw in ("带汉字", "has\ttab", "has\nnewline"):
        with pytest.raises(HTTPException) as refused:
            jobs_route.parse_idempotency_key(raw)
        assert refused.value.status_code == 422, raw
    # 合法形状：UUID、带冒号前缀的、纯数字串都原样通过，且不 strip、不归一化
    for raw in ("5b8c1d2e-3f4a-4b5c-9d8e-7f6a5b4c3d2e", "retry:job-17", "42"):
        assert jobs_route.parse_idempotency_key(raw) == raw
    # 尾部空格是"形状不对"，不是"把它剪掉再用"：剪一下就可能把两个不同的键洗成同一个，
    # 表现是"我明明提交了两次，第二次没有任何反应"
    with pytest.raises(HTTPException):
        jobs_route.parse_idempotency_key("padded ")


# ---------------------------------------------------------------- 判据本身（库层与并发）


def test_the_unique_index_is_what_rejects_the_duplicate(env):
    """约束真的在库里：绕过应用直接插第二行，必须撞 IntegrityError。

    这条是"判据在库层不在应用层"的证据。如果哪天有人把幂等改成"应用里先查再插"，
    这条会红——因为它测的不是行为而是**约束在场**。
    """
    import sqlite3

    uid = query_one("SELECT id FROM users WHERE username = 'admin'")["id"]
    execute(
        "INSERT INTO jobs (job_id, user_id, org_id, question, mode, status, idempotency_key) "
        "VALUES ('job_idem_direct_1', ?, 0, '直插', 'mock', 'queued', 'direct-key')",
        (uid,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        execute(
            "INSERT INTO jobs (job_id, user_id, org_id, question, mode, status, idempotency_key) "
            "VALUES ('job_idem_direct_2', ?, 0, '直插', 'mock', 'queued', 'direct-key')",
            (uid,),
        )


def test_null_keys_never_collide(env):
    """没带键的行（NULL）互不冲突：SQLite 与 Postgres 的唯一索引都把 NULL 当互不相同。"""
    uid = query_one("SELECT id FROM users WHERE username = 'admin'")["id"]
    for index in range(3):
        execute(
            "INSERT INTO jobs (job_id, user_id, org_id, question, mode, status) "
            f"VALUES ('job_idem_null_{index}', ?, 0, '没带键', 'mock', 'queued')",
            (uid,),
        )
    assert query_one("SELECT COUNT(*) AS n FROM jobs WHERE idempotency_key IS NULL")["n"] == 3


def test_another_user_with_the_same_key_gets_their_own_job(env):
    """键的作用域是用户：同事用同一个键不该撞到你的作业上。

    反过来说就是安全事故——全局唯一 + 回读不带归属谓词的话，猜到别人的键就能拿到别人的
    `job_id`，接下来就能去读那条作业的状态与报告。
    """
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/users", json={"username": "idem_peer", "password": "long-enough-123"})
        assert created.status_code == 200, created.text
        dataset_id = _dataset(client)
        mine = _analyze(client, dataset_id, key="shared-looking-key")

    with TestClient(app) as peer:
        _login(peer, "idem_peer", "long-enough-123")
        peer_dataset = _dataset(peer)
        theirs = _analyze(peer, peer_dataset, key="shared-looking-key")
        assert theirs.status_code == 200, theirs.text
        assert theirs.json()["idempotency_replayed"] is False, "别人的键不该让这个人被当成重放"
    assert mine.json()["job_id"] != theirs.json()["job_id"]
    assert _rows_with_key("shared-looking-key") == 2


def test_same_org_teammates_do_not_share_a_key(env):
    """同企业的两个同事带同一个键，也必须是两条作业。

    这条比上一条更贴近真事：共享面已经铺到数据集与报告上了，所以"回读时用什么判据"
    只要松一格（用 SHARED 而不是 OWNER），同事之间就会互相撞键——第二个人的提交会
    安安静静返回第一个人的 job_id，而他看到的是"我的作业在跑"，跑的是别人的问题。

    两人都**用自己上传的数据集**，并且断言是无条件的。上一版只给 A 备了数据集、
    还写了 `if second.status_code == 200:`，于是 B 的请求可能先被别的闸门拒掉——
    "撞没撞键"根本没发生，用例却静默通过。那是量具空转，不是判据通过（PI6 第一次
    就是这个原因漏抓的）。
    """
    with TestClient(app) as client:
        _login(client)
        execute("INSERT INTO organizations (slug, name) VALUES ('idem-org', 'Idem Org')")
        for name in ("idem_a", "idem_b"):
            created = client.post(
                "/api/users", json={"username": name, "password": "long-enough-123", "org": "idem-org"}
            )
            assert created.status_code == 200, created.text

    with TestClient(app) as a:
        _login(a, "idem_a", "long-enough-123")
        first = _analyze(a, _dataset(a), key="teammate-key")
        assert first.status_code == 200, first.text

    with TestClient(app) as b:
        _login(b, "idem_b", "long-enough-123")
        second = _analyze(b, _dataset(b), key="teammate-key")
        assert second.status_code == 200, second.text

    assert second.json()["job_id"] != first.json()["job_id"], "同事之间撞键了：B 拿到了 A 的作业"
    assert second.json()["idempotency_replayed"] is False, second.json()
    assert _rows_with_key("teammate-key") == 2


def test_concurrent_retries_insert_exactly_one_row(env):
    """八个线程同时带同一个键提交：只能有一行，且所有人都拿到同一个 job_id。

    这条就是"先查再插"挡不住的那个形状。附带一条自证：八个线程没有真的并发撞过，
    用例就报红——否则它会因为"顺序执行也不会错"而长期全绿（P2 认领那组用例的同一课）。
    """
    uid = query_one("SELECT id FROM users WHERE username = 'admin'")["id"]
    results: list[str] = []
    collisions: list[bool] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def submit(index: int) -> None:
        barrier.wait()
        outcome = queueing.accept(
            job_id=f"job_idem_race_{index}",
            user_id=uid,
            org_id=0,
            question="并发重试",
            mode="mock",
            session_id=None,
            pack=None,
            spec={"question": "并发重试"},
            idempotency_key="race-key",
        )
        with lock:
            results.append(outcome["job_id"])
            collisions.append(bool(outcome["replayed"]))

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 8, results
    assert len(set(results)) == 1, f"八个线程拿到了不同的 job_id：{set(results)}"
    assert sum(1 for item in collisions if item) == 7, f"只该有一次是真提交：{collisions}"
    assert _rows_with_key("race-key") == 1
    # 自证：并发真发生过（至少一个线程的插入被库层挡下来过），否则这条用例是空跑的
    assert any(collisions), "八个线程都没撞上唯一约束——并发没真的发生，这条用例要修"


def test_the_payload_says_whether_the_job_is_finished(env):
    """作业详情要自带"到终态了没有"这一格，前端不许自己抄一份终态名单。

    这是 P5-2 那条重连逻辑的依赖：客户端判断"还要不要接回去"读的就是它。
    抄一份名单的下场是两份名单分叉——后端多出一个终态（比如 `cancelled`）时，
    前端会一直重连一条早就结束的作业。
    """
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        queued = _analyze(client, dataset_id, key="idem-terminal").json()
        assert queued["terminal"] is False, queued
        job_id = queued["job_id"]

        execute("UPDATE jobs SET status = 'success' WHERE job_id = ?", (job_id,))
        done = client.get(f"/api/jobs/{job_id}").json()
        assert done["terminal"] is True, done

        # `cancelled` 也是终态：它曾经漏在硬编码名单外面一次，所以点名下
        execute("UPDATE jobs SET status = 'cancelled' WHERE job_id = ?", (job_id,))
        assert client.get(f"/api/jobs/{job_id}").json()["terminal"] is True


def test_the_replay_lookup_is_pinned_to_the_owner_predicate_not_to_row_order():
    """回读那一条查询必须传 `OWNER`，这一条按**判据形状**守，不靠行为差异。

    为什么这条要单独存在：把 `scope_by_id(user_id, OWNER)` 换成 SHARED，行为层测不出来——
    两条作业都满足 SHARED 时，`query_one` 先返回哪一行取决于 SQLite 的查询计划（走哪个索引、
    按什么序），不是可复现的差别。位点 PI6 第一次就是因为这个而全绿。
    "同事撞键"那条行为用例仍然要留（它守的是我们承诺的结果），但它不是这条位点的观测点。
    """
    import ast

    source = (
        Path(__file__).resolve().parents[1] / "app" / "queueing.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    accept = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "accept"
    )
    calls = [
        node
        for node in ast.walk(accept)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "scope_by_id"
    ]
    assert len(calls) == 1, f"回读应当只有一处归属判据，实际 {len(calls)} 处"
    args = calls[0].args
    assert len(args) == 2, "第二个参数必须是显式的归属模式（默认值是 SHARED，漏传就等于放宽）"
    mode = args[1]
    assert isinstance(mode, ast.Name) and mode.id == "OWNER", (
        "回读用了 SHARED 或字面量：那等于允许同事的键回读到别人的作业，"
        "而这个改动在行为读数上是随机的（哪一行先返回由查询计划决定），只能靠形状守住"
    )


def test_the_idempotency_judgement_has_exactly_one_home():
    """"谁是真的"这条判据只许住在 `queueing.accept` 里。

    别处再写一份 `ON CONFLICT … idempotency_key`，两个入口就会给出两种答案——与"归属判据
    只有一份"是同一个立场。按 AST 扫字符串字面量而不是 grep 整个文件：注释里出现这个词是
    常态（这片到处都是），语句里出现才算分叉。
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if py.name in {"db.py", "models.py"}:
            continue  # 那两处是建表与建索引（schema），不是判据
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if "ON CONFLICT" in text.upper() and "idempotency_key" in text and py.name != "queueing.py":
                    offenders.append(f"{py.name}:{node.lineno}")
    assert not offenders, "幂等判据出现了第二份实现：\n" + "\n".join(offenders)
