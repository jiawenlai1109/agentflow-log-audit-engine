"""P3 的产物布局：一个企业一棵树，而"在哪"只有一处实现。

这里测的是**位置**，不是权限（权限那套在 `tests/test_org_sharing.py`）。两条线各自要证据：

- 共享判据说"报告在企业内可见"，它没说"文件在哪"。文件位置错了的表现不是越权，
  是**读不到自己该读的东西**（`/api/reports` 返回 404 而 run 明明 success），
  或者两个企业挤在同一棵树上（跨企业探测得到别人的目录名）。
- 会话那一条最值钱：引擎与 Web 侧对"会话目录"各有各的推导式时，跑起来一切正常，
  直到产物按企业分树——那时表现是"这个会话的记忆忽然空了"，一路不报错。
  所以下面那条用例既读接口、也看目录，两边必须指到同一个位置。
"""

from __future__ import annotations

import ast
import base64
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config, paths
from app.db import execute, init_db, query_one
from app.main import app
from app.security import SCOPE_MEDIA, hash_password, make_token

CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n2026-09-05T01:03:03Z,admin,10.0.0.8,ok\n"
QUESTION = "对2026-09-05的登录日志做安全审计"
LEGACY_RUN = "run_20260101_000000_aaaaaaaa"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 1x1 PNG：媒体路由只放行图片，这条用例要的是"图片真从企业树里读出来"，不是图本身
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _user(username: str, password: str) -> int:
    execute("DELETE FROM users WHERE username = ?", (username,))
    return execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password(password)),
    )


def _org(slug: str) -> int:
    execute("DELETE FROM organizations WHERE slug = ?", (slug,))
    return execute("INSERT INTO organizations (slug, name) VALUES (?, ?)", (slug, slug.upper()))


