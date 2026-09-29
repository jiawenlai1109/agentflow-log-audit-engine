"""FastAPI 入口：挂载路由、CORS、启动初始化。

产物目录不再公开挂载，改由 app/routers/media.py 按只读媒体 token 放行；
业务接口一律要求 API token（app/deps.py）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import APP_DATA_DIR, BUNDLES_DIR, DATASETS_DIR, OUTPUTS_ROOT, SESSIONS_ROOT
from app.db import init_db
from app.routers import auth, bundles, datasets, jobs, media, packs, reports, sessions

@asynccontextmanager
async def lifespan(_: FastAPI):
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_ROOT.mkdir(parents=True, exist_ok=True)
    SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


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
app.include_router(jobs.router)
app.include_router(sessions.router)
app.include_router(reports.router)
app.include_router(media.router)
