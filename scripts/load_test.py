"""P0 负载尺子：把"撑不撑得住 100 并发"从形容词变成读数。

口径（先说清楚，否则数字会被读成别的东西）：

- **一律用 mock 模式**。对外服务当然是 real，但压测测的是**平台层**——受理、队列、
  数据库、事件流、worker 生命周期。拿 real 去压等于花真钱测第三方网关的并发上限，
  那个数只能由预检与配额决定，不该由这里决定。上游并发在 P4 里单独钉。
- 每轮记录的是**分布**（p50/p95/p99/max），不是平均值。100 并发的故事全在尾部。
- 观测项之外还有两条"坏消息计数器"：`database is locked` 出现次数，以及
  **中途杀进程后 job 的终态**（这条测的是持久性，不是速度——快的作业也可以永远丢结果）。

用法：

    python scripts/load_test.py --users 100 --rounds 1            # 自己起一座临时的塔
    python scripts/load_test.py --users 20 --rounds 2 --kill-mid   # 顺带测持久性
    python scripts/load_test.py --users 20 --web-dispatch off --extra-workers 2
                                                                   # 分进程形态（P5-3）：受理层不吃作业
    python scripts/load_test.py --users 40 --kill-after 8 --kill-target server --resilient-client on
                                                                   # 杀受理层 + 会重连的客户端（P5-4）

不碰开发机的真库：默认把 `APP_DATA_DIR` / `OUTPUTS_ROOT` 指到临时目录再拉起 uvicorn。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random as _random
import re
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402  # 驱动就用电机客户端：同一份请求形状，不自己造第二套协议

# 分位数与分布读数**只有这一把尺**：压测与上游并发探针（P4）比的是同一组 p50/p95/max，
# 各写一遍的话，两次改造的读数就没法放进同一张表里比了。
from agentflow.core.stats import percentile, shape  # noqa: E402,F401（load_test.shape 仍被用例引用）
from agentflow.core.streams import harden_streams  # noqa: E402
from app.queueing import TERMINAL as TERMINAL_STATUSES  # noqa: E402

# "还没跑完"的判据必须与队列用的是**同一份**名单：原来这里硬编码了三处、漏了 cancelled，
# 于是被取消的 job 在读数里永远是"在跑"。改一处队列口径而尺子跟着坏，比坏在代码里难查。
NON_TERMINAL_WHERE = "status NOT IN (" + ", ".join(f"'{state}'" for state in TERMINAL_STATUSES) + ")" 

CSV = ROOT / "demo" / "data" / "login_auth.csv"
QUESTION = "对2026-09-05的登录日志做安全审计，列出失败次数最高的账号"
PACK = "login_audit"

# ---------------------------------------------------------------- 韧性客户端（P5-4）
#
# 这份策略的**权威住在 `frontend/src/api/backoff.js`**——真客户端在那儿。压测里这一份是**镜像**：
# 尺子要回答"受理层被杀之后用户到底拿不拿得到结果"，就必须用同一个可重试集合与同一组退避参数，
# 否则"扛住了"是 Python 那半份自己扛的，与浏览器无关。两条相等守卫在 `tests/test_resilient_client.py`
# 上（按文本读回 backoff.js 的常量与集合）——镜像不配守卫迟早分叉，而分叉之后这份读数就是假的。
RETRIABLE_STATUS = frozenset({429, 502, 503, 504})
RETRY_BASE_S = 0.5
RETRY_CAP_S = 15.0
RETRY_MAX_ATTEMPTS = 8
RETRY_JITTER_RATIO = 0.3
# `--kill-when submit-in-flight` 的开杀条件：现场至少几个提交正在路上。
# 要 ≥2 是因为"只有一个在途"时，那一轮要么成功要么失败，看不出**重发**撞回同一条作业；
# 两个以上在途被同一刀打断，才会有"第一次已经落库、第二次重发拿到 replayed"这种形状。
SUBMIT_INFLIGHT_TO_KILL = 2


def retriable(exc: BaseException | None = None, status: int | None = None) -> bool:
    """这次失败值不值得再来一次。

    两条口径与 `shouldRetry` 逐字对齐：**没有状态**（连不上、被 reset）正是这一片要扛的那一类；
    401/403/404/422 一次都不重试——重试不会变好，只会把队列与上游再打一遍。
    """
    if status is None and exc is not None:
        return not isinstance(exc, asyncio.CancelledError)
    if status is None:
        return True
    return status in RETRIABLE_STATUS


def next_delay_s(attempt: int, rng: Any = None) -> float:
    """第 attempt 次失败之后等多久：指数增长 + 封顶 + 抖动（`rng` 让用例能钉住确定性值）。"""
    growth = RETRY_BASE_S * 2 ** max(0, attempt - 1)
    capped = min(RETRY_CAP_S, growth)
    random = rng.random if rng is not None else _random.random
    return round(capped + capped * RETRY_JITTER_RATIO * random(), 3)


def retry_after_s(response: Any) -> float:
    """服务端给的 `Retry-After` 优先于我们自己算的：限流与配额那两格知道还要等多久，我们不知道。"""
    if response is None:
        return 0.0
    raw = (response.headers.get("retry-after") or "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, min(60.0, seconds)) if raw else 0.0


async def resilient_call(task: Any, *, enabled: bool, sleep: Any = asyncio.sleep, ledger: dict | None = None):
    """按镜像策略重试一次异步调用；`enabled=False` 就是改造前的形状（一次不重试）。

    `task(attempt)` 必须**可安全重入**：对"创建作业"这类请求，这意味着重试带同一个幂等键
    （调用方负责生成一次），否则这个 helper 自己就是"把一次提问变成两个作业"的放大器。
    """
    attempt = 0
    while True:
        try:
            result = await task(attempt)
        except Exception as exc:  # noqa: BLE001 - 分类交给 retriable，最后原样抛回去
            if ledger is not None:
                ledger["retries"] = ledger.get("retries", 0) + 1
                kinds = ledger.setdefault("retry_kinds", {})
                kinds[type(exc).__name__] = kinds.get(type(exc).__name__, 0) + 1
            if not enabled or attempt + 1 >= RETRY_MAX_ATTEMPTS or not retriable(exc=exc):
                if ledger is not None:
                    ledger["abandoned"] = ledger.get("abandoned", 0) + 1
                raise
            attempt += 1
            response = getattr(exc, "response", None)
            await sleep(retry_after_s(response) or next_delay_s(attempt))
            continue
        status = getattr(result, "status_code", None)
        if not enabled or status is None or status < 400 or not retriable(status=status):
            return result
        if attempt + 1 >= RETRY_MAX_ATTEMPTS:  # 次数有顶：没有顶的退避就是无限重试
            return result
        if ledger is not None:
            ledger["retries"] = ledger.get("retries", 0) + 1
            kinds = ledger.setdefault("retry_kinds", {})
            kinds[f"http_{status}"] = kinds.get(f"http_{status}", 0) + 1
        attempt += 1
        await sleep(retry_after_s(result) or next_delay_s(attempt))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def seed_users(db_path: Path, count: int) -> list[str]:
    """直接写 users 表：这个产品没有自助注册（对外部署时是运维动作），压测也不该假装有一条注册接口。

    一次 PBKDF2 实测约 91ms（240k 轮）⇒ 100 个账号要 9 秒。这本身就是个读数，见报告里的
    `password_hash_ms`：登录风暴（token 过期后集体重登）在 CPU 上是真花钱的。
    """
    from app.security import hash_password

    started = time.perf_counter()
    hash_password("load-test-pw")
    per_hash_ms = (time.perf_counter() - started) * 1000

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    names = [f"loaduser{i:03d}" for i in range(1, count + 1)]
    try:
        for name in names:
            conn.execute("DELETE FROM users WHERE username = ?", (name,))
        conn.commit()
        for name in names:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
                (name, hash_password("load-test-pw")),
            )
        conn.commit()
    finally:
        conn.close()
    print(f"  种子账号 {count} 个（一次口令哈希 {per_hash_ms:.1f}ms）")
    return names, round(per_hash_ms, 2)


def spawn_server(port: int, data_dir: Path, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["APP_DATA_DIR"] = str(data_dir)
    env["OUTPUTS_ROOT"] = str(data_dir / "outputs")
    env["APP_SECRET"] = "load-test-secret-not-a-real-one"
    env["PYTHONIOENCODING"] = "utf-8"
    log = log_path.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "info"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return process, log


def spawn_worker(data_dir: Path, log_path: Path) -> tuple[subprocess.Popen, object]:
    """起一个独立 worker 进程（`scripts/worker.py`）。

    它与 Web 进程共用同一套环境变量：路径与库的权威是环境变量，所以"两个进程对同一份
    数据说话"这件事只有在这一层测才算测到（单元层里它们本来就是同一个进程）。
    """
    env = dict(os.environ)
    env["APP_DATA_DIR"] = str(data_dir)
    env["OUTPUTS_ROOT"] = str(data_dir / "outputs")
    env["APP_SECRET"] = "load-test-secret-not-a-real-one"
    env["PYTHONIOENCODING"] = "utf-8"
    log = log_path.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "worker.py")],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return process, log


def wait_ready(base: str, process: subprocess.Popen, timeout_s: float = 40.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"服务进程提前退出（退出码 {process.returncode}）——看日志尾部")
        try:
            if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                return
        except Exception:  # noqa: BLE001 - 就绪探测就是要吞下所有连接错误
            time.sleep(0.25)
    raise SystemExit("服务 40s 内没就绪")


async def one_user(
    client: httpx.AsyncClient,
    base: str,
    username: str,
    rounds: int,
    metrics: dict[str, list[float]],
    counters: dict[str, int],
    jobs_out: list[dict[str, Any]],
    sse_case: dict[str, Any] | None,
    resilient: bool = False,
) -> None:
    """一个用户的一轮：登录 → 上传 → 提交 → 轮询到终态。

    每一步都计时（失败也计时才有意义），传输层异常按"死在哪一步"记账——
    100 并发下服务端会 reset 连接，那是一条读数，不该让压测崩在栈上。

    `resilient` 决定这一轮用的是哪种客户端：off = 改造前的形状（一次都不重试，
    受理层被杀就等于这一轮观测中断）；on = 按 `backoff.js` 那份策略退避重试，并且
    **重试带同一个幂等键**。两种形状同一把尺各跑一遍，才叫 before/after。
    """
    phase = "login"
    try:
        started = time.perf_counter()
        login = await client.post(
            f"{base}/api/auth/login", json={"username": username, "password": "load-test-pw"}
        )
        metrics["login"].append(time.perf_counter() - started)
        if login.status_code != 200:
            counters["login_failed"] += 1
            return
        # 每个用户带自己的 Authorization 逐次传，不改共享 client 的头：共用一个 AsyncClient
        # 又互相覆盖 header ⇒ 提交时带的是别人的 token，归属过滤会把"读不到他的数据集"
        # 报成 404——那是量具在骗人（这一轮就先这样红过一次）。
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        phase = "upload"
        started = time.perf_counter()
        with CSV.open("rb") as fh:
            upload = await client.post(
                f"{base}/api/datasets", files={"file": (CSV.name, fh, "text/csv")}, headers=headers
            )
        metrics["upload"].append(time.perf_counter() - started)
        if upload.status_code not in (200, 201):
            counters["upload_failed"] += 1
            return
        dataset_id = upload.json()["id"]

        for _round in range(rounds):
            phase = "submit"
            started = time.perf_counter()
            # 杀点的触发器（`--kill-when first-submit`）：记录"第一个提交发出"的那一刻。
            counters.setdefault("first_submit_at", round(started, 3))
            # 韧性开的时候才带幂等键：**一次用户意图一个键，重试带同一个**。
            # 没有这把锁，"会重连的客户端"本身就是放大器——受理层被杀的那几秒里，
            # 一次提问会变成两个作业、双份上游调用。裸客户端那轮不带头（就是改造前的形状）。
            key = f"load_{username}_{_round}" if resilient else None
            submit_headers = {**headers, "Idempotency-Key": key} if key else headers

            async def _submit(_attempt: int):
                return await client.post(
                    f"{base}/api/analyze",
                    json={"question": QUESTION, "dataset_id": dataset_id, "mode": "mock", "pack": PACK},
                    headers=submit_headers,
                )

            counters["submit_inflight"] = counters.get("submit_inflight", 0) + 1
            try:
                submit = await resilient_call(_submit, enabled=resilient, ledger=counters)
            finally:
                # 这是**实时**计数（跑完归零，报告里那个 0 是收尾时刻的快照，不是"从没在途过"）：
                # `--kill-when submit-in-flight` 就是靠它决定"现在就杀，正有几个提交在路上"
                counters["submit_inflight"] -= 1
            metrics["submit"].append(time.perf_counter() - started)
            if submit.status_code != 200:
                counters["submit_failed"] += 1
                continue
            if submit.json().get("idempotency_replayed"):
                # 这一格就是"重试没有产生第二个作业"的直接证据，不靠数日志
                counters["submit_replays"] = counters.get("submit_replays", 0) + 1
            job_id = submit.json()["job_id"]
            if sse_case is not None and sse_case.get("job_id") is None:
                sse_case["job_id"] = job_id
                sse_case["token"] = token  # 只有一个用户负责把事件流跑一遍，见 read_stream
                # 重放探针要用**同一个键**再发一次，所以把这一发的键与数据集一起记下来
                sse_case["key"] = key
                sse_case["dataset_id"] = dataset_id

            phase = "poll"
            waited = 0.0
            status = "unknown"
            while waited < 600:
                poll = await resilient_call(
                    lambda _attempt: client.get(f"{base}/api/jobs/{job_id}", headers=headers),
                    enabled=resilient,
                    ledger=counters,
                )
                status = poll.json().get("status") or "unknown"
                # 终态名单用队列那一份（`TERMINAL_STATUSES`）：原来这里硬编码五个字面量、
                # 漏了 cancelled，于是被取消的 job 在读数里永远"在跑"。
                if status in TERMINAL_STATUSES:
                    break
                await asyncio.sleep(0.2)
                waited += 0.2
            metrics["e2e"].append(waited if waited else 0.2)
            if status in ("failed", "error"):
                counters["jobs_failed"] += 1
            if status not in TERMINAL_STATUSES:
                # 到 600s 还没落到终态：这是一笔**结果**，不是"没测到"。裸客户端那轮
                # （受理层被杀、没人重连）就应当记在这里。
                counters["jobs_unresolved"] = counters.get("jobs_unresolved", 0) + 1
            jobs_out.append({"job_id": job_id, "status": status, "user": username})
    except httpx.HTTPError as exc:
        counters["transport_failed"] += 1
        by_phase = counters.setdefault("transport_by_phase", {})
        by_phase[phase] = by_phase.get(phase, 0) + 1
        kinds = counters.setdefault("transport_kinds", {})
        kinds[type(exc).__name__] = kinds.get(type(exc).__name__, 0) + 1
        if phase == "poll":
            # 退避用完还没读到终态 ⇒ 这一轮**用户没拿到结果**，那是一笔结果。
            # 第一版只在"轮询循环正常跑完但状态非终态"那条记 `jobs_unresolved`，
            # 于是最该记的一群（放弃在重试上）反而没进账——那是把"丢了"读成"没测"。
            counters["jobs_unresolved"] = counters.get("jobs_unresolved", 0) + 1
            jobs_out.append({"job_id": None, "status": f"unresolved_in_{phase}", "user": username})
        return


async def read_stream(base: str, token: str, job_id: str) -> dict[str, Any]:
    """事件流这一段不测吞吐，测三件会骗人的事：续得上吗、续上时重不重放、完成后还读得到吗。

    游标必须是**服务端给的 `id:`**（浏览器就是这么记的），不是"data 行数"：
    第一帧 `queue` 没有 id，用行数当游标会多要一位，于是永远"重复一条"——
    这条红在尺子上，不在系统上（上一轮就是这么读的，读数已作废重测）。
    """
    headers = {"Authorization": f"Bearer {token}"}
    first: list[str] = []
    last_id = 0
    try:
        async with httpx.AsyncClient(timeout=30) as probe:
            async with probe.stream("GET", f"{base}/api/jobs/{job_id}/events", headers=headers) as response:
                async for line in response.aiter_lines():
                    if line.startswith("id: "):
                        last_id = int(line[4:])
                    elif line.startswith("data: "):
                        first.append(line[6:])
                        if len(first) >= 4:
                            break  # 故意断开：模拟用户切页面、网络掉线
    except Exception as exc:  # noqa: BLE001 - 流的坏形状就是要如实记下来
        return {"phase1_frames": len(first), "last_event_id": last_id, "reconnect_error": type(exc).__name__}

    replayed: list[str] = []
    resumed_from = last_id
    async with httpx.AsyncClient(timeout=20) as again:
        try:
            async with again.stream(
                "GET",
                f"{base}/api/jobs/{job_id}/events",
                headers={**headers, "Last-Event-ID": str(resumed_from)},
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        replayed.append(line[6:])
                        if len(replayed) >= 8:
                            break
        except Exception:  # noqa: BLE001
            pass
    # 每次建流都会先发一条 `queue` 元帧（连接自己的深度，不属于历史）。
    # 把它算进"重播"就是尺子在骗人：上一条读数就是这么留下的 1 条重复。
    def _history(frames: list[str]) -> set[str]:
        keep = set()
        for raw in frames:
            try:
                if json.loads(raw).get("type") == "queue":
                    continue
            except Exception:  # noqa: BLE001 - 解析不了的照样算历史，别悄悄丢
                pass
            keep.add(raw)
        return keep

    duplicates = len(_history(first) & _history(replayed))
    return {
        "phase1_frames": len(first),
        "last_event_id": last_id,
        "resumed_from": resumed_from,
        "phase2_frames": len(replayed),
        "duplicated_frames": duplicates,
        # 认 Last-Event-ID 的判据：续读不重播，且确实拿到了后面的帧
        "honors_last_event_id": duplicates == 0 and len(replayed) > 0,
    }


async def replay_probe(client: httpx.AsyncClient, base: str, case: dict[str, Any]) -> dict[str, Any]:
    """跑完补一枪：**已经用过的幂等键再发一次**，看它撞回哪条作业。

    为什么不靠"杀在提交那一瞬"去撞：那条路要的是"第一次已经落库、响应正好丢了"这个毫秒级
    窗口，五轮实验里 `rows_at_kill` 最大才到 0（那一刀落下时库里一行都还没有），
    `submit_replays` 一直是 0 —— 那不是承诺没生效，是**没打到那一瞬**。所以这一格改成
    主动探针：它测的是同一件事（同一个键不产生第二个作业），但走的是真 HTTP、真库、
    刚被压过的现场，而不是 TestClient。
    """
    key, token, dataset_id = case.get("key"), case.get("token"), case.get("dataset_id")
    if not key or not token or not dataset_id:
        return {"skipped": True, "reason": "这一轮没有带键的提交（裸客户端形状），没有可复用的键"}
    before = idempotency_reading(Path(os.environ["LOAD_DB"]))
    try:
        response = await client.post(
            f"{base}/api/analyze",
            json={"question": QUESTION, "dataset_id": dataset_id, "mode": "mock", "pack": PACK},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    after = idempotency_reading(Path(os.environ["LOAD_DB"]))
    body = response.json() if response.status_code == 200 else {}
    return {
        "status_code": response.status_code,
        "same_job_id": body.get("job_id") == case.get("job_id"),
        "flagged_replayed": body.get("idempotency_replayed"),
        "rows_before": before.get("rows"),
        "rows_after": after.get("rows"),
        "rows_unchanged": before.get("rows") == after.get("rows"),
        "duplicates": after.get("duplicates"),
    }


async def concurrency_probe(db_path: Path, holder: dict[str, int], stop: asyncio.Event) -> None:
    """盯 jobs 表里非终态的行数：这是"同时在跑"的直接证据，比猜 worker 数诚实。"""
    while not stop.is_set():
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            live = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {NON_TERMINAL_WHERE}").fetchone()[0]
            conn.close()
        except sqlite3.OperationalError:
            # 读侧撞锁也要计数：它和"服务端 locked 行数"是两个来源，都要留
            holder["locked_reads"] = holder.get("locked_reads", 0) + 1
            live = 0
        except Exception:  # noqa: BLE001
            live = 0
        holder["max_live_jobs"] = max(holder.get("max_live_jobs", 0), int(live))
        holder["samples"] = holder.get("samples", 0) + 1
        # 杀过之后同一条曲线要继续盯：`63 个在杀的那一刻还没跑完` 与 `最后一个是 0` 之间
        # 得有过程，否则"排空了"只是"等到轮询都结束了才看一眼"。
        if holder.get("killed_at_s"):
            holder["max_live_after_kill"] = max(holder.get("max_live_after_kill", 0), int(live))
            holder["last_live_after_kill"] = int(live)
        try:  # 谁在跑：distinct worker 数 > 1 就是"多进程消费"的证据，比数线程诚实
            probe = sqlite3.connect(db_path, timeout=5)
            seen = {row[0] for row in probe.execute("SELECT DISTINCT worker FROM jobs WHERE worker IS NOT NULL")}
            running = probe.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            probe.close()
            # **累加**而不是覆盖：`worker` 在 job 跑完时被清空，最后一次采样恰好在全线结束后
            # 跑到，快照就成了空集——第一次的读数就是这么把"两进程消费"报成"0 岔"的。
            names = holder.setdefault("claimer_names", set())
            names.update(seen)
            holder["claimers"] = sorted(names)
            holder["max_running_rows"] = max(holder.get("max_running_rows", 0), int(running))
        except Exception as error:  # noqa: BLE001 - 采样失败不许静默：静默的读数会被当成"没有多进程"
            holder["claimer_probe_errors"] = holder.get("claimer_probe_errors", 0) + 1
            holder["claimer_probe_first_error"] = f"{type(error).__name__}: {error}"[:120]
        await asyncio.sleep(0.2)


_SERVER_PID_RE = re.compile(r"Started server process \[(\d+)\]")
_WORKER_ID_RE = re.compile(r"worker 起跑：(\S+)")


def _pid_in_name(name: str) -> str:
    """`主机:进程号:随机尾` 里取那个进程号。取不到就回空串，让调用方按"认不出来"处理。"""
    parts = str(name or "").split(":")
    return parts[1] if len(parts) >= 2 and parts[1].isdigit() else ""


def idempotency_reading(db_path: Path) -> dict[str, Any]:
    """库里"同一个幂等键对应几行作业"的直接检查——P5-1 那句承诺在负载形态下的读数。

    单元层已经验过并发重试只建一行，但那是一个受控的小场景；这里要的是**另一件事**：
    受理层真的被打断过（提交在途时被杀）、客户端真带同一个键重发过之后，
    库里仍然没有第二个作业。`rows_with_key > distinct_pairs` 就是"重试产生了第二个作业"的直接证据。

    **比的单位是 `(用户, 键)` 这一对，不是键本身**：幂等键的作用域是用户（库层唯一索引就建在
    这两列上），只按键去重的话，两个人各用 `k1` 提交两次会被算成"重复了一行"——那是把
    别人的合法提交读成自己的放大，与"拿列位置当身份"同族。
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        rows, keyed = conn.execute(
            "SELECT COUNT(*), COUNT(idempotency_key) FROM jobs"
        ).fetchone()
        pairs = conn.execute(
            "SELECT COUNT(*) FROM (SELECT user_id, idempotency_key FROM jobs "
            "WHERE idempotency_key IS NOT NULL GROUP BY user_id, idempotency_key)"
        ).fetchone()[0]
        worst = conn.execute(
            "SELECT user_id, idempotency_key, COUNT(*) AS n FROM jobs WHERE idempotency_key IS NOT NULL "
            "GROUP BY user_id, idempotency_key ORDER BY n DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return {
        "rows": int(rows or 0),
        "rows_with_key": int(keyed or 0),
        "distinct_user_key_pairs": int(pairs or 0),
        # 一个 (用户, 键) 最多对应几行：1 = 承诺成立；>1 = 重发造出了第二个作业
        "max_rows_per_pair": int(worst[2]) if worst else 0,
        "worst_pair": (f"user {worst[0]} / {worst[1]}" if worst else None),
        "duplicates": max(0, int(keyed or 0) - int(pairs or 0)),
    }


