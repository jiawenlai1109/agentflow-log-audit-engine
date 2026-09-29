"""SQLite 元数据库：初始化、迁移兜底与查询助手。"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from app.config import DB_PATH
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
    user_id INTEGER NOT NULL DEFAULT 1,
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
    user_id INTEGER NOT NULL DEFAULT 1,
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
    user_id INTEGER NOT NULL DEFAULT 1,
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
    user_id INTEGER NOT NULL DEFAULT 1,
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
    user_id INTEGER NOT NULL DEFAULT 1,
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
    user_id INTEGER NOT NULL DEFAULT 1,
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
"""

# 本地已有库的增量列（SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，先查 PRAGMA）
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "users": {"role": "TEXT NOT NULL DEFAULT 'user'"},
    # Bundle 子表带 user_id：归属谓词要能写进每一条子表查询，而不是靠"先查父行"
    "bundle_files": {"user_id": "INTEGER NOT NULL DEFAULT 1"},
    "bundle_tables": {"user_id": "INTEGER NOT NULL DEFAULT 1"},
    # 场景包：本地已有库要能补上这一列，否则老库上跑新代码会在 INSERT 处直接崩
    "jobs": {"pack": "TEXT"},
}


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
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
