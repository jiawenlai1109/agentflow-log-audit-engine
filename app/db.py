"""SQLite 元数据库：初始化、迁移兜底与查询助手。"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from app import config
from app.security import hash_password

logger = logging.getLogger("agentflow.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,  -- 没有默认值：漏写就报错，而不是静默把行判给某个账号
    org_id INTEGER NOT NULL DEFAULT 0,  -- 0 = 未归属企业，协作读默认拒绝
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    columns TEXT NOT NULL DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,  -- 没有默认值：漏写就报错，而不是静默把行判给某个账号
    org_id INTEGER NOT NULL DEFAULT 0,  -- 0 = 未归属企业，协作读默认拒绝
    question TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'mock',
    session_id TEXT,
    pack TEXT,
    run_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    progress INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,  -- 没有默认值：漏写就报错，而不是静默把行判给某个账号
    org_id INTEGER NOT NULL DEFAULT 0,  -- 0 = 未归属企业，协作读默认拒绝
    title TEXT,
    dataset_path TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
-- Bundle 三表（#20）：一次上传是一个 Bundle，逐文件一条 bundle_files（含被拒原因），
-- 成表的再落一条 bundle_tables——前端"逐文件状态 + 表预览"要的就是这三层。
-- 命名用 bundle_tables 而不是 tables：读 SQL 的人不必去猜它跟 sqlite 的表元数据有没有关系。
CREATE TABLE IF NOT EXISTS bundles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,  -- 没有默认值：漏写就报错，而不是静默把行判给某个账号
    org_id INTEGER NOT NULL DEFAULT 0,  -- 0 = 未归属企业，协作读默认拒绝
    name TEXT NOT NULL,
    root TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    error TEXT,
    file_count INTEGER NOT NULL DEFAULT 0,
    table_count INTEGER NOT NULL DEFAULT 0,
    document_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS bundle_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,  -- 没有默认值：漏写就报错，而不是静默把行判给某个账号
    org_id INTEGER NOT NULL DEFAULT 0,  -- 0 = 未归属企业，协作读默认拒绝
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'skipped',
    table_ref TEXT,
    reason TEXT,
    hint TEXT,
    risk TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS bundle_tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,  -- 没有默认值：漏写就报错，而不是静默把行判给某个账号
    org_id INTEGER NOT NULL DEFAULT 0,  -- 0 = 未归属企业，协作读默认拒绝
    table_ref TEXT NOT NULL,
    source_file TEXT NOT NULL,
    path TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    columns TEXT NOT NULL DEFAULT '[]',
    encoding TEXT,
    sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_datasets_user ON datasets (user_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs (user_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs (run_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id, id);
CREATE INDEX IF NOT EXISTS idx_bundles_user ON bundles (user_id, id);
CREATE INDEX IF NOT EXISTS idx_bundle_files_bundle ON bundle_files (bundle_id, id);
CREATE INDEX IF NOT EXISTS idx_bundle_tables_bundle ON bundle_tables (bundle_id, table_ref);
-- 企业三表（P3 的地基）。两张 schema 必须同源：这一份 init_db 用的 SCHEMA 与
-- app/models.py + alembic 的那一份一旦分叉，就会出现"迁移说有这张表，运行时库里没有"，
-- 而每条既有用例都还是绿的（列名对不上没人看）。守卫见
-- tests/test_database_layer.py::test_runtime_schema_matches_the_models。
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    org_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE (user_id, org_id)
);
CREATE TABLE IF NOT EXISTS org_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL UNIQUE,
    limit_concurrent_jobs INTEGER,
    limit_jobs_per_day INTEGER,
    limit_llm_calls_per_day INTEGER,
    limit_upload_bytes INTEGER,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
-- 事件落库（P2 前置）：作业"发生过什么"不能只活在某个进程的内存里。
-- seq 由 INSERT 自己算（见 app/eventlog.py），唯一约束让并发写撞车时报错而不是悄悄覆盖。
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_events_seq ON job_events (job_id, seq);
-- org_id 的索引不在这里建，见 _AFTER_MIGRATION_SQL（原因写在那一段）。
"""

