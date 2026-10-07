"""Alembic 运行环境：DSN 与元数据都从应用代码取，不在迁移配置里再抄一份。

三条口径：

1. **DSN 只有一个来源**（`app.database.database_url()`）：`alembic.ini` 里那条留空。
   两处各写一遍迟早会分叉——分叉的表现是"迁移打在了另一个库上"，那是最难查的一类事故。
2. **异步引擎要在同步上下文里跑**：`connectable.run_sync(do_run_migrations)`，
   SQLite/Postgres 两边同一份代码，不写方言分支。
3. **`compare_type=True`**：不打开的话，改列类型不会进 autogenerate 的差异，
   于是"迁移没抓到"会被误读成"不用迁移"。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from app.database import database_url
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # noqa: ANN001 - 由 SQLAlchemy 传入的连接
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite 的 ALTER 能力有限（不能改列、不能删列带索引），batch 模式是唯一能自动生成
        # 正确迁移的形状；Postgres 下关掉，免得把一条 ALTER 拆成建表-拷贝-改名三步。
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """一律走异步引擎 + `run_sync`：SQLite 与 Postgres 同一份代码，不写方言分支。

    第一版这里写的是 `AsyncEngine(url)`——那是个类，参数是**已建好的同步引擎**，
    传 URL 会在 `sync_engine.dialect` 上炸（本项目里"配置传进去没生效"这类错已经犯过几次，
    所以这条写在注释里：构造函数不接受 URL，接受引擎）。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    connectable = create_async_engine(_url(), poolclass=pool.NullPool)

    async def _go() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
        await connectable.dispose()

    asyncio.run(_go())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