def _member(user_id: int, org_id: int) -> None:
    execute(
        "INSERT OR REPLACE INTO memberships (user_id, org_id, role) VALUES (?, ?, 'member')",
        (user_id, org_id),
    )


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def _upload(client: TestClient, name: str = "login_auth.csv") -> int:
    response = client.post("/api/datasets", files={"file": (name, CSV.encode("utf-8"), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _seed_dataset(user_id: int, org_id: int, path: Path) -> int:
    """直接落一行数据集，指向**指定的**那个物理文件。

    上传那条接口会给每个数据集起一个新文件名（各自路径不同），而这条用例要的恰好相反：
    两家企业引用同一个路径、同一个 mtime，指纹才会真的撞上。
    """
    return execute(
        "INSERT INTO datasets (user_id, org_id, filename, path, size, row_count, columns) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, org_id, path.name, str(path), path.stat().st_size, 2000, '["action"]'),
    )


def _run_mock_analysis(client: TestClient, dataset_id: int) -> str:
    job = client.post(
        "/api/analyze", json={"question": QUESTION, "dataset_id": dataset_id, "mode": "mock"}
    ).json()
    deadline = time.time() + 90
    while time.time() < deadline:
        current = client.get(f"/api/jobs/{job['job_id']}").json()
        if current["status"] not in ("pending", "queued", "running"):
            assert current["status"] in ("success", "partial", "degraded"), current
            assert current["run_id"], current
            return str(current["run_id"])
        time.sleep(0.5)
    raise TimeoutError(f"job {job} 超时未完成")


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    """产物与库指到临时位置：与生产同一套配置方式（环境变量），不是往模块上贴属性副本。"""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    init_db()
    return tmp_path / "outputs"


# ---------------------------------------------------------------- 位置本身


def test_run_artifacts_land_inside_their_own_org_tree(scratch):
    """一次运行的产物必须在 `outputs/org/<id>/` 底下，根目录不再收裸 run 目录。"""
    alpha = _org("alpha")
    _member(_user("ns_author", "pw-ns-author"), alpha)
    with TestClient(app) as client:
        _login(client, "ns_author", "pw-ns-author")
        dataset_id = _upload(client)
        run_id = _run_mock_analysis(client, dataset_id)
        report = client.get(f"/api/reports/{run_id}")
        assert report.status_code == 200
        # 报告里的图片必须真取不到不了——这一句盯的是媒体路由：它如果还按"扁平位置"拼路径，
        # 企业树里的图就 404，而报告接口本身照样 200（MP9 最初就是这么漏掉的）。
        content = report.json()["content"]
        if "![" in content:
            image_url = content.split("![", 1)[1].split("](", 1)[1].split(")", 1)[0]
            if image_url.startswith("/outputs/"):
                image = client.get(image_url)
                assert image.status_code == 200, f"报告给的图片取不到：{image_url}"
                assert image.headers["content-type"].startswith("image/"), image.headers
    in_tree = scratch / "org" / str(alpha) / run_id
    assert (in_tree / "report.md").is_file(), f"产物没落在企业树里：{in_tree}"
    assert (in_tree / "evaluation.json").is_file()
    # 根下不该再有 run 目录：那正是"全局一棵树"的形状，也是这次要改掉的东西
    assert list(scratch.glob("run_*")) == [], list(scratch.glob("run_*"))


def test_session_tree_follows_the_session_row_not_the_job(scratch):
    """会话状态跟着**会话行**的企业走，运行产物跟着**作业行**的企业走——两条各管各的。

    这条用例是补出来的：MP4（引擎继续自己猜会话位置）最初全绿，因为在常规形状下
    会话行与作业行的 org 是同一个数，两条推导式**算出同一个路径**，改坏了也看不见。
    要让它可观察，就得造出那个真会发生的分歧：会话建在 alpha，之后这人被挪进 beta
    （"建号时指定企业"的形态下，挪人是运维会做的事）。这时续轮的那次运行属于 beta，
    而会话仍然是 alpha 那一行。
    """
    alpha, beta = _org("alpha"), _org("beta")
    uid = _user("moved_user", "pw-moved-user")
    _member(uid, alpha)
    with TestClient(app) as client:
        _login(client, "moved_user", "pw-moved-user")
        dataset_id = _upload(client)
        session_id = client.post(
            "/api/sessions", json={"title": "建在 alpha", "dataset_id": dataset_id}
        ).json()["session_id"]
        # 运维把这个成员挪到 beta：会话行不动（它的历史在 alpha 那棵树下），
        # 从这一刻起新建的资源（作业、数据集）才盖 beta
        execute("DELETE FROM memberships WHERE user_id = ?", (uid,))
        _member(uid, beta)
        job = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"question": QUESTION, "mode": "mock"},
        ).json()
        finished = _wait_once(client, job["job_id"])
        assert finished["status"] in ("success", "partial", "degraded"), finished

        turns = client.get(f"/api/sessions/{session_id}/messages").json()
        assert turns, "会话状态被写到了作业那棵树上，接口读回来是空的（两条推导式分叉）"
        assert turns[0]["run_id"] == finished["run_id"], turns[0]

    # 运行产物在 beta（作业行的归属）
    assert (scratch / "org" / str(beta) / str(finished["run_id"]) / "report.md").is_file()
    # 会话状态在 alpha（会话行的归属），而且 beta 那棵树下没有第二个 sessions 目录把历史分走
    assert (scratch / "org" / str(alpha) / "sessions" / session_id).is_dir()
    assert not (scratch / "org" / str(beta) / "sessions" / session_id).exists(), "同一份会话被写进了两棵树"


def _wait_once(client: TestClient, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = client.get(f"/api/jobs/{job_id}").json()
        if current["status"] not in ("pending", "queued", "running"):
            return current
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} 超时未完成")


def test_two_orgs_on_the_same_source_file_get_two_caches(scratch):
    """同一条源路径 + 同一个 mtime ⇒ 同一个指纹名，但两家企业各得一份，不再叠在同一目录上。

    先纠正我自己写进日志的一处错：`bd_<指纹>` 的指纹是**解析后的源路径 + mtime_ns**
    （`pipeline._bundle_fingerprint`），不是内容哈希——工作日志 2026-10-07/08 两处写成
    "按内容指纹共享"，那条说法站不住（字节相同但路径不同的两批文件，本来就不会共用缓存）。
    真正会撞上的是"同一个文件被两家企业各跑一次"：`path:` 引用、或库里有两条指向同一个
    物理文件的行——那在旧的 `outputs/bundles/` 全局树下就是**同一个目录**，两家企业的
    归一化快照物理混在一起。分树之后同一个名字在各自的树里各有一份。
    """
    alpha, beta = _org("alpha"), _org("beta")
    user_a = _user("ns_a", "pw-ns-a")
    user_b = _user("ns_b", "pw-ns-b")
    _member(user_a, alpha)
    _member(user_b, beta)
    shared = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"
    ids = {
        org_id: _seed_dataset(user_id, org_id, shared)
        for org_id, user_id in ((alpha, user_a), (beta, user_b))
    }
    runs: dict[str, str] = {}
    with TestClient(app) as client:
        _login(client, "ns_a", "pw-ns-a")
        runs["alpha"] = _run_mock_analysis(client, ids[alpha])
        _login(client, "ns_b", "pw-ns-b")
        runs["beta"] = _run_mock_analysis(client, ids[beta])

    caches = {}
    for label, org_id in (("alpha", alpha), ("beta", beta)):
        tree = scratch / "org" / str(org_id)
        assert (tree / runs[label] / "report.md").is_file(), f"{label} 的产物不在自己的树下"
        names = sorted(path.name for path in (tree / "bundles").iterdir())
        assert names and all(name.startswith("bd_") for name in names), names
        caches[label] = set(names)
    # 同一个源文件 ⇒ 同一个指纹名；但它们是两棵树下两个不同路径，不是一份东西
    assert caches["alpha"] == caches["beta"], (caches["alpha"], caches["beta"])
    for label, org_id in (("alpha", alpha), ("beta", beta)):
        name = next(iter(caches[label]))
        assert (scratch / "org" / str(org_id) / "bundles" / name).is_dir()
    assert list(scratch.glob("bundles")) == [], "归一化缓存还留在全局根目录"
    for label, org_id in (("alpha", alpha), ("beta", beta)):
        other = scratch / "org" / str(beta if org_id == alpha else alpha)
        assert runs[label] not in [path.name for path in other.iterdir()]


def test_media_route_serves_from_the_org_tree(scratch):
    """产物路由按企业树取文件：真实 run 的目录里放一张图，必须按报告给的那种地址取到。

    这条是补出来的。MP9（媒体路由退回扁平位置 `outputs/<run_id>/`）**最初全绿**，
    因为 `tests/test_auth.py` 那组用例的产物夹具本来就把文件放在扁平位置——
    量具与可疑实现住在同一个形状里，改坏了自然测不出。所以这里用**真跑出来的 run**
    （产物只会落在企业树里），再往它自己的 `artifacts/` 里放一张图去取。
    图片不是 mock 那次运行的产物（这个题集在 mock 下不出图），是测试放的——
    要测的是"取的位置对不对"，不是"引擎会不会出图"。
    """
    alpha = _org("alpha")
    uid = _user("media_user", "pw-media-user")
    _member(uid, alpha)
    with TestClient(app) as client:
        _login(client, "media_user", "pw-media-user")
        run_id = _run_mock_analysis(client, _upload(client))
        tree = scratch / "org" / str(alpha) / run_id
        (tree / "artifacts").mkdir(parents=True, exist_ok=True)
        (tree / "artifacts" / "chart_probe.png").write_bytes(TINY_PNG)
        assert not (scratch / run_id).exists(), "扁平位置不该同时有一份"

        token = make_token("media_user", scope=SCOPE_MEDIA, run_scope=run_id)
        response = client.get(f"/outputs/{run_id}/artifacts/chart_probe.png?t={token}")
        assert response.status_code == 200, f"企业树里的图片取不到：{response.status_code}"
        assert response.content == TINY_PNG
        # 分树不改变另外两条闸门：非图片不放行、别人拿不到（地址里也没有企业段可猜）
        assert client.get(f"/outputs/{run_id}/report.md?t={token}").status_code == 404
        other = _user("media_outsider", "pw-media-outsider")
        _member(other, _org("beta"))
        _login(client, "media_outsider", "pw-media-outsider")
        stolen = make_token("media_outsider", scope=SCOPE_MEDIA, run_scope=run_id)
        assert client.get(f"/outputs/{run_id}/artifacts/chart_probe.png?t={stolen}").status_code == 404


def test_legacy_flat_artifacts_stay_readable(scratch, monkeypatch):
    """命名空间之前的历史产物仍然能读——回退那条只在可见性判过之后才走。

    这条不是"兼容旧行为"的客套：开发机的 `outputs/` 里就有一批按老布局躺着的 run，
    升级之后它们应当仍能从历史页打开。回退**不新增放行路径**：能不能看见仍然只由
    作业行那条归属谓词决定（判据在 `app/access.py`）。
    """
    alpha = _org("alpha")
    uid = _user("legacy_owner", "pw-legacy-owner")
    _member(uid, alpha)
    flat = scratch / LEGACY_RUN
    flat.mkdir(parents=True)
    (flat / "report.md").write_text("![图](./artifacts/chart_task_1.png)\n\n旧报告\n", encoding="utf-8")
    (flat / "evaluation.json").write_text(
        '{"run_id": "%s", "status": "success", "question": "旧问题", "llm_calls": 3, "duration_seconds": 1.0}'
        % LEGACY_RUN,
        encoding="utf-8",
    )
    execute(
        "INSERT INTO jobs (job_id, user_id, org_id, question, mode, run_id, status, progress) "
        "VALUES (?, ?, ?, '旧问题', 'mock', ?, 'success', 100)",
        (f"job_legacy_{LEGACY_RUN[-8:]}", uid, alpha, LEGACY_RUN),
    )
    with TestClient(app) as client:
        _login(client, "legacy_owner", "pw-legacy-owner")
        assert [row["run_id"] for row in client.get("/api/runs").json()] == [LEGACY_RUN]
        report = client.get(f"/api/reports/{LEGACY_RUN}")
        assert report.status_code == 200, report.text
        # 图片地址里没有企业段：org 只存在于服务器本地路径
        assert f"/outputs/{LEGACY_RUN}/artifacts/chart_task_1.png?t=" in report.json()["content"]
        assert "/org/" not in report.json()["content"]


def test_absolute_image_links_lose_the_org_segment(scratch):
    """旧报告里的绝对路径可能已经带企业树段，转成的 URL 必须只剩 run_id 那一段。"""
    from app.routers.reports import _normalize_report_links

    run_id = "run_20260101_000000_bbbbbbbb"
    raw = rf"![图](D:\proj\多agent数据分析\outputs\org\7\{run_id}\artifacts\c.png)"
    url = _normalize_report_links(run_id, raw)
    assert url == f"![图](/outputs/{run_id}/artifacts/c.png)", url


def test_location_refuses_before_touching_the_filesystem(scratch):
    """看不见就当场按「不存在」收敛，而且定位这件事不许有副作用（不建目录、不落文件）。"""
    alpha, beta = _org("alpha"), _org("beta")
    author = _user("loc_author", "pw-loc-author")
    outsider = _user("loc_out", "pw-loc-out")
    _member(author, alpha)
    _member(outsider, beta)
    with TestClient(app) as client:
        _login(client, "loc_author", "pw-loc-author")
        run_id = _run_mock_analysis(client, _upload(client))

    user_out = query_one("SELECT id, username, role FROM users WHERE username = ?", ("loc_out",))
    with pytest.raises(HTTPException) as refused:
        paths.run_dir(run_id, user_out)
    assert refused.value.status_code == 404, "跨企业定位没有按「不存在」收敛"
    # 拒绝之后不许留下任何痕迹：外企业那棵树下既没有这个 run，也没被顺手建出 run 目录
    assert not (scratch / "org" / str(beta) / run_id).exists()
    assert not list((scratch / "org" / str(beta)).glob("run_*"))
    # CLI 直跑的 run 没有作业行 ⇒ 谁都定位不到（默认拒绝），包括 admin
    seed_admin = query_one("SELECT id, username, role FROM users WHERE role = 'admin' LIMIT 1")
    if seed_admin:
        with pytest.raises(HTTPException):
            paths.run_dir("run_20991231_000000_ffffffff", seed_admin)


def test_session_state_is_one_place_not_two(scratch):
    """会话目录：引擎写的与 Web 读的必须是同一个位置，而且就在会话行那棵树上。

    这条用例盯的是那次真会发生的分叉：产物按企业分树之后，如果引擎还按"它拿到的产物根"
    自己猜会话位置，而 Web 侧按会话行算，两边就指到两个目录——续轮读回来是空的，
    而接口一路 200。所以这里既看接口回读，也看目录，两边都要。
    """
    alpha = _org("alpha")
    _member(_user("sess_user", "pw-sess-user"), alpha)
    with TestClient(app) as client:
        _login(client, "sess_user", "pw-sess-user")
        dataset_id = _upload(client)
        session_id = client.post(
            "/api/sessions", json={"title": "多轮", "dataset_id": dataset_id}
        ).json()["session_id"]
        job = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"question": QUESTION, "mode": "mock"},
        ).json()
        deadline = time.time() + 90
        while time.time() < deadline:
            current = client.get(f"/api/jobs/{job['job_id']}").json()
            if current["status"] not in ("pending", "queued", "running"):
                break
            time.sleep(0.5)
        assert current["status"] in ("success", "partial", "degraded"), current

        turns = client.get(f"/api/sessions/{session_id}/messages").json()
        assert turns, "续轮跑完了，接口却读不回任何一轮——两边的会话位置分叉了"
        assert turns[0]["run_id"] == current["run_id"], turns[0]

    tree = scratch / "org" / str(alpha)
    assert (tree / "sessions" / session_id).is_dir(), f"会话不在企业树里：{tree}"
    assert not (scratch / "sessions").exists(), "全局 sessions 树还在被写"
    # 位置由**会话行**的 org 决定：把行里的归属改掉，读回来的路径跟着改（同一条推导式）
    row = query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    assert paths.session_dir(row) == tree / "sessions" / session_id


