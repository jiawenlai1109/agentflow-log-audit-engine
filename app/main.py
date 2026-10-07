"""FastAPI 入口：挂载路由、CORS、启动初始化。

产物目录不再公开挂载，改由 app/routers/media.py 按只读媒体 token 放行；
业务接口一律要求 API token（app/deps.py）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.db import init_db
from app.routers import auth, bundles, datasets, jobs, llm, media, packs, reports, sessions
from app.runner import reclaim_at_boot

@asynccontextmanager
async def lifespan(_: FastAPI):
    for directory in (
        config.app_data_dir(),
        config.datasets_dir(),
        config.bundles_dir(),
        config.outputs_root(),
        config.sessions_root(),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    init_db()
    # 上次进程被杀时留下的"running"要收回来（P0 读数：28 个 job 永远停在非终态）。
    # 先收再开认领循环，否则新起的认领者看不到那批僵尸行要等的更久。
    reclaim_at_boot()
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
