"""鉴权与数据隔离回归测试。

这一组用例是"第 0 点地基"的验收判据：无 token 进不来、跨用户拿不到、
token 会过期、API token 与媒体 token 作用域互斥、run_id 与产物路径不能逃逸。
"""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import execute, query_one
from app.main import app
from app.security import (
    SCOPE_API,
    SCOPE_MEDIA,
    decode_token,
    hash_password,
    make_token,
    verify_password,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
VALID_RUN = "run_20260101_000000_aaaaaaaa"


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def _create_user(username: str, password: str, role: str = "user") -> int:
    """本地元数据库跨测试运行会残留，写入一律幂等（库不是 tmp_path，属既有测试风格）。"""
    execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        (username, hash_password(password), role),
    )
    return query_one("SELECT id FROM users WHERE username = ?", (username,))["id"]


def _seed_dataset(user_id: int) -> int:
    """直接落一行数据集记录（不跑真实上传，只为拿到属于该用户的 dataset_id）。"""
    path = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"
    return execute(
        "INSERT INTO datasets (user_id, filename, path, size, row_count, columns) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, path.name, str(path), path.stat().st_size, 2000, '["销售额"]'),
    )


def _create_job(user_id: int, run_id: str = VALID_RUN, question: str = "q") -> str:
    job_id = f"job_test_{run_id[-4:]}_{user_id}"
    execute(
        "INSERT OR REPLACE INTO jobs (job_id, user_id, question, mode, run_id, status, progress) "
        "VALUES (?, ?, ?, 'mock', ?, 'success', 100)",
        (job_id, user_id, question, run_id),
    )
    return job_id


@pytest.fixture()
def users():
    """两个普通用户 + 各自的 job 记录（owner 与 intruder 用来验归属）。"""
    owner = _create_user("owner", "owner-pw-1")
    intruder = _create_user("intruder", "intruder-pw-1")
    return {"owner": owner, "intruder": intruder}


@pytest.fixture()
def run_dir(tmp_path, monkeypatch):
    """伪造一次 run 的产物目录，并把 media/reports 的 OUTPUTS_ROOT 指过来。"""
    artifacts = tmp_path / VALID_RUN / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "chart_task_1.png").write_bytes(TINY_PNG)
    (tmp_path / "secret.txt").write_text("do-not-read", encoding="utf-8")
    report = (
        "# 报告\n\n关键指标 100\n\n![图](./artifacts/chart_task_1.png)\n\n"
        f"绝对路径引用 ![](D:\\somewhere\\outputs\\{VALID_RUN}\\artifacts\\chart_task_1.png)\n"
    )
    (tmp_path / VALID_RUN / "report.md").write_text(report, encoding="utf-8")
    (tmp_path / VALID_RUN / "evaluation.json").write_text(
        '{"run_id": "%s", "status": "success", "question": "q", "llm_calls": 3}' % VALID_RUN,
        encoding="utf-8",
    )
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- 401 覆盖面


PROTECTED = [
    ("get", "/api/datasets"),
    ("post", "/api/datasets"),
    ("delete", "/api/datasets/1"),
    ("post", "/api/analyze"),
    ("get", "/api/packs"),
    ("get", "/api/queue"),
    ("get", "/api/jobs/job_nope"),
    ("get", "/api/jobs/job_nope/events"),
    ("get", "/api/trace/any-chain-1"),
    ("get", "/api/sessions"),
    ("post", "/api/sessions"),
    ("get", "/api/sessions/session_nope/messages"),
    ("post", "/api/sessions/session_nope/messages"),
    ("delete", "/api/sessions/session_nope"),
    ("get", "/api/runs"),
    ("get", f"/api/reports/{VALID_RUN}"),
    ("get", "/api/evaluations/summary"),
    ("get", "/api/auth/me"),
]


@pytest.mark.parametrize("method,url", PROTECTED)
def test_protected_endpoints_reject_anonymous(method, url):
    with TestClient(app) as client:
        response = client.request(method, url)
        assert response.status_code == 401, f"{method.upper()} {url} 竟然放行了匿名请求"


def test_health_stays_public():
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


