"""P6-2：把链路标识从受理层带进**执行侧与产物**。

P6-1 只保证"库里有一个串"。这一片要保证的是：拿到那个串之后，运行期写下来的东西
能对上它——转录的每一行、`evaluation.json`、落库的每条事件。这三处各自的失效方式不同，
所以各有一条用例：

- 转录走 `TranscriptWriter.write()`（一处盖章，覆盖十几个生产者），生产者里有并发那一层；
- `evaluation.json` 走 `RunContext.trace_id`（评测层直接读这个文件，不读 contextvar）；
- 事件走 `app/runner._publish`（Web 与 worker 两种宿主共用那一份）。

**并发那一层是本片真正的风险点**：`ThreadPoolExecutor.submit` 不复制 contextvar，
所以任务线程里写的转录行会静默丢掉链路——症状不是报错，而是"按 trace 查回来的记录
只覆盖流水线前三段"。有一条用例专门数"由并发层写下的那些行"。

另一条底线是**不许假关联**：没有链路的运行（CLI 单跑、评测夹具）那一格**整个缺席**，
而不是写一个空串或 `null`——缺席与"该有而没有"是两种意思，混起来就查不出是哪一路漏了。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 夹具贴主场景：登录审计（不是零售报表）——这套系统要跑的是安全运营分诊
LOGIN = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"
QUESTION = "失败登录次数最高的用户是谁？"
CHAIN = "p62enginechain0000000000000000ab"  # 32 位，与列宽同形

CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n"


def _lines(run_dir: Path) -> list[dict]:
    path = run_dir / "transcript.jsonl"
    assert path.exists(), f"没有转录文件：{path}"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _evaluation(run_dir: Path) -> dict:
    return json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 产物那两侧


def test_every_transcript_line_of_one_run_carries_the_chain(tmp_path):
    result = run_analysis(QUESTION, str(LOGIN), outputs_root=tmp_path, trace_id=CHAIN)
    run_dir = Path(result["outputs_dir"])
    lines = _lines(run_dir)
    assert len(lines) > 3, f"这次运行只写了 {len(lines)} 行，样本太小证明不了什么"
    strays = [line.get("kind") for line in lines if line.get("trace_id") != CHAIN]
    assert not strays, f"这些记录没带上链路标识（或带错了）：{strays}"


def test_the_concurrent_layer_is_not_a_gap_in_the_chain(tmp_path, monkeypatch):
    """并发那一层（`_execute_dag` 的线程池）写下的行必须也带着同一个串。

    只数"所有行都带"是不够的：如果链路只在主线程绑定、任务线程里丢，那 `sender` 为
    orchestrator 的那些行照样全带，而**恰恰是任务级记录**（各角色的输入输出、工具审计）空着。
    所以这里点名"由角色写下的那些行"，并要求至少有一条——一条都没有就是这次运行没走到并发层，
    用例什么都没测（前置条件是借来的那一族）。
    """
    result = run_analysis(QUESTION, str(LOGIN), outputs_root=tmp_path, trace_id=CHAIN)
    lines = _lines(Path(result["outputs_dir"]))
    agent_lines = [line for line in lines if line.get("sender") and line["sender"] != "orchestrator"]
    assert agent_lines, "这次运行一条角色级转录都没写，用例测不到并发那一层"
    strays = [line["sender"] for line in agent_lines if line.get("trace_id") != CHAIN]
    assert not strays, f"这些由并发层写下的记录丢了链路标识：{sorted(set(strays))}"


def test_evaluation_json_carries_the_same_chain(tmp_path):
    """评测层读的是这个文件，不是 contextvar——所以它必须自己带一份。

    `run_id` 早就在这份文件里，但没有链路标识的话，"这次提交 → 哪份产物"只能在库里再跳一次；
    P6-4 那句"凭一个 id 查回产物"要能直接从产物侧成立。
    """
    result = run_analysis(QUESTION, str(LOGIN), outputs_root=tmp_path, trace_id=CHAIN)
    evaluation = _evaluation(Path(result["outputs_dir"]))
    assert evaluation["trace_id"] == CHAIN, {key: evaluation.get(key) for key in ("run_id", "trace_id")}
    # run_id 与 trace_id 是两件事（一次提交可能重试出多条 run），谁也不许冒充谁
    assert evaluation["run_id"] != CHAIN


def test_a_run_without_a_chain_omits_the_field_instead_of_writing_null(tmp_path):
    """CLI 与评测夹具本来就没有提交链路：那一格**缺席**，不写 null、更不写上一次的。

    这条同时是"认领循环不许留着上一个作业的值"那一句的产物侧版本——
    如果实现改成 `record["trace_id"] = trace.current()`（无脑赋值），这里会拿到一串 None；
    如果实现忘了清空，第二次运行会带上第一次的串，那在这份文件里就是两条无关运行被读成一条。
    """
    first = run_analysis(QUESTION, str(LOGIN), outputs_root=tmp_path / "a", trace_id=CHAIN)
    assert _lines(Path(first["outputs_dir"]))[0].get("trace_id") == CHAIN  # 前置条件自己摆

    second = run_analysis(QUESTION, str(LOGIN), outputs_root=tmp_path / "b")
    lines = _lines(Path(second["outputs_dir"]))
    assert lines, "第二次运行没写转录，这条用例就是空转"
    assert all("trace_id" not in line for line in lines), [line.get("kind") for line in lines if "trace_id" in line]
    assert "trace_id" not in _evaluation(Path(second["outputs_dir"]))


# ---------------------------------------------------------------- 事件与两种宿主


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app.db import init_db

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setenv("WEB_DISPATCH", "on")
    init_db()
    return tmp_path


def _login(client: TestClient, username: str = "admin", password: str = "admin") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def _wait_terminal(client: TestClient, job_id: str, within_s: float = 60.0) -> dict:
    """轮询到终态。**挂住不算抓到**：超时就带着最后一份读数红在断言上。"""
    deadline = time.monotonic() + within_s
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body.get("terminal"):
            return body
        time.sleep(0.4)
    raise AssertionError(f"{within_s}s 内没落到终态，最后一次读数：{body}")


def test_events_written_during_a_real_submission_carry_the_jobs_chain(env):
    """一次真实提交跑完之后，库里每条事件都要能对上作业行那一条链路。

    事件是"这条链路上发生过什么"的观测面（P2 把它落库就是为了这个）。如果只有作业行带 trace
    而事件不带，P6-4 那句"凭一个 id 查回排了多久、谁认领、重试几次"就只能查到一半——
    而且缺的正好是过程那半。
    """
    from app import eventlog

    with TestClient(app) as client:
        _login(client)
        dataset_id = client.post(
            "/api/datasets", files={"file": ("login_auth.csv", CSV.encode(), "text/csv")}
        ).json()["id"]
        submitted = client.post(
            "/api/analyze",
            json={"question": QUESTION, "dataset_id": dataset_id, "mode": "mock"},
            headers={"X-Trace-Id": "gateway-events-1"},
        ).json()
        _wait_terminal(client, submitted["job_id"])

        rows = eventlog.read_after(submitted["job_id"], 0)
        assert rows, "这次运行一条事件都没落库（那这条用例什么都没测）"
        strays = [(seq, event.get("type")) for seq, event in rows if event.get("trace_id") != "gateway-events-1"]
        assert not strays, f"这些事件没带上链路标识：{strays}"


def test_execute_job_binds_the_chain_from_the_row_whichever_host_runs_it(env):
    """两种宿主共用 `execute_job` 这一份代码（P2 的立场），所以链路的绑定也只在这一处。

    这里**不走 HTTP 路由**：手工建一条带 trace 的作业行，然后 `claim()` + `execute_job()`——
    这正是 `scripts/worker.py` 那条路。断言的是认领行里那一格真的带出来了（`claim` 的 SELECT
    要包含它），以及产物与事件两侧都绑上了。少了这一条，"独立 worker 进程也有链路"这句话
    就只有单进程形态的证据撑着。
    """
    from app import access, eventlog, queueing
    from app.db import execute, query_one
    from app.jobs import JobManager
    from app.runner import execute_job

    user = query_one("SELECT * FROM users WHERE username = 'admin'")
    assert user, "测试库里没有 admin，前置条件没摆出来"
    # 企业归属走**那一份判据**（`access.primary_org`），不在测试里手写 memberships 查询：
    # 手写一份就会与受理路径算出两个数，产物落的那棵树与用例查的那棵树对不上，红得没道理
    org_id = int(access.primary_org(user))
    inserted = execute(
        "INSERT INTO datasets (user_id, org_id, filename, path, size, row_count, columns) "
        "VALUES (?, ?, 'login_auth.csv', ?, 1, 1, '[\"ts\",\"user\",\"ip\",\"action\"]')",
        (user["id"], org_id, str(LOGIN)),
    )
    assert inserted is not None, "数据集行没插进去"
    dataset_id = int(inserted)

    job_id = "job_p62_worker_host"
    spec = {
        "question": QUESTION,
        "mode": "mock",
        "session_id": None,
        "pack": None,
        "mcp_approvals": {},
        "run_origin": {},
        "source_ref": f"dataset:{dataset_id}",
        "trace_id": "worker-host-chain-77",
    }
    queueing.accept(
        job_id=job_id,
        user_id=int(user["id"]),
        org_id=org_id,
        question=QUESTION,
        mode="mock",
        session_id=None,
        pack=None,
        spec=spec,
        trace_id="worker-host-chain-77",
    )
    manager = JobManager(sink=eventlog.append_event)
    claimed = queueing.claim("p62-test-worker")
    assert claimed and claimed["job_id"] == job_id, "没认领到这条作业（claim 的候选查询有问题）"
    # 这一格必须是认领行带出来的：worker 进程读的就是这一行，它没有别的来源
    assert claimed.get("trace_id") == "worker-host-chain-77", sorted(claimed)

    status = execute_job(claimed, manager)
    assert status in {"success", "partial", "degraded"}, f"这次运行没成：{status}"

    run_id = query_one("SELECT run_id FROM jobs WHERE job_id = ?", (job_id,))["run_id"]
    run_dir = Path(env) / "outputs" / "org" / str(org_id) / str(run_id)
    lines = _lines(run_dir)
    assert all(line.get("trace_id") == "worker-host-chain-77" for line in lines), [
        line.get("kind") for line in lines if line.get("trace_id") != "worker-host-chain-77"
    ]
    rows = eventlog.read_after(job_id, 0)
    assert rows and all(event.get("trace_id") == "worker-host-chain-77" for _seq, event in rows)
