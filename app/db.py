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
CREATE INDEX IF NOT EXISTS idx_datasets_user ON datasets (user_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs (user_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs (run_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id, id);
"""

# 本地已有库的增量列（SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，先查 PRAGMA）
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "users": {"role": "TEXT NOT NULL DEFAULT 'user'"},
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