def test_bad_credentials_rejected(users):
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login", json={"username": "owner", "password": "wrong"}
        ).status_code == 401
        # 不存在的用户与错密码返回同一句话，不给用户名枚举留差异
        missing = client.post(
            "/api/auth/login", json={"username": "nobody-here", "password": "wrong"}
        )
        assert missing.status_code == 401
        assert missing.json()["detail"] == "用户名或密码错误"


def test_me_returns_identity(users):
    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        # P3 之后 /me 多带一个 `orgs`：建号时指定企业之后，前端与运维要能一眼看出
        # 这个人到底有没有归属（空列表 = 未归属，共享读对他默认拒绝）。
        # 这条断言写死整个形状而不是只查 username，是为了让"接口加了字段"这件事留在测试里，
        # 而不是变成一次静默的接口变更。
        assert me.json() == {"username": "owner", "role": "user", "orgs": []}


# ---------------------------------------------------------------- token 机制


def test_login_returns_expiry(users):
    with TestClient(app) as client:
        body = client.post(
            "/api/auth/login", json={"username": "owner", "password": "owner-pw-1"}
        ).json()
        assert body["expires_in"] > 0
        payload = decode_token(body["token"], expected_scope=SCOPE_API)
        assert payload and payload["sub"] == "owner" and payload["exp"] > payload["iat"]


