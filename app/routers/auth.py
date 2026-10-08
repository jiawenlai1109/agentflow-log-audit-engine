"""登录、当前身份与 Token。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app import ratelimit
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
def login(payload: LoginRequest, request: Request) -> dict:
    # 限流排在这条路由最前面，而且**在查库与那次"陪跑 PBKDF2"之前**：24000 次迭代是真实的
    # CPU 成本，一台机器对着不存在的用户名每秒刷几百次，挡不住的话先倒下的是我们自己的进程。
    # 两道闸的顺序有讲究：来源在前、账号在后——只按账号计，拿一批用户名各试一次的枚举
    # 完全没有成本；来源那道会在扫到几十个名字时先响。
    ratelimit.guard(
        "login_source",
        ratelimit.source_of(request),
        limit=ratelimit.LOGIN_PER_SOURCE,
        window_s=ratelimit.WINDOW_S,
        request=request,
        reason="这台来源的登录尝试太频繁",
    )
    ratelimit.guard(
        # 键取小写并去空格：`ADMIN` 与 `admin` 在 SQLite 的 BINARY 比较下是两个不同的查询，
        # 但在限流上必须是同一个预算——否则每个大小写变体都有一条新的额度。
        "login_user",
        payload.username.strip().lower(),
        limit=ratelimit.LOGIN_PER_USER,
        window_s=ratelimit.WINDOW_S,
        request=request,
        reason="这个账号的登录尝试太频繁",
    )
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
