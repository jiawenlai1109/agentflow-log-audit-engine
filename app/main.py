"""FastAPI 入口：挂载路由、CORS、启动初始化。

产物目录不再公开挂载，改由 app/routers/media.py 按只读媒体 token 放行；
业务接口一律要求 API token（app/deps.py）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app import config, llm_gate, ratelimit
from app.db import init_db
from app.routers import auth, bundles, datasets, jobs, llm, media, packs, reports, sessions, users
from app.runner import dispatch_form, reclaim_at_boot

logger = logging.getLogger("agentflow.web")


def ensure_operator_visible_logging() -> None:
    """让 `agentflow.*` 的 INFO 在**部署形态**下真的能被看到——只这一件事。

    实测出来的缺陷（2026-10-08，分进程那轮压测）：lifespan 里那三行启动读数（闸门 / 入口限流 /
    执行形态）用的是 `logger.info`，而 uvicorn 只给自己那族 logger 配了 handler，root 上没有，
    Python 的 `lastResort` 又只处理 WARNING 以上。结果部署起来**那三行根本没打出来**：
    作业日志里只有访问行，运维看不到"闸门是量来的还是占位"，也看不到"这个进程不认领作业"。
    这就是"写了但没人读的读数＝假出口"那一族——上一轮以为接进启动日志就有人读了。

    只在**没有任何 handler** 时补一个：部署方自己配过日志（gunicorn/容器那条链路）就不动它，
    也不设 `propagate = False`——那会让人家的 handler 收不到我们的记录，把"看得见"换成"更看不见"。
    """
    if logging.getLogger().handlers:
        return
    target = logging.getLogger("agentflow")
    target.setLevel(logging.INFO)
    if not target.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        target.addHandler(handler)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 先接通日志，再打任何读数：顺序反过来，第一批行就又变成"只有代码里存在"的那种了。
    ensure_operator_visible_logging()
    # 只建"根"。企业树（`outputs/org/<id>/`）由第一次落盘时 `mkdir(parents=True)` 带出来：
    # 启动时把库里已有的企业逐个建目录，等于让一次启动去猜将来会有多少棵空树。
    for directory in (
        config.app_data_dir(),
        config.datasets_dir(),
        config.bundles_dir(),
        config.outputs_root(),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    init_db()
    # 上游并发闸门在**开认领循环之前**定档：晚了就没有"第一批作业就撞上未开闸的窗口"这种
    # 说不清的状态。读数一行打进日志——运维要能在启动日志里看到这个数是量来的还是占位。
    gate_view = llm_gate.apply_for_process()
    logger.info(
        "LLM 闸门：%s 路（来源=%s）%s",
        gate_view.get("limit"),
        gate_view.get("limit_source"),
        f"｜{gate_view.get('note')}" if gate_view.get("limit_source") != "env" else "",
    )
    limits = ratelimit.snapshot()
    logger.info(
        "入口限流：登录每 %ss 账号 %s 次 / 来源 %s 次，建号每 %ss %s 次｜范围=%s（每个进程各自的额度，不是平台级）",
        limits["limits"]["window_s"],
        limits["limits"]["login_per_user"],
        limits["limits"]["login_per_source"],
        limits["limits"]["account_window_s"],
        limits["limits"]["account_create_per_actor"],
        limits["scope"],
    )
    # 形态先定再开认领循环：运维要在启动日志第一屏看到这个进程到底吃不吃作业。
    # `WEB_DISPATCH=off` 时这里只打一行说明，不开线程——那时执行在 `scripts/worker.py` 里。
    form = dispatch_form()
    logger.info("执行形态：%s｜本进程认领循环 %s｜%s", form["shape"], form["claim_loops"], form["note"])
    # 上次进程被杀时留下的"running"要收回来（P0 读数：28 个 job 永远停在非终态）。
    # 先收再开认领循环，否则新起的认领者看不到那批僵尸行要等的更久。
    reclaim_at_boot()
    if form["dispatch_in_web"]:
        jobs.dispatcher.start()
    try:
        yield
    finally:
        jobs.dispatcher.stop()


app = FastAPI(
    title="多智能体数据分析引擎",
    version="0.3.0",
    description="基于多智能体协作的自动化数据分析 Web 服务",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(datasets.router)
app.include_router(bundles.router)
app.include_router(packs.router)
app.include_router(llm.router)
app.include_router(jobs.router)
app.include_router(sessions.router)
app.include_router(reports.router)
app.include_router(media.router)
app.include_router(users.router)
