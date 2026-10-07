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

不碰开发机的真库：默认把 `APP_DATA_DIR` / `OUTPUTS_ROOT` 指到临时目录再拉起 uvicorn。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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

from agentflow.core.streams import harden_streams  # noqa: E402
from app.queueing import TERMINAL as TERMINAL_STATUSES  # noqa: E402

# "还没跑完"的判据必须与队列用的是**同一份**名单：原来这里硬编码了三处、漏了 cancelled，
# 于是被取消的 job 在读数里永远是"在跑"。改一处队列口径而尺子跟着坏，比坏在代码里难查。
NON_TERMINAL_WHERE = "status NOT IN (" + ", ".join(f"'{state}'" for state in TERMINAL_STATUSES) + ")" 

CSV = ROOT / "demo" / "data" / "login_auth.csv"
QUESTION = "对2026-09-05的登录日志做安全审计，列出失败次数最高的账号"
PACK = "login_audit"


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q / 100 * (len(ordered) - 1)))))
    return ordered[index]


def shape(name: str, values: list[float]) -> dict[str, float]:
    """一条分布的六个读数。平均值不在这里——平均值是这批里最容易被挑来骗人的那一个。"""
    if not values:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "n": len(values),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3),
        "mean": round(statistics.fmean(values), 3),
    }


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
) -> None:
    """一个用户的一轮：登录 → 上传 → 提交 → 轮询到终态。

    每一步都计时（失败也计时才有意义），传输层异常按"死在哪一步"记账——
    100 并发下服务端会 reset 连接，那是一条读数，不该让压测崩在栈上。
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

        for _ in range(rounds):
            phase = "submit"
            started = time.perf_counter()
            submit = await client.post(
                f"{base}/api/analyze",
                json={"question": QUESTION, "dataset_id": dataset_id, "mode": "mock", "pack": PACK},
                headers=headers,
            )
            metrics["submit"].append(time.perf_counter() - started)
            if submit.status_code != 200:
                counters["submit_failed"] += 1
                continue
            job_id = submit.json()["job_id"]
            if sse_case is not None and sse_case.get("job_id") is None:
                sse_case["job_id"] = job_id
                sse_case["token"] = token  # 只有一个用户负责把事件流跑一遍，见 read_stream

            phase = "poll"
            waited = 0.0
            status = "unknown"
            while waited < 600:
                poll = await client.get(f"{base}/api/jobs/{job_id}", headers=headers)
                status = poll.json().get("status") or "unknown"
                if status in ("success", "partial", "degraded", "failed", "error"):
                    break
                await asyncio.sleep(0.2)
                waited += 0.2
            metrics["e2e"].append(waited if waited else 0.2)
            if status in ("failed", "error"):
                counters["jobs_failed"] += 1
            jobs_out.append({"job_id": job_id, "status": status, "user": username})
    except httpx.HTTPError as exc:
        counters["transport_failed"] += 1
        by_phase = counters.setdefault("transport_by_phase", {})
        by_phase[phase] = by_phase.get(phase, 0) + 1
        kinds = counters.setdefault("transport_kinds", {})
        kinds[type(exc).__name__] = kinds.get(type(exc).__name__, 0) + 1
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


async def drive(users, base, rounds, concurrency, server=None, kill_after_s=None, victim=None):
    metrics: dict[str, list[float]] = {"login": [], "upload": [], "submit": [], "e2e": []}
    counters = {
        "login_failed": 0, "upload_failed": 0, "submit_failed": 0, "jobs_failed": 0,
        "transport_failed": 0,
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
                await one_user(client, base, name, rounds, metrics, counters, jobs_out, sse_case)

        async def killer_task() -> None:
            """中途杀进程：测的是持久性，不是速度——快的系统也可以永远丢结果。

            `victim` 可以是某个 worker 进程：那样测的就是"Web 还活着、执行的人死了"，
            这正是 P0 那条读数（28 个 job 永远停在非终态）的形态。
            计数必须在**杀之前一刻**取：等到整轮 gather 结束再取，那时用户都走完了，
            非终态必然是 0——"恢复了"就变成一句平凡真，而不是证据。
            """
            if kill_after_s is None:
                return
            await asyncio.sleep(kill_after_s)
            holder["killed_at_s"] = 1
            holder["non_terminal_at_kill"] = non_terminal(Path(os.environ["LOAD_DB"]))
            target = victim if victim is not None else server
            if target is not None and target.poll() is None:
                target.kill()

        await asyncio.gather(
            *(guarded(name) for name in users),
            killer_task(),
        )
        stop.set()
        if probe_task:
            await probe_task

    stream = {"skipped": True}
    if sse_case.get("job_id") and sse_case.get("token"):
        stream = await read_stream(base, sse_case["token"], sse_case["job_id"])
    stream["job_completed_first"] = True

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
            drive(names, base, args.rounds, args.concurrency or args.users, process, args.kill_after, victim)
        )
        result["processes"] = {
            "server": process.pid,
            "workers": [item[0].pid for item in workers],
            "worker_concurrency_per_process": int(os.getenv("WORKER_CONCURRENCY", "2")),
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
    print(f"  看到过的认领者 = {len(result.get('claimers_seen', []))} 岔："
          f"{'（多进程消费）' if len(result.get('claimers_seen', [])) > 1 else '（只有受理进程）'}")
    print(f"  服务端 'database is locked' 行数 = {result['server_locked_lines']}")
    print(f"  口令哈希一次 = {result['password_hash_ms']}ms")
    print(f"  事件流：{json.dumps(result['sse'], ensure_ascii=False)}")
    print(f"  计数器：{json.dumps(result['counters'], ensure_ascii=False)}")
    if "durability" in result:
        print(f"  持久性：{json.dumps(result['durability'], ensure_ascii=False)}")
    print(f"\n报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
