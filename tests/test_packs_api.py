"""#33 Web 层场景包入口的回归：能列出、能点名跑、坏输入在派发前就被拒。

断言的重点不是"接口返回 200"，而是四件事：
① 包名是授权面——不在名单里的名字（含 `../`）必须在边界上拒，而不是变成一个 failed job；
② "包要什么列、数据有什么列"用引擎同一套判据，界面放行而跑起来缺列是不允许的；
③ 请求侧批准只对 `grantable_approvals` 名单里的工具生效，且判定留在引擎；
④ 每条新 SQL 仍自带 user_id，跨用户一律 404。
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import paths
from app.db import execute, query, query_one
from app.main import app
from app.security import hash_password

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIAGE = PROJECT_ROOT / "demo" / "data" / "triage"
LOGIN_CSV = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"

T1_SUBJECTS = ["203.0.113.7->admin"]


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def _make_user(username: str, password: str) -> int:
    execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password(password)),
    )
    return query_one("SELECT id FROM users WHERE username = ?", (username,))["id"]


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """产物与包目录都指到 tmp：本地 outputs/ 与 .appdata/ 会跨运行残留。

    指路径只走环境变量（`app/config.py` 唯一的权威）：受理层、执行层与报告接口读到
    的必须是同一个目录，否则"跑完了但报告读不到"这种红就是自己造的。
    """
    monkeypatch.setenv("BUNDLES_DIR", str(tmp_path / "appdata" / "bundles"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    return tmp_path


@pytest.fixture()
def owner():
    return _make_user("p_owner", "p-owner-pw-1")


@pytest.fixture()
def intruder():
    return _make_user("p_intruder", "p-intruder-pw-1")


def _jobs_of(user_id: int) -> list[dict]:
    return query("SELECT job_id, status, pack FROM jobs WHERE user_id = ?", (user_id,))


def _upload_triple(client: TestClient, name: str = "SOC三源") -> str:
    files = [
        (
            "files",
            (
                path.name,
                io.BytesIO(path.read_bytes()),
                "text/csv",
            ),
        )
        for path in (TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv")
    ]
    response = client.post("/api/bundles", files=files, data={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["bundle_id"]


def _seed_dataset(client: TestClient, path: Path) -> int:
    """按上传接口的口径塞一行 datasets（列名从文件头读，不手抄）。"""
    columns = path.read_text(encoding="utf-8").splitlines()[0].split(",")
    row_id = execute(
        "INSERT INTO datasets (user_id, filename, path, size, row_count, columns)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            query_one("SELECT id FROM users WHERE username = 'p_owner'", ())["id"],
            path.name,
            str(path),
            path.stat().st_size,
            sum(1 for _ in path.open(encoding="utf-8")) - 1,
            json.dumps(columns, ensure_ascii=False),
        ),
    )
    return int(row_id)


def _wait_job(client: TestClient, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("pending", "queued", "running"):
            return job
        time.sleep(0.4)
    raise AssertionError(f"任务 {job_id} 超时未结束")


# ---------------------------------------------------------------- 目录接口


def test_packs_endpoint_requires_auth(workspace, owner):
    with TestClient(app) as client:
        assert client.get("/api/packs").status_code == 401


def test_packs_lists_both_scenarios_with_their_contract(workspace, owner):
    """列出的不只是名字：必需列与规则 id 要在，UI 与审计都靠它判断能不能跑。"""
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        body = client.get("/api/packs").json()
    names = [item["name"] for item in body["packs"]]
    assert names == ["login_audit", "sigma_triage"], names
    sigma = next(item for item in body["packs"] if item["name"] == "sigma_triage")
    assert sigma["rule_ids"] == ["T1", "T3", "T4"], sigma["rule_ids"]
    assert "是否生产" in sigma["required_columns"], "跨表加权规则的必需列必须在契约里露出来"
    assert sigma["subject_label"] == "多源告警分诊"
    assert body["broken"] == []


def test_broken_pack_directory_is_reported_not_hidden(workspace, owner, tmp_path, monkeypatch):
    """坏包从列表里蒸发 = 用户只看到"没这个场景"，而真因在磁盘上少了一行 yaml。"""
    from agentflow.core import pack as pack_module

    # packs 根用单独一个目录，不用 tmp_path 本身：那个目录里现在住着 outputs/ 与 appdata/
    # （lifespan 按环境变量建它们），拿它当根就等于把"运行产物目录"报成坏包。
    root = tmp_path / "packs_root"
    broken_dir = root / "broken_pack"
    broken_dir.mkdir(parents=True)
    (broken_dir / "rules.yaml").write_text(
        "pack:\n  name: broken_pack\nrules:\n  - id: X1\n", encoding="utf-8"
    )
    real = pack_module.PACKS_DIR
    monkeypatch.setattr(pack_module, "PACKS_DIR", root)
    try:
        with TestClient(app) as client:
            _login(client, "p_owner", "p-owner-pw-1")
            body = client.get("/api/packs").json()
    finally:
        monkeypatch.setattr(pack_module, "PACKS_DIR", real)
    assert [item["dir"] for item in body["broken"]] == ["broken_pack"], body
    assert "report_template.md" in body["broken"][0]["reason"]
    assert body["packs"] == []


# ---------------------------------------------------------------- 点名运行


def test_bundle_plus_pack_runs_the_triage_scenario_end_to_end(workspace, owner):
    """SOC 分诊第一次从 HTTP 跑通：报告是分诊队列，事实层记下了跑的哪个包。"""
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        bundle_id = _upload_triple(client)
        response = client.post(
            "/api/analyze",
            json={
                "question": "生产域主机的异常告警有哪些？哪些需要立刻处置",
                "bundle_id": bundle_id,
                "mode": "mock",
                "pack": "sigma_triage",
            },
        )
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["pack"] == "sigma_triage", "job 回显要带包名，历史页才分得开场景"
        finished = _wait_job(client, job["job_id"])
        assert finished["status"] == "success", finished

        report = client.get(f"/api/reports/{finished['run_id']}").json()["content"]
        assert "分诊队列" in report
        for subject in T1_SUBJECTS:
            assert subject in report, f"命中主体没进报告：{subject}"

        # 这条断言伸手进文件系统，所以它必须用**同一个**定位函数（app/paths.py）。
        # 原来这里硬拼 `outputs/<run_id>/evaluation.json`：P3 产物按企业分树之后它立刻红了，
        # 而红的原因是量具自己写了第二套位置推导，不是包没进事实层——那正是"定位有第二处实现"
        # 要付的账，只不过这次付在测试里。
        evaluation_path = paths.run_dir(
            str(finished["run_id"]), {"id": owner, "role": "user"}
        ) / "evaluation.json"
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        assert evaluation["pack"]["name"] == "sigma_triage"
        assert evaluation["pack"]["rule_ids"] == ["T1", "T3", "T4"]
        # 包名进了事实层才算数：只写进 jobs 表的话，产物本身仍回答不了"这是哪套规则的产出"
        assert evaluation["skills"] is not None, "能力面留痕不许因为走 Web 就少一段"


def test_login_pack_runs_on_a_single_dataset(workspace, owner):
    """dataset 分支也通：包与单文件数据集的列匹配用的是同一套判据。"""
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)
        response = client.post(
            "/api/analyze",
            json={
                "question": "对登录日志做安全审计",
                "dataset_id": dataset_id,
                "mode": "mock",
                "pack": "login_audit",
            },
        )
        assert response.status_code == 200, response.text
        finished = _wait_job(client, response.json()["job_id"])
        assert finished["status"] == "success", finished
        assert finished["pack"] == "login_audit"


def test_run_without_pack_is_unchanged(workspace, owner):
    """不选包 = 老的普通分析路径一个字节都没变（回归的底线）。"""
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)
        response = client.post(
            "/api/analyze",
            json={"question": "登录尝试有多少次", "dataset_id": dataset_id, "mode": "mock"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["pack"] is None


# ---------------------------------------------------------------- 迁移与事实层


def test_old_database_gets_the_pack_column(tmp_path, monkeypatch):
    """本地库早就存在、且**没有**这一列：不加增量迁移，Web 侧第一次 INSERT 就崩。

    这条必须在"干净的老库"上测。在开发机上测是测不出来的——那台机器的库里
    这一列已经被前一次运行加上了，删掉迁移也照样绿。
    """
    import sqlite3

    from app import db as db_module

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,"
        " user_id INTEGER NOT NULL DEFAULT 1, question TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'mock',"
        " session_id TEXT, run_id TEXT, status TEXT NOT NULL DEFAULT 'pending', progress INTEGER NOT NULL DEFAULT 0,"
        " error TEXT, created_at TEXT, finished_at TEXT)"
    )
    conn.execute(
        "INSERT INTO jobs (job_id, user_id, question) VALUES ('job_legacy', 1, '老库里的一次运行')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(legacy))
    db_module.init_db()

    conn = sqlite3.connect(legacy)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert "pack" in columns, "老库没补上 pack 列：新代码的 INSERT 会直接崩"
        # 老行不能被迁移弄丢：它是历史，不是待清理的脏数据
        assert conn.execute("SELECT pack FROM jobs WHERE job_id='job_legacy'").fetchone()[0] is None
    finally:
        conn.close()


def test_evaluation_json_records_which_pack_ran(tmp_path):
    """事实层要能自证"这次跑的是哪个场景包"。

    只写进 jobs 表不够：产物本身（给审计、给报告页、给离线复盘看的那份东西）
    必须带着这个信息，否则一次分诊的 report.md 与一次普通审计的 report.md
    在产物层面分不出彼此。
    """
    from agentflow.pipeline import run_analysis

    result = run_analysis(
        "生产域主机的异常告警有哪些？哪些需要立刻处置",
        [str(TRIAGE / "auth.csv"), str(TRIAGE / "assets.csv"), str(TRIAGE / "edr.csv")],
        outputs_root=tmp_path,
        pack="sigma_triage",
    )
    evaluation = json.loads(
        (Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8")
    )
    assert evaluation["pack"] == {
        "name": "sigma_triage",
        "version": "1.0",
        "rule_ids": ["T1", "T3", "T4"],
        "required_columns": ["time", "account", "auth_result", "service", "主机", "是否生产", "严重级"],
    }, evaluation["pack"]

    plain = run_analysis("总销售额是多少？", [str(PROJECT_ROOT / "demo" / "data" / "retail_sales.csv")], outputs_root=tmp_path)
    plain_evaluation = json.loads((Path(plain["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    assert plain_evaluation["pack"] is None, "没选包时这个字段是 null，不是缺键——两种写法下游要各判一次"

# ---------------------------------------------------------------- 边界拒绝


@pytest.mark.parametrize(
    "bad_name",
    ["nope", "../packs", "../../etc", "a/b", "sigma_triage/", "", "Sigma_Triage", " login_audit"],
)
def test_unknown_or_malformed_pack_name_is_422_and_creates_no_job(workspace, owner, bad_name):
    """422 而不是 failed job：包名写错是调用方的即时错误，不该占用一次运行。

    `""` 也在列里——空的 pack 不该被当成"没选包"静默放行；`Sigma_Triage` 在列里是因为
    包名大小写敏感，"帮你归一化"会让人以为跑的是那个包。
    """
    user_id = _make_user("p_owner", "p-owner-pw-1")
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)
        before = len(_jobs_of(user_id))
        response = client.post(
            "/api/analyze",
            json={
                "question": "x",
                "dataset_id": dataset_id,
                "mode": "mock",
                "pack": bad_name,
            },
        )
        assert response.status_code == 422, (bad_name, response.status_code, response.text)
        assert len(_jobs_of(user_id)) == before, "被拒的请求不该留下任何 job 行"


def test_payload_with_a_misspelled_pack_key_is_rejected(workspace, owner):
    """`packk` 必须 422，而不是静默当成普通分析跑起来。

    这一条是 `extra="forbid"` 的存在理由：拼错的键被吃掉时，调用方看到"跑成功了"，
    实际跑的是另一个东西——那是最难自察的一类错。
    """
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)
        response = client.post(
            "/api/analyze",
            json={
                "question": "对登录日志做安全审计",
                "dataset_id": dataset_id,
                "mode": "mock",
                "packk": "login_audit",
            },
        )
        assert response.status_code == 422, response.text
        assert "Extra data" in json.dumps(response.json()) or "packk" in response.text


def test_pack_with_dataset_missing_required_columns_is_422_naming_them(workspace, owner):
    """缺列要在派发前说清缺哪几列，而不是让用户等一份降级报告。

    E25 在评测集里钉的是引擎立场（缺表就整包拒绝）；这里钉的是同一立场的 Web 表达。
    """
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)  # 没有 是否生产 / 严重级
        response = client.post(
            "/api/analyze",
            json={
                "question": "生产域主机的异常告警有哪些",
                "dataset_id": dataset_id,
                "mode": "mock",
                "pack": "sigma_triage",
            },
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "是否生产" in detail and "严重级" in detail, detail
        assert "data_convention.md" in detail, "要告诉调用方去哪看列约定"


def test_pack_plus_session_is_rejected_with_a_reason(workspace, owner):
    """会话只绑单文件数据集：允许带包的续轮 = 第二轮静默丢掉场景口径。"""
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)
        session_id = client.post(
            "/api/sessions", json={"title": "t", "dataset_id": dataset_id}
        ).json()["session_id"]
        response = client.post(
            "/api/analyze",
            json={
                "question": "对登录日志做安全审计",
                "dataset_id": dataset_id,
                "mode": "mock",
                "pack": "login_audit",
                "session_id": session_id,
            },
        )
        assert response.status_code == 422, response.text
        # pydantic 的校验错误是结构化列表，要按原文找原因而不是猜 detail 的形状
        assert "会话" in json.dumps(response.json(), ensure_ascii=False)


def test_intruder_cannot_borrow_owners_bundle_with_a_pack(workspace, owner, intruder):
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        bundle_id = _upload_triple(client)
    with TestClient(app) as client:
        _login(client, "p_intruder", "p-intruder-pw-1")
        response = client.post(
            "/api/analyze",
            json={
                "question": "生产域主机的异常告警有哪些",
                "bundle_id": bundle_id,
                "mode": "mock",
                "pack": "sigma_triage",
            },
        )
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------- 批准的第二入口


def _config_with_grantable(tmp_path: Path, grantable: list[str]):
    import yaml

    from agentflow.core.mcp import load_mcp_config

    payload = yaml.safe_load(
        (PROJECT_ROOT / "config" / "mcp.yaml").read_text(encoding="utf-8")
    )
    payload["grantable_approvals"] = grantable
    path = tmp_path / "mcp.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return load_mcp_config(path)


def test_approval_for_an_unlisted_tool_is_422(workspace, owner, monkeypatch):
    """请求想批的工具不在名单里 ⇒ 当场拒，而不是"批了但不生效"。"""
    from agentflow.core.mcp import load_mcp_config

    monkeypatch.setattr("app.routers.jobs.load_mcp_config", lambda: load_mcp_config())
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)
        response = client.post(
            "/api/analyze",
            json={
                "question": "登录审计",
                "dataset_id": dataset_id,
                "mode": "mock",
                "mcp_approvals": {"mcp:soc_intel:write_note": True},
            },
        )
        assert response.status_code == 422, response.text
        assert "grantable_approvals" in response.json()["detail"]


def test_grantable_list_can_open_the_approval_and_the_actor_is_traced(
    workspace, owner, monkeypatch
):
    """运维把工具放进名单后请求批准才生效，而且**经手人**要落到调用参数里。

    这里只验"API 把请求批准与来源交给了 pipeline"——真正裁决批不批的是引擎闸门，
    由 `test_mcp.py::test_request_approval_only_counts_for_grantable_tools` 负责。
    分成两条是刻意的：如果只在 API 层测放行，将来加一条不走 HTTP 的调用通道就把判定绕开了。
    """
    recorded: list[dict] = []

    def stub_run_analysis(**kwargs):
        recorded.append(kwargs)
        return {"run_id": "run_stub", "status": "success", "report": {}, "outputs_dir": str(workspace)}

    config = _config_with_grantable(workspace, ["mcp:soc_intel:write_note"])
    monkeypatch.setattr("app.routers.jobs.load_mcp_config", lambda: config)
    monkeypatch.setattr("app.runner.run_analysis", stub_run_analysis)
    with TestClient(app) as client:
        _login(client, "p_owner", "p-owner-pw-1")
        dataset_id = _seed_dataset(client, LOGIN_CSV)
        response = client.post(
            "/api/analyze",
            json={
                "question": "登录审计",
                "dataset_id": dataset_id,
                "mode": "mock",
                "mcp_approvals": {"mcp:soc_intel:write_note": True},
            },
        )
        assert response.status_code == 200, response.text
        deadline = time.time() + 10
        while not recorded and time.time() < deadline:
            time.sleep(0.2)
    assert recorded, "run_analysis 没被调到（桩没生效或线程没跑）"
    kwargs = recorded[-1]
    assert kwargs["mcp_approvals"] == {"mcp:soc_intel:write_note": True}
    origin = kwargs["run_origin"]
    assert origin["source"] == "web"
    assert origin["actor_username"] == "p_owner", "批准要能追回经手人"
    assert origin["approvals_requested"] == {"mcp:soc_intel:write_note": True}


def test_default_config_keeps_the_ops_only_authority():
    """仓库默认配置里 grantable 是空的：任何请求签字都不作数。"""
    from agentflow.core.mcp import load_mcp_config

    config = load_mcp_config(PROJECT_ROOT / "config" / "mcp.yaml")
    assert config.grantable == frozenset()
