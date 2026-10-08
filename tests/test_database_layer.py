"""P1 数据层底座的回归：模型、DSN 与迁移必须自证"能建出同一份 schema"。

这一层还没有接管任何路由（ routers 仍是裸 SQL），所以这里的判据只守三件事：
① 不设环境变量时的 DSN 与今天的路径一致（改造不能改变"我没配数据库"的行为）；
② Alembic 的 baseline 与 SQLAlchemy 模型建出来的表**同一套**（两边各长一套 schema
   是最难查的分叉——迁移说我有 11 张表，模型说有 12 张，程序连的是第三个东西）；
③ 多租户与持久事件需要的列真的在（`jobs.org_id / worker / lease_expires_at / trace_id`、
   `job_events.(job_id, seq)` 唯一）——P2/P3 要用的东西不能等用到时才发现没建。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
from app import config as app_config


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch) -> Path:
    """每个用例一座自己的库。

    `reset_engines()` 不是装饰性的：引擎按 DSN 缓存，而 monkeypatch 改环境变量不会让
    已经建好的引擎改道。不清就是"连着跑的两个用例其实连同一个库"——第一版就是这么红的。
    """
    path = tmp_path / "layer.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    from app.database import reset_engines

    reset_engines()
    yield path
    reset_engines()


def test_dsn_defaults_to_the_local_sqlite_file(monkeypatch):
    """没配 `DATABASE_URL` 时必须落回今天那个文件——否则"没配"会变成"连不上"。"""
    from app.database import database_url, is_sqlite

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert is_sqlite()
    assert database_url().endswith(app_config.db_path().name), database_url()


def test_migration_and_models_agree(temp_db: Path):
    """alembic upgrade head 建出来的表集合，必须等于模型元数据里的集合。"""
    from alembic import command
    from alembic.config import Config

    from app.models import Base

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")

    built = {
        row[0]
        for row in sqlite3.connect(str(temp_db)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    expected = set(Base.metadata.tables)
    assert expected - built == set(), f"模型里有、迁移建不出：{sorted(expected - built)}"
    assert built - expected - {"alembic_version"} == set(), (
        f"迁移建了模型没有的表（谁偷偷改了这一层？）：{sorted(built - expected)}"
    )


def test_migration_and_models_agree_column_by_column(temp_db: Path):
    """表名对上还不够：**每张表的列名集合**也要对上。

    只比表集合的守卫会被这种事放过：P2 给 `jobs` 加了 `spec` / `attempts`（队列的真相就存在
    这两列里），运行时迁移 `app/db.py:_ADDED_COLUMNS` 有它们，而 alembic baseline 没有——
    于是"用 init_db 建的库"和"用 alembic upgrade head 建的库"是两座不同的库，
    而所有既有用例都是绿的。模型里写了≠库里存在，这条对迁移同样成立。
    """
    from alembic import command
    from alembic.config import Config

    from app.models import Base

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(temp_db))
    diverged: dict[str, dict[str, list[str]]] = {}
    for table, model in Base.metadata.tables.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue
        built_columns = {row[1] for row in rows}
        model_columns = {column.name for column in model.columns}
        missing = sorted(model_columns - built_columns)
        extra = sorted(built_columns - model_columns)
        if missing or extra:
            diverged[table] = {"迁移建不出": missing, "迁移多出来": extra}
    assert not diverged, "迁移与模型的列集合不一致（按表列出来）：\n" + json.dumps(
        diverged, ensure_ascii=False, indent=2
    )


def test_tenancy_and_durability_columns_are_really_there(temp_db: Path):
    from alembic import command
    from alembic.config import Config

    Config_new = Config("alembic.ini")
    Config_new.set_main_option("script_location", "migrations")
    command.upgrade(Config_new, "head")
    conn = sqlite3.connect(str(temp_db))

    def columns(table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    # P3 的可见性靠 org_id；P2 的可恢复靠 worker/lease；P6 的可追靠 trace_id
    assert {"org_id", "worker", "lease_expires_at", "trace_id"} <= columns("jobs"), columns("jobs")
    for table in ("datasets", "sessions", "bundles", "bundle_files", "bundle_tables"):
        assert "org_id" in columns(table), f"{table} 少了 org_id——这一张表还会漏在租户边界外"
    assert {"organizations", "memberships", "job_events", "org_quotas"} <= set(
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )


def test_job_event_seq_is_unique_per_job(temp_db: Path):
    """`(job_id, seq)` 唯一是断线重连不重放的前提；不唯一，Last-Event-ID 就没有含义。"""
    from sqlalchemy import exc as sa_exc

    from app.database import get_engine, session_scope
    from app.models import Base, JobEvent

    engine = get_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            session.add(JobEvent(job_id="job_x", seq=1, kind="run_started", payload="{}"))
            await session.commit()
        async with session_scope() as session:
            session.add(JobEvent(job_id="job_x", seq=1, kind="duplicated", payload="{}"))
            with pytest.raises(sa_exc.IntegrityError):
                await session.commit()

    asyncio.run(scenario())


def test_org_membership_pair_is_unique(temp_db: Path):
    from sqlalchemy import exc as sa_exc

    from app.database import get_engine, session_scope
    from app.models import Base, Membership, Organization, User

    engine = get_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            org = Organization(slug="acme", name="ACME")
            user = User(username="alice", password_hash="x")
            session.add_all([org, user])
            await session.flush()
            session.add(Membership(user_id=user.id, org_id=org.id, role="admin"))
            await session.commit()
            session.add(Membership(user_id=user.id, org_id=org.id, role="member"))
            with pytest.raises(sa_exc.IntegrityError):
                await session.commit()

    asyncio.run(scenario())


def test_changing_dsn_rebuilds_the_engine(tmp_path: Path, monkeypatch):
    """换了 `DATABASE_URL` 就必须换库：第一版引擎在 import 时建死，两条用例连着跑会共用
    一座库，单跑过、组合跑红——而且红的那条可能连的是开发机的真库。"""
    from app.database import get_engine, reset_engines

    first = tmp_path / "one.db"
    second = tmp_path / "two.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{first.as_posix()}")
    reset_engines()
    engine_one = get_engine()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{second.as_posix()}")
    reset_engines()
    engine_two = get_engine()
    assert engine_one is not engine_two, "DSN 换了引擎没换——连接池会替你把请求打到旧库上"
    assert str(engine_two.url).endswith("two.db"), engine_two.url


def test_sqlite_wal_pragmas_are_set_on_connect(temp_db: Path):
    """PRAGMA 走的是引擎 connect 事件——每握手一次都要设，别指望只设一次。"""
    from sqlalchemy import text

    from app.database import get_engine

    engine = get_engine()

    async def scenario():
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        return str(mode), int(timeout)

    mode, timeout = asyncio.run(scenario())
    assert mode.lower() == "wal", mode
    assert timeout >= 1000, timeout


def test_load_harness_reports_shapes(monkeypatch):
    """压测脚手架自己的读数形状：分布字段缺一个，读的人就会拿 p50 当承诺。"""
    from scripts.load_test import shape, percentile

    readings = shape("submit_ms", [1.0, 2.0, 3.0, 4.0, 50.0])
    assert set(readings) == {"n", "p50", "p95", "p99", "max", "mean"}, readings
    assert readings["max"] == 50.0 and readings["p50"] == 3.0, readings
    # n=5 时 p95 落在最大值上（索引 = round(0.95×4)=4）——这不是 bug，是小样本的诚实结果。
    # 记在这里是因为它解释了为什么读数必须连 n 一起印：n 小的时候"p95"就是"最慢的那一个"。
    assert percentile([1.0, 2.0, 3.0, 4.0, 50.0], 95) == 50.0
    assert readings["p95"] == 50.0, readings
    assert "mean" in readings, "平均值要一起印，但只能和分位数放在一起看，不然会被挑着说"
    assert shape("empty", [])["n"] == 0, "空样本要能报 0，不许炸也不许报个假的 0 分位"


def test_paths_resolve_at_call_time_from_one_authority(tmp_path, monkeypatch):
    """路径的唯一权威是环境变量，而且是**调用时**读。

    这条判据是 P2 踩出来的：各模块 `from app.config import OUTPUTS_ROOT` 会拿走一份值副本，
    测试只改得动其中几份 ⇒ 受理层与报告接口读 tmp，执行层把 run 写进仓库真 `outputs/`
    （实测 22:35 一次跑测多出 7 个 run 目录），反过来报告 404。
    """
    first, second = tmp_path / "one", tmp_path / "two"
    monkeypatch.setenv("APP_DATA_DIR", str(first))
    monkeypatch.setenv("OUTPUTS_ROOT", str(first / "outputs"))
    from app import db as db_module

    db_module.init_db()
    db_module.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')", ("u_a", "x"))
    assert db_module.query_one("SELECT id FROM users WHERE username='u_a'") is not None

    # 换 DSN 必须真的换库：新库是自己长出来的空 schema，老库里的人不跟着过来
    monkeypatch.setenv("APP_DATA_DIR", str(second))
    assert app_config.db_path() == second / "app.db"
    assert not (second / "app.db").exists(), "改了环境变量而连接还指着老库，才会在这里就把它建出来"
    db_module.init_db()
    assert (second / "app.db").exists()
    assert db_module.query_one("SELECT id FROM users WHERE username='u_a'") is None


def test_runtime_schema_matches_the_models(tmp_path, monkeypatch):
    """`init_db()` 建出来的库，列集合必须等于模型元数据——**这是运行时真正用的那一套 schema**。

    为什么单独一条：路由现在走的是 `app/db.py` 的裸 sqlite3（`init_db` + SCHEMA），而不是
    alembic。上一条守卫比的是"迁移 vs 模型"，于是运行时那份可以缺东西而没人看见——实测就是：
    `organizations/memberships/org_quotas` 三张表与六张归属表的 `org_id` 列**只存在于模型与迁移里**，
    运行时的库里根本没有，而 P3 的隔离谓词正要靠它们。
    """
    from app.db import init_db
    from app.models import Base

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "runtime"))
    init_db()

    conn = sqlite3.connect(str(tmp_path / "runtime" / "app.db"))
    built_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing_tables = sorted(set(Base.metadata.tables) - built_tables)
    assert not missing_tables, f"模型与迁移里有、运行库里没有：{missing_tables}"

    diverged: dict[str, list[str]] = {}
    for table, model in Base.metadata.tables.items():
        if table not in built_tables:
            continue
        built_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        gap = sorted({column.name for column in model.columns} - built_columns)
        if gap:
            diverged[table] = gap
    assert not diverged, "运行库缺列（P3 的谓词会在第一条查询上撞崩）：\n" + json.dumps(diverged, ensure_ascii=False)


def test_no_owned_row_defaults_to_the_admin_account(tmp_path, monkeypatch):
    """归属列不许有"忘了写就把这行判给某人"的默认值。

    `user_id INTEGER NOT NULL DEFAULT 1` 在旧 SCHEMA 里有六处：任何一条漏写 user_id 的
    INSERT 都会静默把资源判给 id=1（种子 admin），而读路径的归属谓词看起来完全正常——
    这是 IDOR 的种子，不是风格问题。新库要求：user_id **没有默认值**（漏写当场报错），
    org_id 的默认只能是 0（未归属，协作读默认拒绝），不许是 1。
    """
    from app.db import init_db

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "defaults"))
    init_db()
    conn = sqlite3.connect(str(tmp_path / "defaults" / "app.db"))
    offenders = []
    for table in ("datasets", "jobs", "sessions", "bundles", "bundle_files", "bundle_tables"):
        for row in conn.execute(f"PRAGMA table_info({table})"):
            name, default = row[1], row[4]
            if name == "user_id" and default is not None:
                offenders.append(f"{table}.user_id 默认 {default}")
            if name == "org_id" and str(default) not in {"0", "None"}:
                offenders.append(f"{table}.org_id 默认 {default}")
    assert not offenders, "这些默认值会把没声明归属的行判给别人：\n" + "\n".join(offenders)


def test_org_indexes_exist_on_both_schema_paths(tmp_path, monkeypatch):
    """`org_id` 的索引要在**两条路径**上都真的建出来：新建库与老库升级。

    这条是被自己的变异逼出来的：把 org 索引从 SCHEMA 挪到 `_AFTER_MIGRATION_SQL`（因为老库里
    org_id 是补列补出来的，索引建在补列之前会 `no such column`）之后，"删掉那一次执行"这个
    变异**全绿**——因为没有任何一条用例查过索引在不在。模型里写了≠库里存在，这条对索引同样成立。
    """
    expected = {
        "idx_datasets_org",
        "idx_jobs_org",
        "idx_sessions_org",
        "idx_bundles_org",
        "idx_memberships_user",
        # P4-2 的公平认领每次轮询都要跑一条按 (org_id, status) 的相关子查询：这条索引不在，
        # 认领就从 0.006ms 变成几十 ms（实测见 app/db.py 的注释）。模型与迁移里一直写着它，
        # 运行时那份此前没有——正是这条守卫要抓的那种"写了等于建了"。
        "ix_jobs_org_status",
    }

    def index_names(db: Path) -> set[str]:
        conn = sqlite3.connect(str(db))
        try:
            return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            conn.close()

    # ① 新库
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("APP_DATA_DIR", str(fresh))
    from app.db import init_db

    init_db()
    missing = expected - index_names(fresh / "app.db")
    assert not missing, f"新建库里没有这些 org 索引：{sorted(missing)}"

    # ② 老库：表结构是 P2 之前的形状（没有 org_id，也没有这些索引），init_db 要把它升上来
    legacy = tmp_path / "legacy" / "app.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE datasets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL DEFAULT 1,"
        " filename TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, row_count INTEGER, columns TEXT,"
        " created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,"
        " user_id INTEGER NOT NULL DEFAULT 1, question TEXT NOT NULL, mode TEXT NOT NULL,"
        " session_id TEXT, run_id TEXT, status TEXT NOT NULL, progress INTEGER NOT NULL, error TEXT,"
        " created_at TEXT, finished_at TEXT)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "legacy"))
    init_db()
    built = index_names(legacy)
    assert not expected - built, f"老库升级后缺这些索引：{sorted(expected - built)}"
    conn = sqlite3.connect(legacy)
    try:
        for table in ("datasets", "jobs"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "org_id" in columns, f"{table} 没被补上 org_id：老库上的 P3 谓词会直接崩"
    finally:
        conn.close()


def test_app_layer_never_copies_a_config_value(tmp_path):
    """`from app.config import X` 在 app/ 里一律禁止——它造的是副本，不是引用。

    守卫读 AST 而不是正则源码：同一批语句里，读法被语句自己的形状骗过一次
    （见 test_auth.py 里那条队列豁免的注释）。
    """
    import ast

    offenders = []
    for py in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "app.config":
                offenders.append(f"{py.name}: {[alias.name for alias in node.names]}")
    assert not offenders, "这些模块把配置抄成了自己的常量，改一处不动另外几处：\n" + "\n".join(offenders)
