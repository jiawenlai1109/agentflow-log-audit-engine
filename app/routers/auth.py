"""登录与 Token。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.db import query_one
from app.schemas import LoginRequest
from app.security import hash_password, make_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(payload: LoginRequest) -> dict:
    user = query_one("SELECT * FROM users WHERE username = ?", (payload.username,))
    if not user or user["password_hash"] != hash_password(payload.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": make_token(payload.username), "username": payload.username}