# ---------------------------------------------------------------- 下限与守卫


def test_pack_preflight_builds_its_cache_inside_the_same_tree(scratch):
    """预检（派发前那次归一化）与运行用的必须是同一棵树——被拒的请求也不例外。

    这条刻意拿一个**必被预检拒掉**的组合：缺列的包 ⇒ 422 ⇒ 根本不会有 run。
    于是"缓存出现在哪儿"只可能是预检那一次写的，观测点与运行分开了。
    原来预检写 `outputs/bundles/`、运行写 `outputs/org/<id>/bundles/`：同一批数据解析两遍、
    磁盘两份，而"这里建的缓存就是待会儿那次运行要用的那份"这句注释会变成假的。
    """
    alpha = _org("alpha")
    uid = _user("pre_user", "pw-pre-user")
    _member(uid, alpha)
    # login_audit 包要 time/user/action 这类列，零售销量表一列都没有 ⇒ 预检必拒
    retail = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"
    dataset_id = _seed_dataset(uid, alpha, retail)
    with TestClient(app) as client:
        _login(client, "pre_user", "pw-pre-user")
        response = client.post(
            "/api/analyze",
            json={"question": "登录审计", "dataset_id": dataset_id, "mode": "mock", "pack": "login_audit"},
        )
        assert response.status_code == 422, response.text
        assert "必需列" in response.json()["detail"], response.json()

    org_tree = scratch / "org" / str(alpha)
    cached = sorted(path.name for path in (org_tree / "bundles").iterdir()) if (org_tree / "bundles").is_dir() else []
    assert cached and all(name.startswith("bd_") for name in cached), f"预检没在企业树里建缓存：{cached}"
    assert not (scratch / "bundles").exists(), "预检把归一化缓存写回了全局根目录"
    assert not list(scratch.glob("run_*")), "被拒的请求不该产出任何 run 目录"


