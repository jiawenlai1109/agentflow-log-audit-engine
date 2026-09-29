"""#33 的活体 HTTP 验收：真起服务、真发请求、用一份全新的临时库。

为什么不用 TestClient：那已经在 tests/test_packs_api.py 里做过了。这里要额外证明的是
"过一遍真实 HTTP 栈也没变形"——中间件、鉴权头、multipart 上传、异步 job 线程、
产物落盘，全在真进程里跑一遍。

为什么用临时库：本机 .appdata/app.db 的 admin 口令是我不知道的（也不该去改），
而探针要可重跑。所以整个 APP_DATA_DIR/OUTPUTS_ROOT 指向一个临时目录，跑完即弃。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # scripts/ → 仓库根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

SCRATCH = Path(tempfile.mkdtemp(prefix="packs_e2e_"))
os.environ["APP_SECRET"] = "e2e-secret-not-for-production"
os.environ["ADMIN_PASSWORD"] = "e2e-probe-pw"

import app.config as config  # noqa: E402

config.APP_DATA_DIR = SCRATCH / ".appdata"
config.DB_PATH = config.APP_DATA_DIR / "app.db"
config.DATASETS_DIR = config.APP_DATA_DIR / "datasets"
config.BUNDLES_DIR = config.APP_DATA_DIR / "bundles"
config.SESSIONS_ROOT = SCRATCH / "outputs" / "sessions"
config.OUTPUTS_ROOT = SCRATCH / "outputs"

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from agentflow.core.streams import harden_streams  # noqa: E402
from app.main import app  # noqa: E402
from app.db import init_db  # noqa: E402

PORT = 8178
BASE = f"http://127.0.0.1:{PORT}"
TRIAGE = ROOT / "demo" / "data" / "triage"


def serve() -> threading.Thread:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(80):
        try:
            if httpx.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                return thread
        except Exception:  # noqa: BLE001 - 启动竞态，轮询到就绪为止
            time.sleep(0.25)
    raise RuntimeError("服务没起来")


def wait_job(client: httpx.Client, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.4)
    raise AssertionError(f"job {job_id} 未结束")


def main() -> int:
    # 探针自己不许把"一次全过的验收"报成崩溃：✓ ✗ 在 Windows 重定向下按 cp936 编码会抛
    harden_streams()
    init_db()
    serve()
    checks: list[tuple[str, bool, str]] = []

    with httpx.Client(base_url=BASE, timeout=30) as client:
        anonymous = client.get("/api/packs")
        checks.append(("匿名 GET /api/packs 被拒（401）", anonymous.status_code == 401, str(anonymous.status_code)))

        login = client.post("/api/auth/login", json={"username": "admin", "password": "e2e-probe-pw"})
        checks.append(("登录成功", login.status_code == 200, login.text[:80]))
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"

        listed = client.get("/api/packs").json()
        names = [item["name"] for item in listed["packs"]]
        checks.append(("列出两个场景包", names == ["login_audit", "sigma_triage"], str(names)))

        files = [
            ("files", (path.name, path.read_bytes(), "text/csv"))
            for path in (TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv")
        ]
        bundle_id = client.post("/api/bundles", files=files, data={"name": "E2E三源"}).json()["bundle_id"]

        bad = client.post(
            "/api/analyze",
            json={"question": "x", "bundle_id": bundle_id, "mode": "mock", "pack": "../../etc"},
        )
        checks.append(("越界包名 422", bad.status_code == 422, str(bad.status_code)))

        mismatch = client.post(
            "/api/analyze",
            json={"question": "x", "bundle_id": bundle_id, "mode": "mock", "pack": "login_audit"},
        )
        checks.append(("包与数据不匹配时 422 并点名缺列", mismatch.status_code == 422, mismatch.text[:120]))

        started = client.post(
            "/api/analyze",
            json={
                "question": "生产域主机的异常告警有哪些？哪些需要立刻处置",
                "bundle_id": bundle_id,
                "mode": "mock",
                "pack": "sigma_triage",
                "mcp_approvals": {"mcp:soc_intel:write_note": True},
            },
        )
        checks.append(("请求批准未放行→422", started.status_code == 422, started.text[:160]))

        started = client.post(
            "/api/analyze",
            json={
                "question": "生产域主机的异常告警有哪些？哪些需要立刻处置",
                "bundle_id": bundle_id,
                "mode": "mock",
                "pack": "sigma_triage",
            },
        )
        job = started.json()
        checks.append(("带包运行被受理", started.status_code == 200 and job["pack"] == "sigma_triage", str(job)[:120]))
        finished = wait_job(client, job["job_id"])
        checks.append(("运行 success", finished["status"] == "success", str(finished)[:160]))

        report = client.get(f"/api/reports/{finished['run_id']}").json()["content"]
        checks.append(("报告是分诊队列", "分诊队列" in report, ""))
        checks.append(("命中主体在报告里", "203.0.113.7->admin" in report, ""))
        checks.append(("外部库独有的 4242 不在报告里", "4242" not in report, ""))

    run_dir = config.OUTPUTS_ROOT / finished["run_id"]
    evaluation = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))
    checks.append(("事实层记下了包与规则", evaluation["pack"]["rule_ids"] == ["T1", "T3", "T4"], str(evaluation["pack"])))
    events = [json.loads(line) for line in (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    attached = next((e for e in events if e.get("event") == "mcp_attached"), None)
    checks.append(("外部证据留痕在场", bool(attached) and bool(evaluation["external_evidence"]), str(attached)[:120] if attached else "无"))
    kinds = [entry.get("kind") for entry in events]
    checks.append(("包运行走了规则规划（不是通用 LLM 规划）", "plan" not in [k for k in kinds if k == "plan_llm"], ""))

    print("=" * 74)
    for label, ok, note in checks:
        print(f"  {'✓' if ok else '✗'} {label}" + (f"   [{note}]" if note and not ok else ""))
    failed = [label for label, ok, _n in checks if not ok]
    print("=" * 74)
    print(f"产物目录：{run_dir}")
    print(f"临时库：{config.DB_PATH}")
    print(f"结论：{len(checks) - len(failed)}/{len(checks)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