# 本地已有库的增量列（SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，先查 PRAGMA）
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "users": {"role": "TEXT NOT NULL DEFAULT 'user'"},
    # Bundle 子表带 user_id：归属谓词要能写进每一条子表查询，而不是靠"先查父行"。
    # 老库补列时 SQLite 要求 NOT NULL 的新列带默认值——取 0（未归属），不取 1：
    # 默认 1 等于把"没人声明归属"的行判给 admin。
    "bundle_files": {"user_id": "INTEGER NOT NULL DEFAULT 0", "org_id": "INTEGER NOT NULL DEFAULT 0"},
    "bundle_tables": {"user_id": "INTEGER NOT NULL DEFAULT 0", "org_id": "INTEGER NOT NULL DEFAULT 0"},
    "datasets": {"org_id": "INTEGER NOT NULL DEFAULT 0"},
    "sessions": {"org_id": "INTEGER NOT NULL DEFAULT 0"},
    "bundles": {"org_id": "INTEGER NOT NULL DEFAULT 0"},
    # 场景包：本地已有库要能补上这一列，否则老库上跑新代码会在 INSERT 处直接崩
    # spec = "这个 job 到底要跑什么"（存数据源引用，不存绝对路径）；attempts = 被认领过几次，
    # 崩溃恢复靠它封顶，否则一个稳定崩溃的 job 会把队列变成永动机。
    "jobs": {
        "org_id": "INTEGER NOT NULL DEFAULT 0",
        "pack": "TEXT",
        "spec": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        # 认领三件套：谁拿着、租约到什么时候、这条链路是哪个 trace_id（P6 从受理第一跳开始记）
        "worker": "TEXT",
        "lease_expires_at": "TEXT",
        "trace_id": "TEXT",
    },
}


# 补列之后才能建的索引：`executescript(SCHEMA)` 跑在 `_ensure_columns` 之前，而 org_id 在老库里
# 恰恰是靠 `_ensure_columns` 补出来的。把这两件事的顺序写死在这里，而不是指望读者记得。
_AFTER_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships (user_id, org_id);
CREATE INDEX IF NOT EXISTS idx_datasets_org ON datasets (org_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_org ON jobs (org_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_org ON sessions (org_id, id);
CREATE INDEX IF NOT EXISTS idx_bundles_org ON bundles (org_id, id);
-- P4-2 的公平认领按企业数"在跑几个"，每轮认领都要跑一次那条相关子查询。
-- 实测（`.appdata/probe_fair_claim_cost.py`，复用一条连接量的净差）：500 待领 / 20 在跑 / 1 家企业
-- 没有这条索引要 **24.6ms**，2000 待领 / 40 在跑要 **79.3ms**——那是 worker 每秒都在付的钱。
-- 名字跟着模型与迁移里那份（`ix_jobs_org_status`），不是另起一个：两条路径上出现两个同义索引
-- 比没有索引更难查。
CREATE INDEX IF NOT EXISTS ix_jobs_org_status ON jobs (org_id, status);
"""


def get_conn() -> sqlite3.Connection:
    db_path = config.db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 三条 PRAGMA 是 100 并发下的第一道止血（P1 的最终形态是 Postgres，不是把 SQLite 调到极限）：
    #   journal_mode=WAL —— 默认 rollback journal 下写者互斥，并发写会直接抛 `database is locked`；
    #                       WAL 让读不挡写、写不挡读（同库多进程）。
    #   busy_timeout     —— 撞锁时等而不是立刻报错；配合 timeout= 参数覆盖 sqlite3 默认的 5s。
    #   synchronous=NORMAL —— WAL 下的常规档：断电不损库，只可能丢最后几个未 checkpoint 的事务，
    #                       对"作业元数据 + 产物索引"是可接受的（产物本身在文件系统，另有原子写）。
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=float(os.getenv("DB_BUSY_TIMEOUT_S", "10")))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(os.getenv('DB_BUSY_TIMEOUT_MS', '10000'))}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        conn.executescript(_AFTER_MIGRATION_SQL)
        _seed_admin(conn)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _seed_admin(conn: sqlite3.Connection) -> None:
    """种子管理员。口令一律用新算法写入：旧格式（无盐 sha256）不具备安全性，直接覆盖重种。"""
    password = os.getenv("ADMIN_PASSWORD", "").strip() or "admin"
    if password == "admin":
        logger.warning("使用默认口令 admin/admin 种子账号，多用户部署前请设置 ADMIN_PASSWORD")
    row = conn.execute("SELECT id, password_hash FROM users WHERE username = 'admin'").fetchone()
    if row is not None and row["password_hash"].startswith("pbkdf2_"):
        return
    if row is None:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            ("admin", hash_password(password)),
        )
    else:
        conn.execute(
            "UPDATE users SET password_hash = ?, role = 'admin' WHERE id = ?",
            (hash_password(password), row["id"]),
        )


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int | None:
    conn = get_conn()
    try:
        with conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid
    finally:
        conn.close()
