"""P5-3：部署形态（Web 进程要不要自己认领作业）与它的可见性。

这一片测的不是"能不能拆进程"——`scripts/worker.py` 从 P2 就在，两进程实跑也有读数。
测的是**开关本身有没有牙**：

- 默认必须是今天这套能直接跑的形状（一个进程就够）。把默认改成 off 等于要求使用者先改
  环境才能让系统动起来，那是把形态切换的代价推给他。
- `off` 要真的让 Web 进程**一处都不认领**：只在 lifespan 判、提交路径照旧 `start()`，
  表现是"拆了而没拆干净"——Web 继续吃 CPU（P0 那笔登录 p95 8977.9→14378.3ms 的账），
  而运维以为它只受理。所以这里既测启动路径也测提交路径。
- 认不出来的值必须留在 on 并打一行：静默关掉执行的表现是"作业永远排队、接口全绿"，
  那是最难查的一类故障。
- 形态要**出现在读数里**（作业详情、SSE 首帧、worker 的起跑日志）。只写在启动日志里
  等于让排障的人去翻第一屏；而"这个进程有几个认领循环"报成"平台有几个循环"，
  分进程部署下就是差着进程数的一个假数。
- worker 进程也要自己定档一次（本轮实测出来的缺陷）：它不调 `apply_for_process()`，
  就会走 `get_gate()` 的懒建路径——那条路径故意不读预检缓存（读缓存是策略不是机制），
  于是"实测 16 路"在分进程部署下静默退成占位 4 路。作业不报错，只是每条都慢，
  而排队发生在没人看的那一侧。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentflow.core.llm_preflight import PROBE_VERSION, host_of, measured_concurrency

from app import llm_gate, queueing
from app.db import execute, init_db, query_one
from app.main import app
from app.routers import jobs as jobs_route
from app.runner import WEB_DISPATCH_ENV, dispatch_form, web_dispatch_enabled

CSV = "ts,user,ip,action\n2026-09-05T01:02:03Z,root,10.0.0.7,fail\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.delenv(WEB_DISPATCH_ENV, raising=False)
    init_db()
    return tmp_path


@pytest.fixture(autouse=True)
def _dispatcher_quiet():
    """模块级单例：上一个用例留下的认领循环会把这个用例的"没人认领"变成"有人认领"。

    进程级状态必须显式交接（与 `_fresh_rate_limits`、`_fresh_gate` 同一条纪律）。
    """
    jobs_route.dispatcher.stop()
    yield
    jobs_route.dispatcher.stop()


def _login(client: TestClient, username: str = "admin", password: str = "admin") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def _dataset(client: TestClient) -> int:
    response = client.post("/api/datasets", files={"file": ("login_auth.csv", CSV.encode(), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _submit(client: TestClient, dataset_id: int) -> str:
    response = client.post(
        "/api/analyze",
        json={"question": "失败登录次数最高的用户是谁？", "dataset_id": dataset_id, "mode": "mock"},
    )
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _wait_until(predicate, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _status(job_id: str) -> tuple[str, str | None]:
    row = query_one("SELECT status, worker FROM jobs WHERE job_id = ?", (job_id,))
    return str(row["status"]), row["worker"]


def _frames(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


# ---------------------------------------------------------------- 默认形态


def test_default_is_the_single_process_shape(env, monkeypatch):
    """没设任何环境变量时，Web 进程自己认领作业——这就是今天这套能跑的形状。"""
    monkeypatch.delenv(WEB_DISPATCH_ENV, raising=False)
    form = dispatch_form()
    assert form["dispatch_in_web"] is True, form
    assert "单进程" in form["shape"], form
    assert form["claim_loops"] == queueing.default_worker_concurrency(), form


def test_lifespan_and_submit_path_both_honour_off(env, monkeypatch):
    """off 要让**两条**开循环的路都闭嘴：lifespan 与每次提交。

    判据是行为而不是内部标志位：作业在两轮认领轮询（1s）之后仍然 `queued` 且没有 worker。
    只看 `_started` 会放过"标志没置上但线程已经起了"这类写法。
    """
    monkeypatch.setenv(WEB_DISPATCH_ENV, "off")
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        assert jobs_route.dispatcher._started is False, "lifespan 在 off 下仍开了认领循环"
        job_id = _submit(client, dataset_id)
        assert jobs_route.dispatcher._threads == [], "提交路径在 off 下仍起了线程——拆进程只拆了一半"

        stayed_queued = _wait_until(lambda: _status(job_id)[0] != "queued", timeout_s=2.0)
        assert not stayed_queued, (
            f"off 形态下作业被认领了（{_status(job_id)}）：受理层还在吃 CPU，"
            "而运维以为执行已经搬走了"
        )
        assert dispatch_form()["claim_loops"] == 0, dispatch_form()


def test_on_actually_runs_the_job(env, monkeypatch):
    """对照：同一条用例在默认形态下必须跑到终态。

    只测"off 不认领"会放过一类缺陷——把开关写成"永远不认领"，两条断言一样绿。
    """
    monkeypatch.setenv(WEB_DISPATCH_ENV, "on")
    with TestClient(app) as client:
        _login(client)
        dataset_id = _dataset(client)
        job_id = _submit(client, dataset_id)
        assert _wait_until(lambda: _status(job_id)[0] in jobs_route.TERMINAL_STATUSES), (
            f"默认形态下作业没跑完：{_status(job_id)}"
        )


@pytest.mark.parametrize("value", ["on", "ON", "  on  ", "1", "true", "yes", "web", "both"])
def test_the_accepted_spellings_for_on_all_mean_the_same(env, monkeypatch, value):
    """`.env.example` 里承诺过的写法必须都认。文档写了而代码不认，比少写一种更坏。"""
    monkeypatch.setenv(WEB_DISPATCH_ENV, value)
    assert web_dispatch_enabled() is True, value


@pytest.mark.parametrize("value", ["off", "OFF", "  off  ", "0", "false", "no", "external", "worker"])
def test_the_accepted_spellings_for_off_all_mean_the_same(env, monkeypatch, value):
    monkeypatch.setenv(WEB_DISPATCH_ENV, value)
    assert web_dispatch_enabled() is False, value


def test_unrecognised_value_stays_on_and_says_so(env, monkeypatch, caplog):
    """打错的值按 on 处理，并且必须留下一行日志。

    默认"认不出来就当 off"的表现是作业全排队、每个接口都返回 200——那是最难查的一类故障。
    """
    monkeypatch.setenv(WEB_DISPATCH_ENV, "ture")  # 手滑打错
    with caplog.at_level("WARNING", logger="agentflow.worker"):
        assert web_dispatch_enabled() is True
    assert any(WEB_DISPATCH_ENV in record.message for record in caplog.records), caplog.text


def test_claim_loops_describes_this_process_not_the_platform(env, monkeypatch):
    """`claim_loops` 在 off 时是 0，不是 `WORKER_CONCURRENCY` 那个数。

    报成后者就是把"这个进程几个循环"说成"平台几个循环"——分进程下差着进程数。
    """
    monkeypatch.setenv("WORKER_CONCURRENCY", "8")
    monkeypatch.setenv(WEB_DISPATCH_ENV, "off")
    assert dispatch_form()["claim_loops"] == 0, dispatch_form()
    monkeypatch.setenv(WEB_DISPATCH_ENV, "on")
    assert dispatch_form()["claim_loops"] == 8, dispatch_form()


def test_starting_twice_does_not_double_the_loops(env):
    """lifespan 起一次、提交路径再叫一次，只能有一套循环。

    两套就是同一个 job 被自己的两个线程抢：队列看起来更快，实际是重复执行。
    """
    with TestClient(app) as client:
        _login(client)
        _submit(client, _dataset(client))
        before = len(jobs_route.dispatcher._threads)
        jobs_route.dispatcher.start()
        assert len(jobs_route.dispatcher._threads) == before == queueing.default_worker_concurrency()


# ---------------------------------------------------------------- 形态要看得见


def test_form_is_visible_on_the_job_payload(env):
    """`/api/jobs/{id}` 带 `dispatch`。

    这一格同时验的是**响应模型没把它丢掉**：`JobOut` 没声明的键会被 pydantic 静默过滤，
    那时候接口仍然 200，只是运维要看的字段没了。
    """
    with TestClient(app) as client:
        _login(client)
        job_id = _submit(client, _dataset(client))
        payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["dispatch"]["dispatch_in_web"] is True, payload
    assert payload["dispatch"]["llm_gate_scope"] == "per_process", payload
    assert set(payload["dispatch"]) == {
        "dispatch_in_web",
        "shape",
        "claim_loops",
        "llm_gate_scope",
        "note",
    }, payload["dispatch"]


def test_form_is_in_the_first_sse_frame(env):
    """进度流第一帧带形态：`off` 而没人认领时，queued 一直涨、running 恒为 0，
    而用户停在这条流上——这一帧是唯一会说话的地方。"""
    with TestClient(app) as client:
        _login(client)
        job_id = _submit(client, _dataset(client))
        execute("UPDATE jobs SET status = 'success' WHERE job_id = ?", (job_id,))
        body = client.get(f"/api/jobs/{job_id}/events").text
    first = _frames(body)[0]
    assert first["type"] == "queue", body[:400]
    assert first["dispatch"]["dispatch_in_web"] is True, first


def test_the_claimer_survives_the_finished_job(env):
    """作业跑完之后也要查得到"是谁执行的"。

    `worker` 是租约的**当前持有者**，终态时被清空；分进程形态的归因（"这批 job 被谁吃掉了"）
    只能问 `claimed_by`。少了这一列，压测只能在作业活着的那几秒采样——实测那份报告里
    `jobs_by_actor` 是空的，连单进程那一轮都被念成"受理进程认领 0 个"，而那是假的读数。
    另一半是**不许外泄**：这一格是内部进程标识，接口响应里不能出现（与撤 `report_path`、
    把别人的 `user_id` 换成 `is_mine` 同一条口径）。
    """
    with TestClient(app) as client:
        _login(client)
        job_id = _submit(client, _dataset(client))
        assert _wait_until(lambda: _status(job_id)[0] in jobs_route.TERMINAL_STATUSES), _status(job_id)
        row = query_one("SELECT worker, claimed_by FROM jobs WHERE job_id = ?", (job_id,))
        assert row["worker"] is None, "终态还留着 worker：那是租约字段，跑完该清"
        assert row["claimed_by"], row
        assert str(row["claimed_by"]).count(":") == 2, f"不像 `主机:pid:尾` 的形状：{row}"
        payload = client.get(f"/api/jobs/{job_id}").json()
    assert "claimed_by" not in payload, "内部进程标识漏进了接口响应"


def test_queue_endpoint_carries_the_three_readings_and_no_rate_limits(env):
    """`GET /api/queue`：没提交过作业时界面也能问到这三份数，且**不**带限流额度。

    三份数各用一个函数（`queueing.stats` / `llm_gate.snapshot` / `dispatch_form`），
    这里断言的是"三格都在、且没有第四格从侧门出去"。`ratelimit` 的额度刻意排除：
    P4-3 定过 429 响应里不写限额（那等于替试探者标定天花板），一个登录用户可读的 GET
    把整份吐出去，就是从侧门把那条决定撤掉——所以这条断言按"整份 json 里搜不到那些键"来写，
    而不是只检查"我没返回它"。
    """
    with TestClient(app) as client:
        _login(client)
        body = client.get("/api/queue").json()
    assert set(body) == {"queue", "gate", "dispatch"}, body
    assert {"queued", "running", "stale_pending"} <= set(body["queue"]), body["queue"]
    assert body["gate"]["limit"] >= 1 and "limit_source" in body["gate"], body["gate"]
    assert body["dispatch"]["llm_gate_scope"] == "per_process", body["dispatch"]
    flat = json.dumps(body, ensure_ascii=False)
    for leak in ("login_per_user", "login_per_source", "account_create_per_actor", "evictions"):
        assert leak not in flat, f"入口限流的读数从 /api/queue 漏出去了：{leak}"
    assert TestClient(app).get("/api/queue").status_code == 401, "匿名可读"


# ---------------------------------------------------------------- worker 那一侧

ROOT = Path(__file__).resolve().parents[1]


def _measured_cache_file(target_dir: Path) -> dict:
    """在 `target_dir` 里造一份"这台站这个型号实测 16 路干净"的预检缓存。

    位置按子进程自己那套规则算（`$APP_DATA_DIR/llm_preflight.json`），不在测试里另立一份
    "缓存在哪"的式子——两处各写一遍，迟早出现"测试写的缓存服务读不到而照样绿"。
    """
    base_url, model = llm_gate.effective()
    report = {
        "probe_version": PROBE_VERSION,
        "base_host": host_of(base_url),
        "models": [],
        "concurrency_by_model": {
            model: {
                "model": model,
                "recommended_limit": 16,
                "measured_at": int(time.time()),
                "widths": [1, 4, 8, 16],
                "history": [],
            }
        },
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / llm_gate.CACHE_NAME).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    # 先证这份缓存**本身**能被读成 16：尺子没接上就断言服务行为，红会归错地方。
    assert measured_concurrency(report, base_url, model)[0] == 16
    return report


def _run_worker(target_dir: Path) -> str:
    """真的起一个 `scripts/worker.py` 子进程（`--once`，队列为空所以立刻返回）。

    用子进程而不是在测试进程里调 `main()`：这一片测的就是"另一个进程"，它的闸门与它读到的
    缓存本来也只能在那一侧观察；顺带躲开进程级单例被测试污染的账。实测一轮 0.8s。
    """
    env = dict(os.environ)
    env["APP_DATA_DIR"] = str(target_dir)
    env["OUTPUTS_ROOT"] = str(target_dir / "outputs")
    env["PYTHONIOENCODING"] = "utf-8"
    result = None
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "worker.py"), "--once"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT),
            timeout=90,
        )
    except subprocess.TimeoutExpired as expired:
        # 一个不自终止的 child 会把整片测试挂住（P5-3 那轮三位点 TIMEOUT 420s 就是这么废的），
        # 所以这里要有顶：到点就**大声失败**并把已经产生的输出带出来，而不是让 pytest 干等。
        raise AssertionError(
            f"scripts/worker.py --once 没在 90s 内退出。已产生输出：{str(expired.stdout)[:400]}"
        ) from expired
    assert result.returncode == 0, result.stderr[-500:]
    return result.stdout


def test_worker_process_applies_the_measured_gate(tmp_path):
    """worker 启动时要按**同一份策略**定档，否则实测档在分进程部署下退成占位值。

    少了 `apply_for_process()` 这一句，real 作业走的是 `get_gate()` 的懒建路径，而那条路径
    故意不读预检缓存（读缓存是策略不是机制）——表现不是报错，是每个 job 都比预期慢，
    而排队发生在没人看的那一侧。
    """
    _measured_cache_file(tmp_path / "wd")
    out = _run_worker(tmp_path / "wd")
    assert "LLM 闸门：16 路" in out, out
    assert "measured" in out, out


def test_worker_gate_line_names_the_per_process_scope(tmp_path):
    """起跑那行要写明"每进程各一份"。

    两个 worker 各 16 路 = 上游看到 32 路，而这台网关实测 32 路要出 429（2026-10-08 的读数）。
    不说这一句，"平台还是 16 路"就成了默认假设。
    """
    _measured_cache_file(tmp_path / "wd")
    out = _run_worker(tmp_path / "wd")
    assert "每进程各一份" in out, out
