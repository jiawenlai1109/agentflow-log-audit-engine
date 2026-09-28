"""Bundle 上传接口回归（#20）：多文件、逐文件状态、三道防线、归属隔离。

断言的重点不是"接口返回 200"，而是三件事：
① 一个坏文件不拖垮整包（逐文件 kind/reason 各归各）；
② 防线在解析之前（伪装扩展名、zip 炸弹、超大文件都不进解析器）；
③ 每条读写都自带 user_id（跨用户一律 404，不给枚举留缝）。
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import execute, query, query_one
from app.main import app
from app.security import hash_password

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TINY_PNG = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001")
SALES_CSV = "订单日期,销售额\n2026-01-01,10\n2026-01-02,20\n"
HOSTS_CSV = "主机,域\nh1,生产\nh2,测试\n"
AUDIT_LOG = "2026-09-05 20:00 svc_backup login ok\n"


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
def bundles_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.bundles.BUNDLES_DIR", tmp_path / "bundles")
    monkeypatch.setattr("app.routers.jobs.OUTPUTS_ROOT", tmp_path / "outputs")
    # 报告接口也读 OUTPUTS_ROOT：三个消费方一起指到 tmp，否则跑完的 run 会在项目目录里找
    monkeypatch.setattr("app.routers.reports.OUTPUTS_ROOT", tmp_path / "outputs")
    return tmp_path / "bundles"


@pytest.fixture()
def owner():
    return _make_user("b_owner", "b-owner-pw-1")


@pytest.fixture()
def intruder():
    return _make_user("b_intruder", "b-intruder-pw-1")


@pytest.fixture(autouse=True)
def _own_bundles_cleaned(owner):
    """本地元数据库跨运行会残留：本文件的每个包都由这里按 bundle_id 收尾。

    早先用 `DELETE ... WHERE user_id = ?` 清理，会连带删掉同一用户其他用例的行，
    而且不清子表——留下的正是"按 filename 回查读到历史行"这类假失败的温床。
    """
    before = {
        row["bundle_id"]
        for row in query("SELECT bundle_id FROM bundles WHERE user_id = ?", (owner,))
    }
    yield
    created = [
        row["bundle_id"]
        for row in query("SELECT bundle_id FROM bundles WHERE user_id = ?", (owner,))
        if row["bundle_id"] not in before
    ]
    for bundle_id in created:
        for table in ("bundle_tables", "bundle_files", "bundles"):
            execute(
                f"DELETE FROM {table} WHERE bundle_id = ? AND user_id = ?", (bundle_id, owner)
            )


def _upload(
    client: TestClient,
    files: list[tuple[str, bytes, str]],
    name: str = "包A",
    uploads: list[dict] | None = None,
    async_parse: bool = False,
):
    data = {"name": name}
    if uploads is not None:
        data["uploads"] = json.dumps(uploads, ensure_ascii=False)
    if async_parse:
        data["async_parse"] = "true"
    return client.post(
        "/api/bundles",
        files=[("files", (fname, io.BytesIO(content), ctype)) for fname, content, ctype in files],
        data=data,
    )


def test_multi_file_bundle_lands_tables_and_evidence(bundles_dir, owner, intruder):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        response = _upload(
            client,
            [
                ("sales.csv", SALES_CSV.encode("utf-8"), "text/csv"),
                ("hosts.csv", HOSTS_CSV.encode("utf-8"), "text/csv"),
                ("audit.log", AUDIT_LOG.encode("utf-8"), "text/plain"),
            ],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        kinds = {item["filename"]: item["kind"] for item in body["files"]}
        assert kinds == {"sales.csv": "table", "hosts.csv": "table", "audit.log": "document"}
        assert body["table_count"] == 2
        assert body["document_count"] == 1
        # 证据文档不进表清单（I1：文档不是数字来源）
        refs = {item["table_ref"] for item in body["tables"]}
        assert refs == {"t1", "t2"}
        assert all("主机" in item["columns"] or "销售额" in item["columns"] for item in body["tables"])
    

def test_anonymous_upload_is_rejected(bundles_dir):
    with TestClient(app) as client:
        response = _upload(client, [("a.csv", SALES_CSV.encode("utf-8"), "text/csv")])
        assert response.status_code == 401


def test_cross_user_bundle_is_404_not_403(bundles_dir, owner, intruder):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        bundle_id = _upload(client, [("a.csv", SALES_CSV.encode("utf-8"), "text/csv")]).json()["bundle_id"]
    with TestClient(app) as other:
        _login(other, "b_intruder", "b-intruder-pw-1")
        for path in (f"/api/bundles/{bundle_id}", f"/api/bundles/{bundle_id}/preview", ):
            assert other.get(path).status_code == 404, path
        assert other.delete(f"/api/bundles/{bundle_id}").status_code == 404
        assert query_one("SELECT * FROM bundles WHERE bundle_id = ?", (bundle_id,)) is not None


def test_list_only_shows_own_bundles(bundles_dir, owner, intruder):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        _upload(client, [("a.csv", SALES_CSV.encode("utf-8"), "text/csv")])
        mine = client.get("/api/bundles").json()
        assert all(item["name"] == "包A" for item in mine)
    with TestClient(app) as other:
        _login(other, "b_intruder", "b-intruder-pw-1")
        theirs = other.get("/api/bundles").json()
        ids = {item["bundle_id"] for item in mine}
        assert ids.isdisjoint({item["bundle_id"] for item in theirs})


def test_disguised_binary_is_rejected_per_file_without_killing_bundle(bundles_dir, owner):
    """PNG 改名 .csv：坏的那条被拒并说明原因，同批好文件照常成表——不是整包 400。"""
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(
            client,
            [
                ("fake.csv", TINY_PNG + b"\x00\x01", "text/csv"),
                ("good.csv", HOSTS_CSV.encode("utf-8"), "text/csv"),
            ],
        ).json()
        bad = next(item for item in body["files"] if item["filename"] == "fake.csv")
        assert bad["kind"] == "skipped"
        assert "PNG" in bad["reason"]
        assert body["status"] == "ready" and body["table_count"] == 1
        assert query_one(
            "SELECT reason FROM bundle_files WHERE bundle_id = ? AND filename = ? AND user_id = ?",
            (body["bundle_id"], "fake.csv", owner),
        )["reason"]


def test_unsupported_extension_and_empty_file_have_reasons(bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(
            client,
            [
                ("tool.exe", b"MZ\x90\x00", "application/octet-stream"),
                ("empty.csv", b"", "text/csv"),
                ("ok.csv", SALES_CSV.encode("utf-8"), "text/csv"),
            ],
        ).json()
        by_name = {item["filename"]: item for item in body["files"]}
        assert by_name["tool.exe"]["kind"] == "skipped"
        assert "不支持的文件类型" in by_name["tool.exe"]["reason"]
        assert by_name["empty.csv"]["reason"] == "空文件（0 字节）"
        assert by_name["ok.csv"]["kind"] == "table"


def test_oversize_file_is_dropped_not_half_stored(bundles_dir, owner, monkeypatch):
    monkeypatch.setattr("app.routers.bundles.MAX_UPLOAD_MB", 0)  # 上界压到 0MB：任何文件都超限
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(client, [("big.csv", SALES_CSV.encode("utf-8"), "text/csv")]).json()
        item = body["files"][0]
        assert item["kind"] == "skipped" and "超过" in item["reason"]
        assert "stored_path" not in item  # 响应不外泄服务器绝对路径
        # 响应里根本没有 stored_path 字段（M0：不外泄服务器绝对路径）；要验落盘状态只能回查库。
        # 回查必须带 bundle_id：本地库跨运行会残留同名文件行，按 filename 查会读到别人的历史
        row = query_one(
            "SELECT stored_path FROM bundle_files WHERE bundle_id = ? AND filename = ? AND user_id = ?",
            (body["bundle_id"], "big.csv", owner),
        )
        assert row["stored_path"] == ""
        assert body["status"] == "failed"  # 没有任何表 ⇒ 整包不可分析


def test_documents_only_bundle_fails_with_explanation(bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(
            client, [("notes.md", "# 说明 只有文字".encode("utf-8"), "text/markdown")]
        ).json()
        assert body["status"] == "failed"
        assert "没有任何可分析的表" in body["error"]


def test_formula_cells_flagged_but_data_not_mutated(bundles_dir, owner):
    """只标记不改数据：改写会让 sources/ 原件与 sha256 失去"当时读的就是这一份"的意义。"""
    payload = "名称,值\n=1+2|cmd,5\n-3.5,7\n@SUM(1+2),9\n"
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(client, [("f.csv", payload.encode("utf-8"), "text/csv")]).json()
        item = body["files"][0]
        assert item["risk"]["formula_cells"] == 2  # 负数 -3.5 不算注入
        preview = client.get(f"/api/bundles/{body['bundle_id']}/preview?table_ref=t1").json()
        assert preview["row_count"] == 3
        assert "=1+2|cmd" in str(preview["head"])  # 原文一字未改


def test_path_traversal_filename_is_neutralized(bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(client, [("../../etc/passwd.csv", SALES_CSV.encode("utf-8"), "text/csv")]).json()
        assert body["status"] == "ready"
        files = list((bundles_dir / body["bundle_id"] / "uploads").iterdir())
        assert all("_" in f.name or "/" not in f.name for f in files)
        assert all((bundles_dir / body["bundle_id"]).resolve() in f.resolve().parents for f in files)


def test_preview_bounds_and_unknown_table(tmp_path, bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        bundle_id = _upload(client, [("a.csv", SALES_CSV.encode("utf-8"), "text/csv")]).json()["bundle_id"]
        assert client.get(f"/api/bundles/{bundle_id}/preview?table_ref=t9").status_code == 404
        assert client.get(f"/api/bundles/{bundle_id}/preview?table_ref=t1&rows=0").status_code == 422
        assert client.get(f"/api/bundles/{bundle_id}/preview?table_ref=t1&rows=9999").status_code == 422
        assert client.get(f"/api/bundles/nope_bu/preview?table_ref=t1").status_code == 404


def test_analyze_accepts_bundle_and_produces_run(bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(
            client,
            [("sales.csv", SALES_CSV.encode("utf-8"), "text/csv"), ("hosts.csv", HOSTS_CSV.encode("utf-8"), "text/csv")],
        ).json()
        analyze = client.post(
            "/api/analyze",
            json={"question": "总销售额是多少？", "bundle_id": body["bundle_id"], "mode": "mock"},
        )
        assert analyze.status_code == 200, analyze.text
        job_id = analyze.json()["job_id"]
        job = _wait_job(client, job_id)
        assert job["status"] == "success", job
        report = client.get(f"/api/reports/{job['run_id']}")
        assert report.status_code == 200 and "关键指标" in report.json()["content"]


def test_analyze_rejects_failed_bundle_and_requires_one_source(bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        doc_only = _upload(
            client, [("n.md", "# 只有文字".encode("utf-8"), "text/markdown")]
        ).json()
        refused = client.post(
            "/api/analyze",
            json={"question": "分析一下", "bundle_id": doc_only["bundle_id"], "mode": "mock"},
        )
        assert refused.status_code == 409
        assert client.post("/api/analyze", json={"question": "q", "mode": "mock"}).status_code == 422
        both = {
            "question": "q",
            "mode": "mock",
            "bundle_id": doc_only["bundle_id"],
            "dataset_id": 1,
        }
        assert client.post("/api/analyze", json=both).status_code == 422


def test_delete_removes_rows_and_directory(bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        bundle_id = _upload(client, [("a.csv", SALES_CSV.encode("utf-8"), "text/csv")]).json()["bundle_id"]
        assert client.delete(f"/api/bundles/{bundle_id}").json()["ok"] is True
        assert query_one("SELECT * FROM bundles WHERE bundle_id = ?", (bundle_id,)) is None
        assert query("SELECT * FROM bundle_files WHERE bundle_id = ?", (bundle_id,)) == []
        assert not (bundles_dir / bundle_id).exists()


def _wait_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.4)
    raise TimeoutError(f"job {job_id} 未完成")


# ---------------------------------------------------------------- 分片与异步解析


def _count_bundles(user_id: int) -> int:
    return len(query("SELECT bundle_id FROM bundles WHERE user_id = ?", (user_id,)))


def _send_chunk(client: TestClient, upload_id: str, index: int, total: int, blob: bytes, filename="big.csv"):
    return client.post(
        "/api/bundles/chunks",
        data={"upload_id": upload_id, "index": index, "total": total, "filename": filename},
        files={"file": (f"{index}.part", io.BytesIO(blob), "application/octet-stream")},
    )


def test_chunks_reassemble_out_of_order_and_accept_resend(bundles_dir, owner):
    """乱序投递 + 同一片重发都要能拼回原样：断言拼出来的表行数与原始数据一致。"""
    rows = "".join(f"2026-01-{i:02d},{i}\n" for i in range(1, 31))
    payload = ("日期,销售额\n" + rows).encode("utf-8")
    size = -(-len(payload) // 3)
    parts = [payload[i * size : (i + 1) * size] for i in range(3)]  # 连续切片：拼回即原文
    upload_id = "up_test_reassemble_1"
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        assert _send_chunk(client, upload_id, 2, 3, parts[2]).status_code == 200
        assert _send_chunk(client, upload_id, 0, 3, parts[0]).status_code == 200
        resend = _send_chunk(client, upload_id, 0, 3, parts[0])  # 重复投递
        assert resend.status_code == 200 and resend.json()["received"] == [0, 2]
        assert _send_chunk(client, upload_id, 1, 3, parts[1]).json()["missing"] == []
        body = _upload(client, [], uploads=[{"upload_id": upload_id, "filename": "big.csv", "total": 3}]).json()
        assert body["status"] == "ready", body
        assert [f["filename"] for f in body["files"]] == ["big.csv"]
        assert body["files"][0]["kind"] == "table"
        assert body["tables"][0]["row_count"] == 30
        assert not (bundles_dir / "_chunks" / upload_id).exists()  # 重组后清掉分片


def test_missing_chunk_is_rejected_and_leaves_no_orphan_row(bundles_dir, owner):
    upload_id = "up_test_missing_1"
    before = _count_bundles(owner)
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        _send_chunk(client, upload_id, 0, 3, b"a,b\n1,2\n", filename="m.csv")
        response = _upload(client, [], uploads=[{"upload_id": upload_id, "filename": "m.csv", "total": 3}])
        assert response.status_code == 400
        assert "缺少序号" in response.json()["detail"] and "1" in response.json()["detail"]
        assert _count_bundles(owner) == before  # 失败的重构不留永远 parsing 的孤行


def test_chunk_upload_id_is_owner_bound(bundles_dir, owner, intruder):
    upload_id = "up_owner_bound_1"
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        assert _send_chunk(client, upload_id, 0, 1, SALES_CSV.encode("utf-8"), filename="sales.csv").status_code == 200
    with TestClient(app) as other:
        _login(other, "b_intruder", "b-intruder-pw-1")
        # 拿别人的 upload_id：既不能续传，也不能借它往自己的包里塞东西
        assert _send_chunk(other, upload_id, 0, 1, b"x" * 10).status_code == 404
        refused = _upload(other, [], uploads=[{"upload_id": upload_id, "filename": "m.csv", "total": 1}])
        assert refused.status_code == 404
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(client, [], uploads=[{"upload_id": upload_id, "filename": "sales.csv", "total": 1}]).json()
        assert body["status"] == "ready"
    

def test_chunk_manifest_refuses_renamed_or_recounted_upload(bundles_dir, owner):
    upload_id = "up_manifest_guard_1"
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        _send_chunk(client, upload_id, 0, 2, b"a,b\n1,2\n")
        assert _send_chunk(client, upload_id, 1, 3, b"x").status_code == 409  # 改总分片数
        assert _send_chunk(client, upload_id, 1, 2, b"x", filename="other.csv").status_code == 409  # 改名
        assert _send_chunk(client, upload_id, 1, 2, b"c,d\n3,4\n").status_code == 200


def test_chunk_rejects_bad_ids_and_out_of_range_index(bundles_dir, owner):
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        assert _send_chunk(client, "../../etc", 0, 1, b"x").status_code == 400
        assert _send_chunk(client, "up_ab", 0, 1, b"x").status_code == 400  # 少于 8 字符
        assert _send_chunk(client, "up_range_ok", 5, 3, b"x").status_code == 400


def test_async_parse_completes_in_background(bundles_dir, owner):
    """异步：立即回一个可见状态，轮询到终态。不断言一定抓到 parsing（那是竞态，不是判据）。"""
    big = "日期,销售额\n" + "".join(f"2026-01-{i:02d},{i*3}\n" for i in range(1, 201))
    with TestClient(app) as client:
        _login(client, "b_owner", "b-owner-pw-1")
        body = _upload(
            client,
            [("big.csv", big.encode("utf-8"), "text/csv"), ("hosts.csv", HOSTS_CSV.encode("utf-8"), "text/csv")],
            async_parse=True,
        ).json()
        assert body["status"] in ("parsing", "ready")
        deadline = time.time() + 20
        seen = body["status"]
        while time.time() < deadline:
            seen = client.get(f"/api/bundles/{body['bundle_id']}").json()["status"]
            if seen != "parsing":
                break
            time.sleep(0.2)
        assert seen == "ready", seen
        detail = client.get(f"/api/bundles/{body['bundle_id']}").json()
        assert detail["table_count"] == 2
        assert {f["kind"] for f in detail["files"]} == {"table"}