def claim_attribution(db_path: Path, server_logs: list[Path], worker_logs: list[Path]) -> dict[str, Any]:
    """把 `jobs.worker` 里那些名字**归到具体进程**上，回答"这批 job 是谁认领的"。

    为什么不能拿 `Popen.pid` 判：这台机器上 `python.exe` 是启动器，真正的解释器是它的子进程
    （实测 Popen.pid=24208，而 uvicorn 自己报 "Started server process [30168]"）。
    原来那行摘要按"distinct 名字数 > 1 才算多进程"来判，于是 **`WEB_DISPATCH=off` + 一个外部
    worker** 这一轮正是要看的形态被念成"只有受理进程"——名字确实只有一个，而那一个恰恰不是受理进程。
    判据因此改成按 pid 点名，并且数的是 **`claimed_by`** 这一列而不是 `worker`：后者是租约的
    当前持有者，作业跑完就清空（实测：压测结束后按 `worker` 归因得到的是一份空表，
    连单进程那一轮都被念成"受理进程认领 0 个"，而那是假的）。`claimed_by` 是留痕，跑完还在。
    """
    pids: dict[str, str] = {}
    # **每一份**受理层日志都要读：`--restart-server-after` 之后那座塔写的是另一个日志文件，
    # 只读第一份的话，重启后的认领者会被归成 `unknown`（今天实测到 `{"unknown": 2}`，
    # 那不是有个来路不明的进程在吃作业，是重启后的受理层没被认出来——同一角色，两份日志）。
    for server_log in server_logs:
        try:
            match = _SERVER_PID_RE.search(server_log.read_text(encoding="utf-8", errors="replace"))
            if match:
                pids[match.group(1)] = "web_process"
        except OSError:
            pass
    for index, path in enumerate(worker_logs):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _WORKER_ID_RE.finditer(text):
            pid = _pid_in_name(match.group(1))
            if pid:
                pids[pid] = f"worker_{index}"
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        rows = conn.execute(
            "SELECT claimed_by, COUNT(*) FROM jobs WHERE claimed_by IS NOT NULL GROUP BY claimed_by"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        rows = []
    by_actor: dict[str, int] = {}
    unknown: list[str] = []
    for name, count in rows:
        actor = pids.get(_pid_in_name(name))
        if actor is None:
            unknown.append(str(name))
            actor = "unknown"
        by_actor[actor] = by_actor.get(actor, 0) + int(count)
    return {
        "jobs_by_actor": by_actor,
        "process_pids": pids,
        "unattributed_names": unknown,
        # 受理进程一次都没认领 = 拆分真的生效了。`web_process` 出现在这里就说明"拆了没拆干净"。
        "web_process_claimed": by_actor.get("web_process", 0),
    }


def wait_for_drain(db_path: Path, wait_s: float) -> dict[str, Any]:
    """杀进程之后不重启任何人，看队列能不能自己排空。

    判据是"期间没有任何人帮它重启"：只有在这种条件下 drained 才叫崩溃恢复，
    而不是"我们把服务又拉起来了"。
    """
    started = time.monotonic()
    while time.monotonic() - started < wait_s:
        if non_terminal(db_path) == 0:
            return {"drained_without_help": True, "seconds": round(time.monotonic() - started, 1)}
        time.sleep(1.0)
    return {"drained_without_help": False, "seconds": round(time.monotonic() - started, 1),
            "still_non_terminal": non_terminal(db_path)}


def non_terminal(db_path: Path) -> int:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {NON_TERMINAL_WHERE}"
        ).fetchone()[0])
    finally:
        conn.close()


