"""独立 worker 进程：`python scripts/worker.py`。

它不重新实现任何执行逻辑——`app.runner.Dispatcher` 与 Web 进程里跑的是同一份代码，
差别只有"谁在跑"。这一点是刻意的：两条执行路径迟早分叉（本项目已经为"两个入口的运行
语义不一致"付过一次学费，见 DatasetsView 那条写死 mock 的"追问"）。

可配的只有三件事，都读环境变量，与 Web 进程同一套口径：
  WORKER_CONCURRENCY  这个进程里几个认领循环（默认 2）
  JOB_LEASE_SECONDS   租约时长；进程被杀后最迟这么久 job 会被别人重新认领
  JOB_MAX_ATTEMPTS    一个 job 最多被认领几次，超了判 failed 并写清原因

配套的部署口径（P5-3 定型）：Web 侧设 `WEB_DISPATCH=off` 才是"受理层不吃作业"的形态；
两边都吃也安全（认领是原子的），只是那时上游拿到的是**两个进程各自的闸门**之和。
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agentflow.core.streams import harden_streams  # noqa: E402

from app import llm_gate, queueing  # noqa: E402
from app.db import init_db  # noqa: E402
from app.jobs import JobManager  # noqa: E402
from app.runner import Dispatcher, execute_job, reclaim_at_boot  # noqa: E402
from app import eventlog  # noqa: E402

_running: Dispatcher | None = None


def _stop(signum, _frame) -> None:  # noqa: ANN001 - 信号处理器的形状是固定的
    print(f"收到信号 {signum}，停止认领（在跑的 job 让它跑完这一条）", flush=True)
    if _running is not None:
        _running.stop()


def main() -> int:
    global _running
    harden_streams()
    parser = argparse.ArgumentParser(description="分析作业 worker（与 Web 进程分开的认领者）")
    parser.add_argument("--workers", type=int, default=None, help="本进程的认领循环数，默认取 WORKER_CONCURRENCY")
    parser.add_argument("--once", action="store_true", help="只处理当前排队的 job 后退出（冒烟用）")
    args = parser.parse_args()

    init_db()
    # 闸门在**这个进程**也定一次档，与 Web 的 lifespan 同一份策略（app/llm_gate.apply_for_process）。
    # 少了这一句，real 作业在这里就只能走 `get_gate()` 的懒建路径，而那条路径故意不读预检缓存
    # （读缓存是策略不是机制）——于是"实测 16 路"在分进程部署下静默退成占位 4 路：
    # 作业不报错，只是每条都比预期慢，而且排队发生在没人看的那一侧。这是 P2 建 worker 时
    # 漏下的一格，本轮定型时实测出来的。
    gate_view = llm_gate.apply_for_process()
    print(
        f"LLM 闸门：{gate_view.get('limit')} 路（来源={gate_view.get('limit_source')}）｜范围=每进程各一份"
        f"{'｜' + str(gate_view.get('note')) if gate_view.get('limit_source') != 'env' else ''}",
        flush=True,
    )
    recovered = reclaim_at_boot()
    if recovered:
        print(f"启动时收回 {len(recovered)} 个失去 worker 的 job：{', '.join(recovered[:5])}", flush=True)

    manager = JobManager(sink=eventlog.append_event)

    if args.once:
        # 冒烟：把当前排队的都跑完就退出。这里是"认领 + 执行"，不是只认领——
        # 只 claim 不执行会把行留在 running，等租约过期才回来，等于自己造一批僵尸 job。
        executed = 0
        name = queueing.worker_id()
        while (claim := queueing.claim(name)) is not None:
            execute_job(claim, manager)
            executed += 1
        print(f"--once：跑完 {executed} 个 job 后退出", flush=True)
        return 0

    _running = Dispatcher(manager, workers=args.workers)
    _running.start()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(
        f"worker 起跑：{queueing.worker_id()}｜循环 {_running.workers}｜租约 {queueing.LEASE_SECONDS}s｜"
        f"最多认领 {queueing.MAX_ATTEMPTS} 次",
        flush=True,
    )
    try:
        # 等信号，不等"队列空"——worker 是常驻的，队列空了也要接着认领
        while not _running.stopped():
            time.sleep(1.0)
    except KeyboardInterrupt:
        _running.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
