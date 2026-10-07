"""会话与引擎：一个 DSN 口径，两边可跑（SQLite 开发/CI、Postgres 生产）。

为什么要这一层：今天每条 SQL 都 `sqlite3.connect(DB_PATH)` 现开现关（`app/db.py`），
连接生命周期没人管 ⇒ 100 并发时一是连接风暴，二是写者互斥。引擎池（Postgres 用 asyncpg
池、SQLite 用单连接池 + WAL）在这里定一次。

三条口径：

1. **DSN 由 `DATABASE_URL` 给**，没配就退回本地 SQLite 文件（默认值与今天的路径一致）——
   不设环境变量时的行为必须和改造前相同，否则"我没配数据库"会变成"连不上库"。
2. **引擎按 DSN 惰性建，绝不在 import 时建**。第一版就是 import 时建死，结果两条用例
   连到同一个库上：单跑过、连着跑红，而且红的那个可能连的是开发机的真库
   （`tests/test_database_layer.py::test_changing_dsn_rebuilds_the_engine` 钉的就是这一条）。
   DSN 是运行期事实，不能在一个进程出生时就定死。
3. **异步引擎 + `AsyncSession`**：路由改 async 之后，一次请求占一个连接，等待上游
   （LLM、文件）不再占线程。这是"100 并发 vs 40 线程上限"的真正解法。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, pool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app import config

_ENGINES: dict[str, AsyncEngine] = {}
_FACTORIES: dict[str, async_sessionmaker[AsyncSession]] = {}


def database_url() -> str:
    """解析 DSN。`postgresql+asyncpg://…` 生产用；默认 SQLite 保证开箱能跑。"""
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if raw:
        return raw
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def is_sqlite() -> bool:
    return database_url().startswith("sqlite")


def _build_engine(url: str) -> AsyncEngine:
    kwargs: dict[str, object] = {"echo": False}
    if url.startswith("sqlite"):
        # SQLite：连接交给驱动自己管（NullPool），配合 WAL + busy_timeout；
        # 真正的水平扩靠换 Postgres，不靠把 SQLite 榨干。
        kwargs["poolclass"] = pool.NullPool
        kwargs["connect_args"] = {"timeout": float(os.getenv("DB_BUSY_TIMEOUT_S", "10"))}
    else:
        kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "20"))
        kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "20"))
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = int(os.getenv("DB_POOL_RECYCLE_S", "1800"))
    engine = create_async_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001 - 形状由 DBAPI 定
            """每次握手都设：WAL 是库级持久设置，busy_timeout/synchronous 是连接级的。"""
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={int(os.getenv('DB_BUSY_TIMEOUT_MS', '10000'))}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine(url: str | None = None) -> AsyncEngine:
    """取（并按需建）当前 DSN 的引擎。同 DSN 复用同一个引擎——池要共享才有意义。"""
    resolved = url or database_url()
    if resolved not in _ENGINES:
        _ENGINES[resolved] = _build_engine(resolved)
        _FACTORIES[resolved] = async_sessionmaker(_ENGINES[resolved], expire_on_commit=False)
    return _ENGINES[resolved]


def get_session_factory(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    get_engine(url)
    return _FACTORIES[url or database_url()]


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：`session: AsyncSession = Depends(get_session)`。"""
    async with session_scope() as session:
        yield session


async def dispose_all() -> None:
    for engine in _ENGINES.values():
        await engine.dispose()
    _ENGINES.clear()
    _FACTORIES.clear()


def reset_engines() -> None:
    """测试隔离用：不异步 dispose（没有事件循环可用），但绝不许留着跨 DSN 的旧引擎。"""
    _ENGINES.clear()
    _FACTORIES.clear()


def sqlite_file() -> Path:
    """当前 DSN 指向的 SQLite 文件（压测与迁移脚本要知道它在哪，不许猜）。"""
    url = database_url()
    if not url.startswith("sqlite"):
        raise RuntimeError(f"当前 DSN 不是 SQLite：{url.split('://')[0]}")
    return Path(url.split("///", 1)[1])
