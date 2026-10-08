"""账号与企业成员：建号时指定企业（用户 2026-10-08 选定的形态）。

为什么不做邀请制：邀请制要一条"未接受的邀请"状态与一条对外可达的链接，那是另一套
生命周期与另一批可滥用的入口；而现在的需求只是"企业内部有多个人能上传与出报告"。
所以建号这个动作留在**已经登录的管理员**手里，不新增任何匿名可达的写入口。

三条口径：

1. **只有全局 admin 能建号**（`users.role == 'admin'`）。`memberships.role` 里那个企业内
   角色**还不参与判定**——把"企业 admin 可以在自己企业里建号"顺手做进来，等于在没有测试
   与审计的情况下引入一类新的授权主体，那要单独一片。
2. 建号时 `org` 可留空。留空 = 该账号 `org_id = 0`（未归属），它只能看见自己的资源：
   共享读对未归属默认拒绝（判据在 `app/access.py`）。**没有企业不是错误，是最小的权限。**
3. 成员列表按**调用者的企业**过滤，而不是"登录就能看全部人"。一个企业里谁在，是协作要用的
   信息；别的企业有谁，不是。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from app import queueing
from app.db import execute, query, query_one
from app.deps import get_current_user, require_admin
from app.schemas import AccountCreateRequest, AccountOut, MemberOut, OrgOut, QuotaPatchRequest
from app.security import hash_password

router = APIRouter(prefix="/api", tags=["accounts"])

# 用户名进 SQL 是参数化的，进不了注入；但形状仍然要收：它以后会进日志与展示层
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_CHARS = 12


@router.get("/orgs", response_model=list[OrgOut])
def list_orgs(user: dict = Depends(require_admin)) -> list[dict]:
    """建号时要选企业，所以这份名单只给管理员——它不该变成" enumerate 平台上有几家租户"。"""
    return query("SELECT id, slug, name FROM organizations ORDER BY id", ())


@router.post("/users", response_model=AccountOut)
def create_account(
    payload: AccountCreateRequest, user: dict = Depends(require_admin)
) -> dict:
    """建号并（可选）当场入企业。重名是 409，不是"静默改密码"。"""
    username = payload.username.strip()
    if not USERNAME.match(username):
        raise HTTPException(
            status_code=422,
            detail="用户名需为 3-32 位字母、数字或 _ . -（会进日志与展示层，先收紧形状）",
        )
    if len(payload.password) < MIN_PASSWORD_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"口令至少 {MIN_PASSWORD_CHARS} 位（PBKDF2 存得好，挡不住弱口令被撞）",
        )
    if query_one("SELECT id FROM users WHERE username = ?", (username,)):
        # 409 而不是"已存在"式静默成功：管理员必须知道自己没建成
        raise HTTPException(status_code=409, detail=f"用户名 {username} 已存在")

    org_id = 0
    if payload.org:
        row = query_one(
            "SELECT id FROM organizations WHERE slug = ? OR id = ?", (payload.org, _as_int(payload.org))
        )
        if not row:
            available = [item["slug"] for item in query("SELECT slug FROM organizations ORDER BY id", ())]
            raise HTTPException(
                status_code=422,
                detail=f"企业 {payload.org} 不存在（可用 slug：{available or '还没有企业，先建一家'}）",
            )
        org_id = int(row["id"])

    uid = execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password(payload.password)),
    )
    if org_id:
        execute(
            "INSERT INTO memberships (user_id, org_id, role) VALUES (?, ?, 'member')",
            (uid, org_id),
        )
    return {"id": uid, "username": username, "org_id": org_id, "role": "user"}


@router.get("/users", response_model=list[MemberOut])
def list_members(user: dict = Depends(get_current_user)) -> list[dict]:
    """同企业成员名单：协作要知道谁能看到彼此的资源，越出企业就不该看见。

    没有企业的调用者拿到空列表（他没有任何共享对象），自己的身份走 `/api/auth/me`。
    """
    return query(
        "SELECT u.id AS user_id, u.username, m.org_id, m.role AS org_role, "
        "       (u.id = ?) AS is_me "
        "FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE m.org_id IN (SELECT org_id FROM memberships WHERE user_id = ?) "
        "ORDER BY m.org_id, u.username",
        (user["id"], user["id"]),
    )


def _as_int(value: str) -> int:
    """`org` 允许写 slug 也允许写数字 id；非数字时给 0，让 slug 那条支路去命中。"""
    text = str(value).strip()
    return int(text) if text.isdigit() else 0


@router.patch("/orgs/{org_id}/quota")
def patch_org_quota(
    org_id: int, payload: QuotaPatchRequest, user: dict = Depends(require_admin)
) -> dict:
    """给一家企业设配额（仅全局 admin）。响应里同时给出**此刻的用量**与**哪几项不生效**。

    为什么用量跟着返回：配额这个数字单独放着没有意义——"上限 5"要和"现在几个"一起看才知道
    是"还宽"还是"已经贴着"。而"哪几项不生效"必须出现在写的那一步，而不是等人来问：
    设了一条不判的列却没人说，运维会以为已经拦住了。

    `exclude_unset` 是这条接口的语义本身：键没出现 = 不动这一列，出现且为 null = 清除。
    """
    if not query_one("SELECT id FROM organizations WHERE id = ?", (org_id,)):
        raise HTTPException(status_code=404, detail=f"企业 {org_id} 不存在（配额不给没谈过的租户留一行）")
    from app import quota  # 局部导入：判定与用量的读法都在 quota 那侧，路由只做 HTTP 形状

    row = quota.set_quota(org_id, payload.model_dump(exclude_unset=True))
    return {"org_id": org_id, "quota": row, "usage": queueing.org_usage(org_id), **quota.capabilities()}