def test_expired_and_tampered_tokens_rejected(users):
    expired = make_token("owner", scope=SCOPE_API, ttl=-1)
    assert decode_token(expired, expected_scope=SCOPE_API) is None
    valid = make_token("owner", scope=SCOPE_API)
    body, signature = valid.split(".")
    assert decode_token(f"{body}.{signature[:-2]}aa", expected_scope=SCOPE_API) is None
    assert decode_token("not-a-token", expected_scope=SCOPE_API) is None
    with TestClient(app) as client:
        assert client.get("/api/runs", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_api_and_media_scopes_are_mutually_exclusive(users):
    """API token 打不开产物目录，媒体 token 也调不动业务接口。"""
    with TestClient(app) as client:
        api_token = _login(client, "owner", "owner-pw-1")
        media = make_token("owner", scope=SCOPE_MEDIA, run_scope=VALID_RUN)

        assert client.get(f"/outputs/{VALID_RUN}/artifacts/chart_task_1.png?t={api_token}").status_code == 401
        assert client.get("/api/runs", headers={"Authorization": f"Bearer {media}"}).status_code == 401


def test_media_token_bound_to_its_run(users, run_dir):
    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        other = make_token("owner", scope=SCOPE_MEDIA, run_scope="run_20250101_000000_deadbeef")
        url = f"/outputs/{VALID_RUN}/artifacts/chart_task_1.png"
        assert client.get(url).status_code == 401
        assert client.get(f"{url}?t={other}").status_code == 403


def test_passwords_are_salted_not_plaintext():
    first, second = hash_password("same-pw"), hash_password("same-pw")
    assert first != second, "相同口令必须得到不同哈希（盐未生效）"
    assert "same-pw" not in first
    assert first.startswith("pbkdf2_sha256$")
    assert verify_password("same-pw", first) and not verify_password("nope", second)


# ---------------------------------------------------------------- 数据隔离


def test_dataset_isolated_between_users(users):
    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        payload = {"file": ("a.csv", b"x,y\n1,2\n", "text/csv")}
        created = client.post("/api/datasets", files=payload)
        assert created.status_code == 200, created.text
        dataset_id = created.json()["id"]
        assert query_one("SELECT user_id FROM datasets WHERE id = ?", (dataset_id,))["user_id"] == users["owner"]

    with TestClient(app) as client:
        _login(client, "intruder", "intruder-pw-1")
        assert dataset_id not in [d["id"] for d in client.get("/api/datasets").json()]
        # 跨用户删除按"不存在"处理，不泄露资源是否存在
        assert client.delete(f"/api/datasets/{dataset_id}").status_code == 404

    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        assert dataset_id in [d["id"] for d in client.get("/api/datasets").json()]
        assert client.delete(f"/api/datasets/{dataset_id}").status_code == 200


def test_job_session_and_report_isolated(users, run_dir):
    job_id = _create_job(users["owner"])
    execute(
        "INSERT OR IGNORE INTO sessions (session_id, user_id, title) VALUES (?, ?, 'owner-session')",
        ("session_owner_1", users["owner"]),
    )
    with TestClient(app) as client:
        _login(client, "intruder", "intruder-pw-1")
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert client.get(f"/api/jobs/{job_id}/events").status_code == 404
        assert client.get("/api/sessions/session_owner_1/messages").status_code == 404
        assert client.get(f"/api/reports/{VALID_RUN}").status_code == 404
        assert client.get("/api/runs").json() == []
        assert client.get("/api/evaluations/summary").json()["total"] == 0

    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        assert client.get(f"/api/jobs/{job_id}").status_code == 200
        report = client.get(f"/api/reports/{VALID_RUN}")
        assert report.status_code == 200
        assert f"/outputs/{VALID_RUN}/artifacts/chart_task_1.png?t=" in report.json()["content"]
        assert client.get("/api/runs").json()[0]["run_id"] == VALID_RUN
        assert client.get("/api/evaluations/summary").json()["total"] == 1


def test_report_image_url_is_readable_by_owner(users, run_dir):
    """报告里给的图片链接必须真能取到，且非图片产物不放行。"""
    _create_job(users["owner"])
    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        content = client.get(f"/api/reports/{VALID_RUN}").json()["content"]
        image_url = content.split("![图](")[1].split(")")[0]
        chart = client.get(image_url)
        assert chart.status_code == 200
        assert chart.headers["content-type"] == "image/png"
        # 同目录下的报告与评估文件不能经产物路由直读（只能走带归属校验的 API）
        token = image_url.split("t=")[1]
        assert client.get(f"/outputs/{VALID_RUN}/report.md?t={token}").status_code == 404
        assert client.get(f"/outputs/{VALID_RUN}/evaluation.json?t={token}").status_code == 404


def test_run_id_and_media_paths_cannot_escape(users, run_dir):
    _create_job(users["owner"])
    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        assert client.get("/api/reports/run_x").status_code == 404
        assert client.get("/api/reports/..%2F.env").status_code == 404
        media = make_token("owner", scope=SCOPE_MEDIA, run_scope=VALID_RUN)
        for rel in ("../../secret.txt", "..%2F..%2Fsecret.txt", "artifacts/..%2F..%2Fsecret.txt"):
            response = client.get(f"/outputs/{VALID_RUN}/{rel}?t={media}")
            assert response.status_code == 404, f"路径逃逸未被拦：{rel}"


def test_anonymous_cannot_read_artifact_directory(run_dir):
    with TestClient(app) as client:
        assert client.get(f"/outputs/{VALID_RUN}/artifacts/chart_task_1.png").status_code == 401
        assert client.get(f"/outputs/{VALID_RUN}/report.md").status_code == 401


# ---------------------------------------------------------------- 复检补口（#14 / #15）


def test_mode_is_validated_on_both_submit_paths(users):
    """`mode` 必须是 mock|real。

    pipeline 用 `MockLLM() if mode == "mock" else OpenAILLM(...)` 选客户端，
    未校验的任意字符串都会落到真实（计费）分支——两条提交路径得收紧到同一档。
    """
    dataset_id = _seed_dataset(users["owner"])
    execute(
        "INSERT OR IGNORE INTO sessions (session_id, user_id, title, dataset_path) "
        "VALUES (?, ?, 's', ?)",
        ("session_mode_1", users["owner"], str(PROJECT_ROOT / "demo" / "data" / "retail_sales.csv")),
    )
    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        before = query_one("SELECT COUNT(*) AS n FROM jobs")["n"]
        assert client.post(
            "/api/analyze",
            json={"question": "总销售额是多少？", "dataset_id": dataset_id, "mode": "turbo"},
        ).status_code == 422
        assert client.post(
            "/api/sessions/session_mode_1/messages",
            json={"question": "总销售额是多少？", "mode": "turbo"},
        ).status_code == 422
        assert query_one("SELECT COUNT(*) AS n FROM jobs")["n"] == before, "非法 mode 却产生了任务"


def test_session_response_does_not_leak_server_paths(users):
    dataset_id = _seed_dataset(users["owner"])
    with TestClient(app) as client:
        _login(client, "owner", "owner-pw-1")
        created = client.post("/api/sessions", json={"title": "t", "dataset_id": dataset_id}).json()
        listed = client.get("/api/sessions").json()
    for body in [created, *listed]:
        assert "dataset_path" not in body, "响应外泄服务器绝对路径与存储命名规则"


def test_every_owned_table_query_is_user_scoped():
    """结构性不变量：凡读写 datasets / jobs / sessions 的 SQL 必须自带 user_id 谓词。

    之前这些语句靠"调用方先做一次归属查询"兜住，将来谁删掉守卫就静默打开 IDOR。
    把不变量写进 SQL，才写进代码顺序里。

    队列是一族**故意跨用户**的语句（共享队列的意义就是任何 worker 都能认领任何人的 job），
    它要豁免，但豁免必须是有形状的四条：
      ① 语句字面量自己以 `/*queue-internal*/` 开头；
      ② 只住在 `app/queueing.py` 这一个文件里；
      ③ 条数封上限，多一条要单独决策；
      ④ 归属仍在别处判：读数据源时用行里的 user_id 重校验（runner.resolve_sources），
         面向用户的每条读路径自带谓词（access.scope 拼出来的那条 / SSE / reports）。

    读代码用 AST，不用"正则 + 相邻三行"：后者会被语句自己的形状骗过。第一版豁免就是这么
    失效的——把标记拼到 SQL 字面量开头之后，`"(SELECT|UPDATE|…)` 那条正则**一条都匹配不到**，
    10 条跨用户语句从"需要豁免"变成"不存在"，守卫当场全绿而一条也没看见。
    """
    import ast
    import re

    owned = ("datasets", "jobs", "sessions")
    marker = "/*queue-internal*/"
    queue_sql_home = "queueing.py"
    queue_sql_max = 14  # 2026-10-07 实测 12 条；涨一条要先写清"归属在哪儿判"，才准动这个数
    # 2026-10-08 P4-2 +2：`org_usage` 的两条按企业聚合的用量查询。豁免理由写在函数 docstring 里
    # （配额判的是企业不是个人），而 org_id 的来源仍是 `access.primary_org`——归属判据没有搬家。
    statement_start = re.compile(r"\s*(?:/\*[^*]*\*/\s*)?(SELECT|INSERT|UPDATE|DELETE)\b", re.I)
    offenders: list[str] = []
    queue_internal: list[str] = []
    composed: list[str] = []
    composed_files: set = set()
    for py in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        if py.name == "access.py":
            # 判据的**家**允许出现 `user_id = ?` 这样的字面量——它是唯一那份实现。
            # "只许有一份"由 test_ownership_predicate_has_exactly_one_home 反向钉住。
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        # f-string 里的字面量段会被 ast.walk **单独**走一遍：那些碎片当然不含 user_id，
        # 但判据是从外面拼进来的（{sql}），把它们当独立语句就会把每一条接线都误报成漏判。
        # 所以先记下"属于某个 f-string 的字面量段"，只判整体。
        inside_format = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.Constant):
                        inside_format.add(id(part))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) in inside_format:
                continue
            has_format = False
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):  # f-string：把字面量段拼起来才是完整语句
                text = "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
                has_format = any(isinstance(part, ast.FormattedValue) for part in node.values)
            else:
                continue
            if not statement_start.match(text):
                continue
            if not any(re.search(rf"\b{table}\b", text) for table in owned):
                continue
            if "user_id" in text:
                continue
            if text.lstrip().startswith(marker) and py.name == queue_sql_home:
                queue_internal.append(" ".join(text.split())[:70])
                continue
            if has_format:
                # P3 之后判据不在语句字面量里，而是从 access 拼进来的（`WHERE id = ?{sql}`）。
                # 这条豁免不是放行：它要求**本文件确实引用了 access.scope**，而
                # "每个碰归属表的函数都真的调了它"由下一条守卫按函数逐个查
                # （test_ownership_predicate_has_exactly_one_home）。
                composed.append(f"{py.name}: {' '.join(text.split())[:70]}")
                composed_files.add(py)
                continue
            offenders.append(f"{py.name}: {' '.join(text.split())[:70]}")
    assert not offenders, "这些语句没有把归属写进 SQL：\n" + "\n".join(offenders)
    assert len(queue_internal) <= queue_sql_max, (
        f"跨用户的队列 SQL 从上限 {queue_sql_max} 涨到 {len(queue_internal)} 条：\n" + "\n".join(queue_internal)
    )
    # 豁免要能查回来源：每一条"从 access 拼进来"的语句，所在文件必须真的引用了 access.scope
    for path in sorted(composed_files, key=str):
        body = path.read_text(encoding="utf-8")
        assert "access.scope" in body, f"{path.name} 用 f-string 拼归属却没有引用 access.scope"
    assert composed, "一条从 access 拼进来的语句都没有：P3 的接线被撤了？"