async def drive(
    users,
    base,
    rounds,
    concurrency,
    server=None,
    kill_after_s=None,
    victim=None,
    resilient: bool = False,
    restart_server_after_s: float | None = None,
    kill_on_submit_inflight: bool = False,
    submit_inflight_to_kill: int = SUBMIT_INFLIGHT_TO_KILL,
):
    metrics: dict[str, list[float]] = {"login": [], "upload": [], "submit": [], "e2e": []}
    counters = {
        "login_failed": 0, "upload_failed": 0, "submit_failed": 0, "jobs_failed": 0,
        "transport_failed": 0,
        # 这几格先建好再记账：**缺键与零是两件事**——缺键会被读成"这一轮没测"，
        # 而零才是"测了，一次都没发生"。韧性对照的核心证据全在这里。
        "retries": 0, "abandoned": 0, "submit_replays": 0, "jobs_unresolved": 0,
    }
    jobs_out: list[dict[str, Any]] = []
    sse_case: dict[str, Any] = {"job_id": None, "token": None}
    # 全量用户同时冲会先把连接池打爆，那测的不是平台的容量而是 httpx 的默认值：
    # 连接池按用户数给，够大才叫"100 个客户端同时压"。
    limits = httpx.Limits(max_connections=len(users) + 20, max_keepalive_connections=len(users) + 20)
    async with httpx.AsyncClient(limits=limits, timeout=60) as client:
        # 先拿一个 token 给探针与事件流用（admin 口令在压测实例里由环境变量给死）
        login = await client.post(f"{base}/api/auth/login", json={"username": users[0], "password": "load-test-pw"})
        token = login.json().get("token") if login.status_code == 200 else None
        sse_case["token"] = token

        holder: dict[str, int] = {}
        stop = asyncio.Event()
        probe_task = None
        if token:
            probe_task = asyncio.create_task(concurrency_probe(Path(os.environ["LOAD_DB"]), holder, stop))

        semaphore = asyncio.Semaphore(concurrency)

        async def guarded(name: str) -> None:
            async with semaphore:
                await one_user(client, base, name, rounds, metrics, counters, jobs_out, sse_case, resilient)

        async def killer_task() -> None:
            """中途杀进程：测的是持久性，不是速度——快的系统也可以永远丢结果。

            `victim` 可以是某个 worker 进程：那样测的就是"Web 还活着、执行的人死了"，
            这正是 P0 那条读数（28 个 job 永远停在非终态）的形态。
            计数必须在**杀之前一刻**取：等到整轮 gather 结束再取，那时用户都走完了，
            非终态必然是 0——"恢复了"就变成一句平凡真，而不是证据。

            `restart_server_after_s` 是给 P5-4 那一对照加的：**受理层必须在客户端还在退避的
            窗口里回来**，否则两种客户端形状都会耗尽 8 次重试，读出来是"韧性没用"——
            第一版就是这么读的（`transport_failed` 两边都是 39，而 retries=312）。
            那不是系统没恢复，是**服务根本没回来**。默认不重启，保持 P2 那种"没人帮忙"的形状。

            `kill_on_submit_inflight` 解决另一件事：按秒排的"第 N 秒杀"打不到我要的那一段。
            今天想测"提交在途时被杀 ⇒ 客户端带同一个幂等键重发"，试了第 2 秒杀（37 次失败全在
            **上传**）、又试了"第一个提交之后 +1s"（那 10 个提交早就落库完了，剩下的人还在上传），
            `submit_replays` 两次都是 0——那不是"不会重发"，是**没打到那一瞬**。
            所以杀点改成等事件本身：**现场有 ≥2 个提交正在路上**那一刻起算 `kill_after_s` 秒。
            """
            if kill_after_s is None:
                return
            if kill_on_submit_inflight:
                waited = 0.0
                while counters.get("submit_inflight", 0) < submit_inflight_to_kill and waited < 90:
                    await asyncio.sleep(0.02)
                    waited += 0.02
                inflight = counters.get("submit_inflight", 0)
                holder["kill_clock"] = (
                    f"现场 {inflight} 个提交在途时开杀（等了 {round(waited, 2)}s，再延后 {kill_after_s}s）"
                    if inflight >= submit_inflight_to_kill
                    else f"等不到 {submit_inflight_to_kill} 个提交同时在途（90s 超时，现场 {inflight} 个）"
                )
            await asyncio.sleep(kill_after_s)
            holder["killed_at_s"] = 1
            holder["non_terminal_at_kill"] = non_terminal(Path(os.environ["LOAD_DB"]))
            # 杀的那一刻库里已经有几行作业。没有这一格，"重放那格是 0"就有两种读法：
            # 一种是"没有提交来得及落库"（那是没打到），另一种是"落库了但响应也丢了，
            # 重发新建了一行"（那是承诺破了）。两者必须分得开。
            try:
                probe = sqlite3.connect(os.environ["LOAD_DB"], timeout=5)
                holder["rows_at_kill"] = int(probe.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                probe.close()
            except sqlite3.Error:
                holder["rows_at_kill"] = -1  # -1 = 取不到，不许当成 0
            target = victim if victim is not None else server
            if target is not None and target.poll() is None:
                target.kill()
            if restart_server_after_s and target is server:
                await asyncio.sleep(restart_server_after_s)
                port = int(os.environ["LOAD_PORT"])
                restarted, restart_log = spawn_server(
                    port, Path(os.environ["APP_DATA_DIR"]), restart_log_path(Path(os.environ["LOAD_DB"]))
                )
                # 等它真的能接请求再记账：拉起来 ≠ 可用，早一秒重试都会撞上"连接被拒"，
                # 于是把"服务还没起来"混进"客户端不会重连"里。
                wait_ready(base, restarted, timeout_s=40)
                holder["server_back_at_s"] = round(kill_after_s + restart_server_after_s, 1)
                holder["restarted_process"] = (restarted, restart_log)

        await asyncio.gather(
            *(guarded(name) for name in users),
            killer_task(),
        )
        stop.set()
        if probe_task:
            await probe_task
    # 这一轮被 killer_task 拉回来的那座塔要收掉：它不属于 `main()` 的清理名单，
    # 留在后台就会污染下一次跑（端口相同 ⇒ 下一次直接连到上一轮的进程上）。
    restarted = holder.get("restarted_process")
    if restarted:
        restarted[0].kill()
        try:
            restarted[1].close()
        except OSError:
            pass

    stream = {"skipped": True}
    if sse_case.get("job_id") and sse_case.get("token"):
        stream = await read_stream(base, sse_case["token"], sse_case["job_id"])
    stream["job_completed_first"] = True

    # 重放探针（跑完之后补一枪）。放在整轮结束之后是因为它要的是"现场已被压过、库里已有行"，
    # 那时再发同一个键，才看得见"撞回同一条作业"还是"又建了一行"。
    async with httpx.AsyncClient(timeout=30) as probe_client:
        replay = await replay_probe(probe_client, base, sse_case)

    return {
        "users": len(users),
        "rounds": rounds,
        "login_ms": shape("login", [v * 1000 for v in metrics["login"]]),
        "upload_ms": shape("upload", [v * 1000 for v in metrics["upload"]]),
        "submit_ms": shape("submit", [v * 1000 for v in metrics["submit"]]),
        "e2e_s": shape("e2e", metrics["e2e"]),
        "max_live_jobs": holder.get("max_live_jobs", 0),
        "non_terminal_at_kill": holder.get("non_terminal_at_kill"),
        "max_live_after_kill": holder.get("max_live_after_kill", 0),
        "last_live_after_kill": holder.get("last_live_after_kill"),
        "claimers": holder.get("claimers", []),
        # 服务什么时候回来的（没有重启就是 None）：**这一格决定"客户端没扛住"该归给谁**——
        # 服务根本没回来时，两种客户端形状的读数本来就该一样。
        "server_back_at_s": holder.get("server_back_at_s"),
        # 杀点是由时钟定的还是由事件定的：`--kill-after 2` 这种秒数会打中上传而不是提交，
        # 那时"没触发"是**没打到**，不是"不会发生"——不写这一格，下一次还会把 0 读成结论。
        "kill_clock": holder.get("kill_clock"),
        "rows_at_kill": holder.get("rows_at_kill"),
        "replay_probe": replay,
        "claimer_probe": {
            "samples": holder.get("samples", 0),
            "errors": holder.get("claimer_probe_errors", 0),
            "first_error": holder.get("claimer_probe_first_error", ""),
            "max_running_rows": holder.get("max_running_rows", 0),
        },
        "probe_locked_reads": holder.get("locked_reads", 0),
        "sse": stream,
        "counters": counters,
        "jobs": jobs_out[:5],
    }


def harvest_locks(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return text.count("database is locked")


def recovery_reading(
    victim_label: str,
    db_path: Path,
    *,
    wait_s: float,
    may_restart_server: bool,
    at_kill: int | None = None,
) -> dict[str, Any]:
    """崩溃恢复的读数：**先不帮它**，看队列能不能自己排空。

    杀掉 worker 时不许重启任何人——那才是"执行的人死了而平台还在受理"的真实形态；
    杀掉 Web 进程时允许再拉一次服务，那时读的是"重启后有没有人接手"。
    两种情形分开报，不许合成一句"恢复了"。

    `at_kill` 必须是**杀那一刻**的计数（由 killer_task 传进来）；在这里现取只会得到 0，
    因为用户轮询都跑完了才走到这个函数——那条平凡真的读数我第一次就差点这么存档。
    """
    reading: dict[str, Any] = {
        "killed": victim_label,
        "lease_seconds": int(os.getenv("JOB_LEASE_SECONDS", "90")),
        "max_attempts": int(os.getenv("JOB_MAX_ATTEMPTS", "3")),
        "non_terminal_when_killed": at_kill if at_kill is not None else non_terminal(db_path),
    }
    reading["counted_at"] = "kill moment" if at_kill is not None else "end of run（读数无意义）"
    reading.update(wait_for_drain(db_path, wait_s))
    if not reading.get("drained_without_help") and may_restart_server:
        port = int(os.environ["LOAD_PORT"])
        restarted, restart_log = spawn_server(port, Path(os.environ["APP_DATA_DIR"]), restart_log_path(db_path))
        wait_ready(f"http://127.0.0.1:{port}", restarted, timeout_s=40)
        reading["after_server_restart"] = wait_for_drain(db_path, wait_s)
        restarted.kill()
        restart_log.close()
    return reading


def restart_log_path(db_path: Path) -> Path:
    return db_path.parent / "server_restart.log"


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 平台负载尺子（mock 模式，不烧上游）")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=0, help="同时发起的用户数，默认=users")
    parser.add_argument("--kill-after", type=float, default=None, help="跑到第 N 秒时杀掉一个进程")
    parser.add_argument("--kill-target", choices=("server", "worker"), default="server",
                        help="杀掉谁：server=受理层，worker=执行层（P2 的崩溃恢复测的是后者）")
    parser.add_argument("--extra-workers", type=int, default=0, help="另起几个独立 worker 进程（scripts/worker.py）")
    parser.add_argument("--web-dispatch", choices=("on", "off"), default="on",
                        help="Web 进程要不要自己认领作业：off = P5-3 的分进程形态（受理层只受理，"
                             "执行只在 --extra-workers 起的那些进程里）")
    parser.add_argument("--resilient-client", choices=("on", "off"), default="off",
                        help="驱动客户端会不会退避重试：off = 改造前的形状（受理层被杀就等于这一轮"
                             "观测中断），on = 按 frontend/src/api/backoff.js 那份策略重试且"
                             "重试带同一个幂等键。P5-4 的 before/after 就是这两个形状各跑一遍")
    parser.add_argument("--restart-server-after", type=float, default=None,
                        help="杀掉受理层之后过几秒把它再拉起来（默认不拉，保持 P2 那种"
                             "『没人帮忙』的形状）。P5-4 的对照必须给一个数：服务不在客户端"
                             "还在退避的窗口里回来，两种形状的读数会一模一样")
    parser.add_argument("--kill-when", choices=("timer", "submit-in-flight"), default="timer",
                        help="第 N 秒杀是按秒算的时钟，还是等现场有 --kill-inflight 个提交正在路上时开杀。"
                             "想测'提交在途时被杀 ⇒ 带同一个幂等键重发'必须用 first-submit："
                             "按秒的时钟会打中上传（今天实测 37 次失败全在 upload），"
                             "于是 submit_replays=0 被误读成'不会重发'")
    parser.add_argument("--kill-inflight", type=int, default=SUBMIT_INFLIGHT_TO_KILL,
                        help="配合 --kill-when submit-in-flight：现场至少几个提交在途才开杀。太少先后各一，看不见重放撞回同一条作业")
    parser.add_argument("--lease-seconds", type=int, default=30, help="租约时长；测试里压短，否则要等满 90s")
    parser.add_argument("--recover-wait", type=float, default=120.0, help="杀完之后最多等多久看队列自己排空")
    parser.add_argument("--report", default=str(ROOT / ".appdata" / f"load_{int(time.time())}.json"))
    args = parser.parse_args()
    # Windows 上 stdout 被重定向时按本地码页编码，打印 `⇒`/`✘` 这类字符会在跑完之后炸栈
    # （缺陷 #39 就是这么来的）。守这条的用例是 tests/test_scripts_encoding.py。
    harden_streams()

    if not CSV.exists():
        raise SystemExit(f"夹具不在：{CSV}")

    data_dir = Path(tempfile.mkdtemp(prefix="agentflow_load_"))
    db_path = data_dir / "app.db"
    port = free_port()
    os.environ["APP_DATA_DIR"] = str(data_dir)
    os.environ["OUTPUTS_ROOT"] = str(data_dir / "outputs")
    os.environ["LOAD_DB"] = str(db_path)
    os.environ["LOAD_PORT"] = str(port)
    # 租约与认领上限走环境变量：子进程（uvicorn 与 scripts/worker.py）读的就是同一份口径，
    # 这里压到 30s 是为了让"崩溃→收回→重认领"在一次压测里可读，而不是等满默认 90s。
    os.environ["JOB_LEASE_SECONDS"] = str(args.lease_seconds)
    # 形态也走环境变量：子进程（uvicorn）里的 `app.runner.web_dispatch_enabled()` 读的就是这一份，
    # 压测脚本不在自己进程里判形态——两处各判一次，迟早出现"脚本以为拆了、服务其实没拆"。
    os.environ["WEB_DISPATCH"] = args.web_dispatch
    if args.web_dispatch == "off" and args.extra_workers < 1:
        # 不拦：这恰好是要能测的一格（受理层活着、没人认领）。但读数必须带着它是什么形态，
        # 否则将来看到"作业全停在 queued"会被当成队列的缺陷，而它是这次配置的形状。
        print("  ⚠ --web-dispatch off 且没起外部 worker：作业会一直停在队列里（这是形态，不是故障）")
    log_path = data_dir / "server.log"

    print(f"负载尺子 ｜ 用户 {args.users} ｜ 每人 {args.rounds} 次 ｜ 端口 {port} ｜ 数据目录 {data_dir}")
    # 先建 schema 再种子账号：init_db 在服务的 lifespan 里也会跑一次，但那时已经晚于
    # 我们往同一份库里插用户。环境变量先设好，两边看到的是同一个 DB_PATH。
    from app.db import init_db

    init_db()
    names, hash_ms = seed_users(db_path, args.users)
    process, log = spawn_server(port, data_dir, log_path)
    workers: list[tuple[subprocess.Popen, object]] = []
    base = f"http://127.0.0.1:{port}"
    try:
        wait_ready(base, process)
        for index in range(args.extra_workers):
            workers.append(spawn_worker(data_dir, data_dir / f"worker_{index}.log"))
        if args.kill_after and args.kill_target == "worker" and not workers:
            raise SystemExit("--kill-target worker 要配 --extra-workers >= 1")
        if workers:
            time.sleep(1.0)  # 等 worker 把起跑日志打出来，别让启动竞态混进"谁认领的"
        victim = process
        if args.kill_after and args.kill_target == "worker":
            victim = workers[0][0]
        result = asyncio.run(
            drive(
                names, base, args.rounds, args.concurrency or args.users, process,
                args.kill_after, victim, args.resilient_client == "on", args.restart_server_after,
                args.kill_when == "submit-in-flight", args.kill_inflight,
            )
        )
        # 客户端形状跟着读数落盘：同一把尺两种形状跑出来的两份报告，只有带着这一格才能对上账
        # （否则下一天翻到这两份 json，谁也不知道哪份是"会重连的"）。
        result["client_shape"] = args.resilient_client
        result["processes"] = {
            "server": process.pid,
            "workers": [item[0].pid for item in workers],
            "worker_concurrency_per_process": int(os.getenv("WORKER_CONCURRENCY", "2")),
            # 这一轮的形态要跟着读数落盘。没有它，一份"queued 一直涨、running 恒为 0"的报告
            # 会被下一个人读成系统坏了，而不是"这一轮就是关掉受理层认领跑的"。
            "web_dispatch": args.web_dispatch,
        }
        result["claimers_seen"] = result.get("claimers", [])
        if args.kill_after:
            # 先不帮它：只有"期间没人重启"这条成立，排空才叫崩溃恢复。
            result["durability"] = recovery_reading(
                args.kill_target,
                db_path,
                wait_s=args.recover_wait,
                may_restart_server=args.kill_target == "server",
                at_kill=result.get("non_terminal_at_kill"),
            )
            result["durability"]["killed_after_s"] = args.kill_after
            # 这一轮**有没有人帮忙重启**必须写在持久性那一格里：`drained_without_help` 这个名字
            # 说的就是"没人帮"，而 P5-4 的对照为了量客户端韧性是故意帮了一把的。
            # 不把这一格写明白，将来翻到这两份 json 会把"重启后排空"读成 P2 那条"无人帮忙排空"。
            result["durability"]["help"] = (
                f"受理层在 +{result.get('server_back_at_s')}s 被脚本重新拉起（这一轮量的是客户端韧性，"
                "不是无人帮忙的排空）"
                if result.get("server_back_at_s")
                else "没人帮忙：全程没有重启任何进程"
            )
            result["durability"]["reclaim_curve"] = {
                "at_kill": result.get("non_terminal_at_kill"),
                "max_after_kill": result.get("max_live_after_kill"),
                "last_sample_after_kill": result.get("last_live_after_kill"),
                "claimer_processes_seen": result.get("claimers_seen", []),
                "jobs_failed": result["counters"].get("jobs_failed"),
            }
        result["password_hash_ms"] = hash_ms
        log.flush()
        result["server_locked_lines"] = harvest_locks(log_path)
        for index, item in enumerate(workers):
            item[1].flush()
            result[f"worker_{index}_locked_lines"] = harvest_locks(data_dir / f"worker_{index}.log")
        # 谁认领了这批 job：按 pid 点名，不按"distinct 名字数"猜（见 claim_attribution 的说明）。
        server_logs = [log_path]
        if args.restart_server_after:
            server_logs.append(restart_log_path(db_path))
        result["claim_attribution"] = claim_attribution(
            db_path, server_logs, [data_dir / f"worker_{index}.log" for index in range(len(workers))]
        )
        # P5-1 那句"重复提交不会变成两个作业"在负载形态下的直接检查（提交在途被杀那一轮尤其要看）
        result["idempotency_check"] = idempotency_reading(db_path)
        # 启动那三行读数**有没有真的到达运维眼前**。这一格是 2026-10-08 那轮分进程压测里
        # 补的：`logger.info` 在 uvicorn 下面根本没 handler 接，写进日志 ≠ 有人能读到。
        server_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        result["startup_lines_seen"] = {
            key: key in server_text for key in ("执行形态", "LLM 闸门", "入口限流")
        }
    finally:
        for item in workers:
            item[0].kill()
        if process and process.poll() is None:
            process.kill()
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass
        for item in workers:
            try:
                item[1].close()
            except Exception:  # noqa: BLE001
                pass

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n读数（{result['users']} 用户 × {result['rounds']} 轮，mode=mock）")
    for key in ("login_ms", "upload_ms", "submit_ms", "e2e_s"):
        print(f"  {key:<12} p50={result[key]['p50']:>9} p95={result[key]['p95']:>9} "
              f"p99={result[key]['p99']:>9} max={result[key]['max']:>9} n={result[key]['n']}")
    print(f"  同时在跑 job 峰值 = {result['max_live_jobs']}")
    attribution = result.get("claim_attribution") or {}
    print(f"  认领归属（按 pid 点名）= {json.dumps(attribution.get('jobs_by_actor', {}), ensure_ascii=False)}"
          f"｜受理进程认领了 {attribution.get('web_process_claimed')} 个 job"
          f"｜形态 WEB_DISPATCH={result.get('processes', {}).get('web_dispatch')}")
    print(f"  启动读数是否真打出来 = {json.dumps(result.get('startup_lines_seen', {}), ensure_ascii=False)}")
    print(f"  服务端 'database is locked' 行数 = {result['server_locked_lines']}")
    print(f"  口令哈希一次 = {result['password_hash_ms']}ms")
    print(f"  事件流：{json.dumps(result['sse'], ensure_ascii=False)}")
    print(f"  计数器：{json.dumps(result['counters'], ensure_ascii=False)}")
    print(f"  幂等检查（库里）：{json.dumps(result.get('idempotency_check', {}), ensure_ascii=False)}")
    print(f"  重放探针（同一个键再发一次）：{json.dumps(result.get('replay_probe', {}), ensure_ascii=False)}")
    print(f"  客户端形状 = {result.get('client_shape')}（off 一次都不重试，on 按退避重试且重试带同一个幂等键）")
    print(f"  受理层何时回来 = {result.get('server_back_at_s')} 秒（None = 这一轮没人帮忙重启，P2 那种形状）")
    if "durability" in result:
        print(f"  持久性：{json.dumps(result['durability'], ensure_ascii=False)}")
    print(f"\n报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
