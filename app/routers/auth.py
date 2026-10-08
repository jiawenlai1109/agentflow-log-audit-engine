"""登录、当前身份与 Token。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.db import query, query_one
from app.deps import get_current_user
from app.schemas import LoginRequest
from app.security import API_TOKEN_TTL_SECONDS, SCOPE_API, make_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 用户名不存在时用来"陪跑"一次 PBKDF2 的假哈希：抹平响应时间差，防用户名枚举
_TIMING_PAD_HASH = (
    "pbkdf2_sha256$240000$"
    "00000000000000000000000000000000$"
    "0" * 64
)


@router.post("/login")
def login(payload: LoginRequest) -> dict:
    user = query_one("SELECT * FROM users WHERE username = ?", (payload.username,))
    granted = verify_password(payload.password, user["password_hash"] if user else _TIMING_PAD_HASH)
    if not user or not granted:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "token": make_token(user["username"], scope=SCOPE_API),
        "username": user["username"],
        "role": user.get("role", "user"),
        "expires_in": API_TOKEN_TTL_SECONDS,
    }


@router.get("/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    """当前身份：除了"我是谁"，还要回答"我在哪家企业"——建号时指定企业之后，
    前端与运维都要能在不查库的情况下看出这个人到底有没有归属（空列表 = 未归属，
    共享读对他默认拒绝）。
    """
    orgs = query(
        "SELECT o.id, o.slug, o.name FROM organizations o "
        "JOIN memberships m ON m.org_id = o.id WHERE m.user_id = ? ORDER BY o.id",
        (user["id"],),
    )
    return {"username": user["username"], "role": user.get("role", "user"), "orgs": orgs}
