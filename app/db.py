"""SQLite 元数据库：初始化与查询助手。"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.config import DB_PATH
from app.security import hash_password


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
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
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, ?)",
            ("admin", hash_password("admin")),
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
