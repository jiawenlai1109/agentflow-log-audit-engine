"""后端 API 集成测试（FastAPI TestClient，mock 模式）。"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.routers.reports import _normalize_report_links

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"
DATA_PROFIT = PROJECT_ROOT / "demo" / "data" / "retail_sales_with_profit.csv"


def test_normalize_relative_report_link():
    content = "![图](./artifacts/chart_task_2.png)"
    assert _normalize_report_links("run_x", content) == "![图](/outputs/run_x/artifacts/chart_task_2.png)"


def test_normalize_absolute_report_link():
    content = r"![图](D:\my_agent_project\多agent数据分析\outputs\run_x\artifacts\chart_task_2.png)"
    assert "/outputs/run_x/artifacts/chart_task_2.png" in _normalize_report_links("run_x", content)


def _wait_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} 超时未完成")


def _login(client: TestClient, username: str = "admin", password: str = "admin") -> str:
    """登录并把 token 装进 client 默认头，返回 token（供媒体 URL 拼接用）。"""
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def test_full_flow(tmp_path):
    with TestClient(app) as client:
        # 登录
        token = _login(client)
        assert token

        # 上传数据集
        with DATA.open("rb") as fh:
            upload = client.post(
                "/api/datasets", files={"file": ("retail_sales.csv", fh, "text/csv")}
            )
        assert upload.status_code == 200, upload.text
        dataset = upload.json()
        assert dataset["row_count"] == 2000
        assert "销售额" in dataset["columns"]

        # 提交分析（mock）
        analyze = client.post(
            "/api/analyze",
            json={"question": "总销售额是多少？", "dataset_id": dataset["id"], "mode": "mock"},
        )
        assert analyze.status_code == 200
        job_id = analyze.json()["job_id"]
        job = _wait_job(client, job_id)
        assert job["status"] == "success"
        run_id = job["run_id"]

        # 报告
        report = client.get(f"/api/reports/{run_id}")
        assert report.status_code == 200
        assert "关键指标" in report.json()["content"]

        # 评估摘要
        summary = client.get("/api/evaluations/summary").json()
        assert summary["total"] >= 1

        # 会话：新建 → 发消息 → 查看历史
        session = client.post("/api/sessions", json={"title": "测试会话", "dataset_id": dataset["id"]})
        assert session.status_code == 200
        session_id = session.json()["session_id"]
        message = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"question": "最近7天每日销售额的走势如何？", "mode": "mock"},
        )
        assert message.status_code == 200
        session_job = _wait_job(client, message.json()["job_id"])
        assert session_job["status"] == "success"
        history = client.get(f"/api/sessions/{session_id}/messages")
        assert history.status_code == 200
        assert len(history.json()) >= 1


def test_mock_profit_top3_report_and_image_link():
    """利润Top3 问题：mock 报告含利润指标，图片链接转为 /outputs/ URL。"""
    with TestClient(app) as client:
        _login(client)
        with DATA_PROFIT.open("rb") as fh:
            upload = client.post(
                "/api/datasets", files={"file": ("retail_sales_with_profit.csv", fh, "text/csv")}
            )
        assert upload.status_code == 200
        dataset = upload.json()
        analyze = client.post(
            "/api/analyze",
            json={
                "question": "近七天哪个产品的销售利润最高？把前三名的列出来",
                "dataset_id": dataset["id"],
                "mode": "mock",
            },
        )
        job = _wait_job(client, analyze.json()["job_id"])
        assert job["status"] == "success"
        report = client.get(f"/api/reports/{job['run_id']}").json()["content"]
        assert "利润" in report
        assert "/outputs/" in report