def test_org_segment_cannot_be_anything_but_an_integer():
    """org_id 会进路径：非数字当场炸，拼不出多一层或带 `..` 的位置。"""
    assert config.org_outputs_root(7) == config.outputs_root() / "org" / "7"
    assert config.org_outputs_root("7") == config.outputs_root() / "org" / "7"
    for bad in ("../evil", "3/../../x", "", None):
        with pytest.raises((TypeError, ValueError)):
            config.org_outputs_root(bad)  # type: ignore[arg-type]
    assert config.sessions_root(3) == config.org_outputs_root(3) / "sessions"


def test_a_dead_knob_stays_dead(monkeypatch, tmp_path):
    """`SESSIONS_ROOT` 曾经是个"看着能配、实际不生效"的旋钮，别再把它养回来。

    引擎不读这个变量（它只认调用方给的产物根与会话根），所以一设它就把同一份会话
    劈成两个目录。现在会话位置只有一条推导式，环境变量改不动它。
    """
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setenv("SESSIONS_ROOT", str(tmp_path / "elsewhere"))
    assert config.sessions_root(2) == tmp_path / "outputs" / "org" / "2" / "sessions"


def test_artifact_location_has_exactly_one_home():
    """路由与 runner 不许自己拼产物路径——定位只住在 `app/paths.py`。

    按 AST 查，不查字符串：这条要防的是"某个路由里多写一句 `config.outputs_root() / run_id`"，
    那种代码在 f-string、变量中转几手之后，正则就看不见它了（P2 的标记内联那次教训）。
    允许出现"根"的地方只有两处：`config.py`（它就是把根算出来的地方）与 `paths.py`（定位）。
    其余模块要产物位置，只能调 `paths.*`。
    """
    root = Path(__file__).resolve().parents[1]
    home = {"config.py", "paths.py"}
    offenders: list[str] = []
    for py in sorted((root / "app").rglob("*.py")):
        if py.name in home:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            left = ast.dump(node.left)
            for name in ("outputs_root", "sessions_root", "org_outputs_root"):
                if f"'{name}'" in left:
                    offenders.append(f"{py.name}:{node.lineno} {name}")
        # 直接拼字符串进路径也一样算越界（f-string 里的 {run_id} 拼出来的那段）
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = "".join(
                    part.value for part in node.values if isinstance(part, ast.Constant)
                )
                if "outputs_root" in text or "sessions_root" in text:
                    offenders.append(f"{py.name}:{node.lineno} f-string 拼产物根")
    assert offenders == [], "产物定位出现了第二处实现：" + ", ".join(offenders)
