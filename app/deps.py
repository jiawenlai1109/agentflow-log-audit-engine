"""FastAPI 鉴权依赖：Bearer token → 当前用户；媒体 token → 产物读取身份。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from agentflow.core.tools import ensure_within
from app.db import query_one
from app.security import SCOPE_API, SCOPE_MEDIA, decode_token

INVALID_TOKEN = HTTPException(
    status_code=401, detail="token 无效或已过期", headers={"WWW-Authenticate": "Bearer"}
)
MISSING_TOKEN = HTTPException(
    status_code=401,
    detail="缺少 Authorization Bearer token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _resolve_user(username: str) -> dict[str, Any]:
    """token 里的 sub 必须仍在 users 表内——删号即让已签发 token 失效。"""
    user = query_one("SELECT id, username, role FROM users WHERE username = ?", (username,))
    if not user:
        raise INVALID_TOKEN
    return user


def get_current_user(request: Request) -> dict[str, Any]:
    """业务接口身份来源：只接受 Header 里的 API token，绝不接受 query 传参。"""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise MISSING_TOKEN
    payload = decode_token(header[7:].strip(), expected_scope=SCOPE_API)
    if payload is None:
        raise INVALID_TOKEN
    return _resolve_user(str(payload["sub"]))


def get_media_principal(request: Request, run_id: str) -> dict[str, Any]:
    """产物读取身份：<img> 带不了自定义请求头，故媒体 token 走 query，但作用域受限。

    三重约束——scope 必须是 media（API token 走这条路会被拒）、必须未过期、
    必须绑定在被请求的那个 run 上（拿 A 的 token 读不到 B 的产物）。
    """
    token = request.query_params.get("t", "")
    payload = decode_token(token, expected_scope=SCOPE_MEDIA)
    if payload is None:
        raise HTTPException(status_code=401, detail="媒体 token 无效或已过期")
    if payload.get("run") != run_id:
        raise HTTPException(status_code=403, detail="媒体 token 与请求的 run 不匹配")
    return _resolve_user(str(payload["sub"]))


def ensure_run_access(run_id: str, user: dict[str, Any]) -> None:
    """run 归属：产出该 run 的 job 的属主，或同企业成员（报告是企业内的共享资产）。

    判定必须是"存在一条我看得见的 job 产出了这个 run"，不能取首行比对——同一 run_id
    若有多条 job 记录（重跑、测试残留），首行匹配会把别人的归属当成结论。
    无 jobs 记录的 run（CLI 直跑、鉴权上线前的历史产物）一律拒绝——默认关闭。
    判据只有一份，在 `app/access.py`：媒体 token 的签发、报告正文的读取都走这条。
    """
    from app import access

    if user.get("role") == "admin":
        return
    sql, params = access.scope(user)
    if not query_one(
        f"SELECT 1 AS ok FROM jobs WHERE run_id = ?{sql}", (run_id, *params)
    ):
        raise HTTPException(status_code=404, detail="资源不存在")


def guard_within(root: Path, target: str) -> Path:
    """目录逃逸守卫：越界统一按 404 收敛，不把内部结构泄露给探测方。"""
    try:
        return ensure_within(root, target)
    except Exception as exc:  # noqa: BLE001 - 非法路径与越界一并收敛
        raise HTTPException(status_code=404, detail="资源不存在") from exc
