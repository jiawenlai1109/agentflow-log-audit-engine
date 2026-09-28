"""登录、当前身份与 Token。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.db import query_one
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
    return {"username": user["username"], "role": user.get("role", "user")}
