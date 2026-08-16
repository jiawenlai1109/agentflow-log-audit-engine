"""后端 API 集成测试（FastAPI TestClient，mock 模式）。"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"


def _wait_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} 超时未完成")


def test_full_flow(tmp_path):
    with TestClient(app) as client:
        # 登录
        login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        assert login.json()["token"]

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
