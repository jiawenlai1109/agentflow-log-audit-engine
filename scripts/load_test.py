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
    started = time.perf_counter()
    login = await client.post(f"{base}/api/auth/login", json={"username": username, "password": "load-test-pw"})
    metrics["login"].append(time.perf_counter() - started)
    if login.status_code != 200:
        counters["login_failed"] += 1
        return
    # 每个用户带自己的 Authorization 逐次传，不改共享 client 的头：
    # 共用一个 AsyncClient 又互相覆盖 header ⇒ 提交时带的是别人的 token，
    # 归属过滤就会把"你读不到他的数据集"报成 404——那是量具在骗人（这轮就先这样红过一次）。
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

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
      try:
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
      except httpx.HTTPError:
        # 服务进程已经不在了（--kill-after 就是故意杀的）：记一条传输失败，别让整个压测崩在栈上
        counters["transport_failed"] += 1
        return


async def read_stream(base: str, token: str, job_id: str) -> dict[str, Any]:
    """事件流这一段不测吞吐，测的是三件会骗人的事：能不能续上、续上时重不重放、完成后还能不能读。"""
    headers = {"Authorization": f"Bearer {token}"}
    first: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30) as probe:
            async with probe.stream("GET", f"{base}/api/jobs/{job_id}/events", headers=headers) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        first.append(line)
                        if len(first) >= 3:
                            break  # 故意断开：模拟用户切页面、网络掉线
    except Exception as exc:  # noqa: BLE001 - 流的坏形状就是要如实记下来
        return {"phase1_events": len(first), "reconnect_error": type(exc).__name__}

    replayed: list[str] = []
    async with httpx.AsyncClient(timeout=20) as again:
        try:
            async with again.stream(
                "GET", f"{base}/api/jobs/{job_id}/events", headers={**headers, "Last-Event-ID": str(len(first))}
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        replayed.append(line)
                        if len(replayed) >= 8:
                            break
        except Exception:  # noqa: BLE001
            pass
    duplicates = len(set(first) & set(replayed))
    return {
        "phase1_events": len(first),
        "phase2_events": len(replayed),
        "duplicated_events": duplicates,
        # 服务端认不认 Last-Event-ID：认了就不该有重复事件
        "honors_last_event_id": duplicates == 0 and len(replayed) > 0,
    }


async def concurrency_probe(db_path: Path, holder: dict[str, int], stop: asyncio.Event) -> None:
    """盯 jobs 表里非终态的行数：这是"同时在跑"的直接证据，比猜 worker 数诚实。"""
    while not stop.is_set():
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            live = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status NOT IN "
                "('success','partial','degraded','failed','error')"
            ).fetchone()[0]
            conn.close()
        except sqlite3.OperationalError:
            # 读侧撞锁也要计数：它和"服务端 locked 行数"是两个来源，都要留
            holder["locked_reads"] = holder.get("locked_reads", 0) + 1
            live = 0
        except Exception:  # noqa: BLE001
            live = 0
        holder["max_live_jobs"] = max(holder.get("max_live_jobs", 0), int(live))
        await asyncio.sleep(0.2)


def non_terminal(db_path: Path) -> int:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status NOT IN "
            "('success','partial','degraded','failed','error')"
        ).fetchone()[0])
    finally:
        conn.close()


async def drive(users, base, rounds, concurrency, server=None, kill_after_s=None):
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
            """中途杀进程：测的是持久性，不是速度——快的系统也可以永远丢结果。"""
            if kill_after_s is None:
                return
            await asyncio.sleep(kill_after_s)
            holder["killed_at_s"] = 1
            if server is not None and server.poll() is None:
                server.kill()

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


def kill_and_check(base: str, process: subprocess.Popen, db_path: Path, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """持久性测法：进程在作业中途被杀，重启后那些 job 是什么下场。

    这一条与速度无关——快的系统也可以永远丢结果。
    """
    pending_before = 0
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        pending_before = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status NOT IN "
            "('success','partial','degraded','failed','error')"
        ).fetchone()[0]
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    process.kill()
    time.sleep(1.5)
    restarted, restart_log = spawn_server(int(os.environ["LOAD_PORT"]), Path(os.environ["APP_DATA_DIR"]), restart_log_path(db_path))
    wait_ready(base, restarted, timeout_s=40)
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        still = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status NOT IN "
            "('success','partial','degraded','failed','error')"
        ).fetchone()[0]
        conn.close()
    finally:
        restarted.kill()
    return {
        "pending_when_killed": pending_before,
        "still_pending_after_restart": still,
        "durable": pending_before > 0 and still == 0,
    }


def restart_log_path(db_path: Path) -> Path:
    return db_path.parent / "server_restart.log"


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 平台负载尺子（mock 模式，不烧上游）")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=0, help="同时发起的用户数，默认=users")
    parser.add_argument("--kill-after", type=float, default=None, help="跑到第 N 秒时杀掉服务进程")
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
    log_path = data_dir / "server.log"

    print(f"负载尺子 ｜ 用户 {args.users} ｜ 每人 {args.rounds} 次 ｜ 端口 {port} ｜ 数据目录 {data_dir}")
    # 先建 schema 再种子账号：init_db 在服务的 lifespan 里也会跑一次，但那时已经晚于
    # 我们往同一份库里插用户。环境变量先设好，两边看到的是同一个 DB_PATH。
    from app.db import init_db

    init_db()
    names, hash_ms = seed_users(db_path, args.users)
    process, log = spawn_server(port, data_dir, log_path)
    base = f"http://127.0.0.1:{port}"
    try:
        wait_ready(base, process)
        result = asyncio.run(
            drive(names, base, args.rounds, args.concurrency or args.users, process, args.kill_after)
        )
        if args.kill_after:
            # 进程已经不在了：这些非终态的 job 永远不会有人去写完它——
            # 作业状态活在内存线程池里，落库的只有 pending。这就是持久性的读数。
            result["durability"] = {
                "killed_after_s": args.kill_after,
                "jobs_left_non_terminal": non_terminal(db_path),
                "server_alive": process.poll() is None,
            }
        result["password_hash_ms"] = hash_ms
        log.flush()
        result["server_locked_lines"] = harvest_locks(log_path)
        if False:
            log.close()
            result["durability"] = kill_and_check(base, process, db_path, result["jobs"])
            process = None
    finally:
        if process and process.poll() is None:
            process.kill()
        try:
            log.close()
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