def test_ownership_predicate_has_exactly_one_home():
    """归属判定**只许住在 `app/access.py`**：路由里不许出现第二条实现。

    P3 加企业共享时，"谁能看见哪一行"这条判据如果在 22 个路由里各写一遍，漏掉一处的表现
    不是报错，而是**该被挡住的人看见了**。所以这里钉两件事：
      ① `app/` 里除 access.py 之外，不许出现字面量 `user_id = ?`（那条只在判据的家里）；
      ② 碰归属表的 SELECT/UPDATE/DELETE 语句，所在函数必须引用 `access.scope`/`access.scope_by_id`；
         碰归属表的 INSERT，所在函数必须引用 `access.primary_org`——
         只写 user_id 不写 org_id 的新行会永远落在"未归属"，共享读对它默认拒绝，
         那种"接了线但没人能共享"的形态必须由守卫拦住，而不是靠人记得。
    队列族（语句里带 `/*queue-internal*/`）不在拦截范围：它**故意**跨用户认领，
    归属在读取时由 access 重判（见 app/runner.py 的 resolve_sources）。
    """
    import ast
    import re

    owned = ("datasets", "jobs", "sessions", "bundles", "bundle_files", "bundle_tables")
    statement_start = re.compile(r"\s*(?:/\*[^*]*\*/\s*)?(SELECT|INSERT|UPDATE|DELETE)\b", re.I)
    bare_predicate = []
    unscoped = []
    for py in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        if py.name == "access.py":
            continue
        source = py.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, node) or ""
            if "queue-internal" in body:
                continue
            sql_texts = []
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    sql_texts.append(inner.value)
                elif isinstance(inner, ast.JoinedStr):
                    sql_texts.append(
                        "".join(
                            part.value
                            for part in inner.values
                            if isinstance(part, ast.Constant) and isinstance(part.value, str)
                        )
                    )
            reads = writes = writes_without_org = False
            for text in sql_texts:
                match = statement_start.match(text)
                if not match:
                    continue
                if not any(re.search(rf"\b{table}\b", text) for table in owned):
                    continue
                if "user_id = ?" in text:
                    # 判据被抄进这条语句了（不是从 access 拼进来的）。范围刻意收在碰归属表的
                    # 语句上：查成员关系那种 SELECT org_id FROM memberships WHERE user_id = ?
                    # 不是归属判据，一起拦就是误伤（第一版按整个文件扫，误伤了 auth.py 与 users.py）。
                    bare_predicate.append(f"{py.name}:{node.name}")
                kind = match.group(1).upper()
                if kind == "INSERT":
                    writes = True
                    if not re.search(r"\borg_id\b", text):
                        writes_without_org = True
                else:
                    reads = True
            if reads and "access.scope" not in body:
                unscoped.append(f"{py.name}:{node.name}（读/改路径没走 access.scope）")
            if writes and writes_without_org and "access.primary_org" not in body:
                # 只写 user_id 不写 org_id 的新行会永远落在"未归属"，共享读对它默认拒绝：
                # 那种"接了线但没人能共享"的形态要由守卫拦住，而不是靠人记得
                unscoped.append(f"{py.name}:{node.name}（INSERT 没盖 org 归属）")
    assert not bare_predicate, "这些文件自己写了 `user_id = ?`，判据出现了第二份：\n" + "\n".join(bare_predicate)
    assert not unscoped, "归属判据没走单点：\n" + "\n".join(unscoped)
