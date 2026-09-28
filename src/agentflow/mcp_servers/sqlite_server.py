"""第一个 MCP server：只读 SQLite 直连（骨架，stdlib-only）。

线格式：行分隔 JSON-RPC 2.0 over stdio，方法取 MCP 的 `initialize` / `tools/list` /
`tools/call` 三个。为什么自己写而不装 SDK：骨架要证的是**我们这一侧的强制链**
（白名单、分级、审批、出站标记、超时），server 越薄越好，而且它必须能跑在 2 核 3G 的
实验机上——多一个 node 运行时就多一个装不上的理由。

三条自带的防线（客户端那三条在 core/mcp.py，这一侧是"就算客户端放水"的下限）：
1. 数据库以 `mode=ro` URI 打开，SQLite 层面就没有写权限；
2. `query` 只接受单条 SELECT/WITH，且外层强制 `LIMIT`——外部数据源不该有一次全表拖取；
3. `write_note` 只有在启动时显式给了 `--writable-db` 才存在（默认零文件系统权限的形状）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "sqlite-readonly"
SERVER_VERSION = "0.1.0"
DEFAULT_ROW_LIMIT = 500
MAX_ROW_LIMIT = 5000

_FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "replace",
)


def _tools(writable_db: str | None) -> list[dict[str, Any]]:
    tools = [
        {
            "name": "list_tables",
            "description": "列出库里的表与每张表的行数（只读）",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"tier": "read", "readOnlyHint": True},
        },
        {
            "name": "query",
            "description": "执行一条只读 SELECT，返回 rows/columns",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                    "params": {"type": "array"},
                    "limit": {"type": "integer"},
                },
                "required": ["sql"],
            },
            "annotations": {"tier": "read", "readOnlyHint": True},
        },
    ]
    if writable_db:
        tools.append(
            {
                # 故意提供一只写工具：人在环那道闸门要有东西可拦，
                # 全是只读工具的测试只能证明"没炸"，证明不了"被拦住"。
                "name": "write_note",
                "description": "向 triage_note 表写入一条备注（写能力，需人在环批准）",
                "inputSchema": {
                    "type": "object",
                    "properties": {"host": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["host", "note"],
                },
                "annotations": {"tier": "write", "readOnlyHint": False, "destructiveHint": True},
            }
        )
    return tools


def _check_read_sql(sql: str) -> str:
    """静态判定这条 SQL 是不是"只读的单条查询"。

    这是字符串级守卫，不是解析器——它的价值在于把明显的越界挡掉，
    真正的下限是连接本身的 `mode=ro`。两者都要有，因为各自都能被单独绕过。
    """
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise ValueError("sql 为空")
    if ";" in text:
        raise ValueError("只允许单条语句（检测到额外的分号）")
    lowered = text.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("只允许 SELECT / WITH 开头的只读查询")
    for word in _FORBIDDEN:
        if f" {word} " in f" {lowered} ":
            raise ValueError(f"查询包含禁止的关键字：{word}")
    return text


def _connect(db: str, writable: bool = False) -> sqlite3.Connection:
    path = Path(db).resolve()
    if not writable:
        if not path.exists():
            raise FileNotFoundError(f"数据库不存在：{path.name}")
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    return sqlite3.connect(str(path), timeout=5)


def _rows(conn: sqlite3.Connection, sql: str, params: list[Any]) -> tuple[list[str], list[dict[str, Any]]]:
    cursor = conn.execute(sql, params)
    columns = [str(d[0]) for d in (cursor.description or [])]
    return columns, [dict(zip(columns, row)) for row in cursor.fetchall()]


def handle_list_tables(args: dict[str, Any], cfg: argparse.Namespace) -> dict[str, Any]:
    conn = _connect(cfg.db)
    try:
        _columns, tables = _rows(
            conn,
            "SELECT name AS table_name FROM sqlite_master WHERE type='table' ORDER BY name",
            [],
        )
        out = []
        for entry in tables:
            name = str(entry["table_name"])
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out.append({"table": name, "row_count": int(count)})
        return {"tables": out, "database": Path(cfg.db).name}
    finally:
        conn.close()


def handle_query(args: dict[str, Any], cfg: argparse.Namespace) -> dict[str, Any]:
    sql = _check_read_sql(str(args.get("sql") or ""))
    limit = min(int(args.get("limit") or DEFAULT_ROW_LIMIT), MAX_ROW_LIMIT)
    params = list(args.get("params") or [])
    conn = _connect(cfg.db)
    try:
        # 外层强制 LIMIT：外部数据源不该因为一条没写限制的话就被整表拖走
        wrapped = f"SELECT * FROM ({sql}) AS _mcp LIMIT ?"
        columns, rows = _rows(conn, wrapped, [*params, limit])
        return {"columns": columns, "rows": rows, "row_limit": limit, "truncated": len(rows) >= limit}
    finally:
        conn.close()


def handle_write_note(args: dict[str, Any], cfg: argparse.Namespace) -> dict[str, Any]:
    if not cfg.writable_db:
        raise PermissionError("本 server 以只读模式启动，write_note 不可用")
    conn = _connect(cfg.writable_db, writable=True)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS triage_note (host TEXT, note TEXT, created_at TEXT)")
        conn.execute(
            "INSERT INTO triage_note(host, note, created_at) VALUES (?, ?, datetime('now'))",
            (str(args.get("host") or ""), str(args.get("note") or "")),
        )
        conn.commit()
        return {"inserted": True, "database": Path(cfg.writable_db).name}
    finally:
        conn.close()


HANDLERS = {
    "list_tables": handle_list_tables,
    "query": handle_query,
    "write_note": handle_write_note,
}


def respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve(cfg: argparse.Namespace) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            respond({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if request_id is None:  # 通知：按协议不应答
            continue
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method == "tools/list":
                result = {"tools": _tools(cfg.writable_db)}
            elif method == "tools/call":
                name = str(params.get("name") or "")
                available = {tool["name"] for tool in _tools(cfg.writable_db)}
                if name not in available:
                    raise KeyError(f"本 server 不提供工具 {name}（可用：{sorted(available)}）")
                payload = HANDLERS[name](dict(params.get("arguments") or {}), cfg)
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                    "isError": False,
                }
            else:
                respond(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"method not found: {method}"},
                    }
                )
                continue
            respond({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:  # noqa: BLE001 - server 侧任何异常都要以 isError 回，不能崩管道
            respond(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)[:400]})}],
                        "isError": True,
                    },
                }
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读 SQLite MCP server")
    parser.add_argument("--db", required=True, help="只读打开的 sqlite 文件")
    parser.add_argument("--writable-db", default=None, help="显式给出才提供 write_note")
    sys.exit(serve(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
