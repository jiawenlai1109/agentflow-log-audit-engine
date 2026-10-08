"""谁能看见哪一行——归属判据**只住在这一个文件里**。

为什么要收成一个函数：改之前 `app/routers/*.py` 里有 22 处各自手写的 `AND user_id = ?`。
那种形状下"加一层企业共享"要改 22 个地方，而漏掉任何一处的表现不是报错，是**该被挡住的
人看见了**——这一类比慢更难查，所以判据不许有第二份。守卫写在 `tests/test_auth.py::test_routers_never_write_their_own_ownership_predicate`（按 AST 查，
见那条的注释）。

口径（用户 2026-10-07 定的四条之一："不同企业隔离，每个企业内部有多个人可以上传文件生成报告"）：

- **SHARED**（读）：`user_id = 我` 或者「这一行的 `org_id <> 0` 且 `org_id` 在我的成员关系里」。
  用在数据集、Bundle、作业与报告——企业内协作共享的就是这几样。
- **OWNER**（删/改，以及会话这类个人上下文）：只有造它的人。删除是破坏性的，默认不给同企业
  的成员；要给的话是单独一条决定（"企业 admin 能删成员的资源"），别顺手放宽。
- `org_id = 0` 表示**未归属企业**：它一律只对 owner 可见。默认拒绝而不是默认放行——
  "没声明归属"不等于"谁都能看"。
- 跨企业/越权统一按"不存在"处理（沿用 M0 的防枚举口径）：调用方拿到 404，不拿到 403。

这里**故意不做**的事：不查"用户是否还在这个企业里"以外的任何角色与权限层级。role 字段在
`memberships` 里存着但还没参与判定——那是 P3 之后按需求再加的一层，不要在这里预支。
"""

from __future__ import annotations

from typing import Any

from app.db import query, query_one

SHARED = "shared"
OWNER = "owner"
UNASSIGNED_ORG = 0  # 0 = 没有企业；协作读对它默认拒绝


def _user_id(user: dict[str, Any]) -> int:
    return int(user["id"])


def scope_by_id(user_id: int, mode: str = SHARED) -> tuple[str, list[Any]]:
    """`scope` 的整数入口：给那些只拿着 user_id 的地方（后台解析线程、内部写库函数）。

    分成两个函数而不是让调用方自己拼字典：**判据仍然只有一份**。判据有第二份实现的后果
    不是报错，是"该被挡住的人看见了"。
    """
    if mode == OWNER:
        return " AND user_id = ?", [user_id]
    if mode != SHARED:
        raise ValueError(f"未知的归属判定模式：{mode!r}")
    return (
        " AND (user_id = ? OR (org_id <> ? AND org_id IN "
        "(SELECT org_id FROM memberships WHERE user_id = ?)))",
        [user_id, UNASSIGNED_ORG, user_id],
    )


def scope(user: dict[str, Any], mode: str = SHARED) -> tuple[str, list[Any]]:
    """一段可以直接拼在任何 `WHERE` 后面的归属谓词，连它的参数一起给。

    返回值刻意拼成 `AND ...` 而不是裸条件：调用方写的是 `WHERE bundle_id = ?{sql}`，
    忘了拼也会**少一条放行路径**（fail-closed），而不是多一条。
    """
    return scope_by_id(_user_id(user), mode)


def primary_org(user: dict[str, Any]) -> int:
    """新建资源时盖上的企业归属。

    取"最小的那个成员关系"而不是让用户每次选：多企业成员是少数情况，而**没归属**是多数情况，
    默认值必须是那个"默认拒绝"的 0。真要一人多企业，这里换成请求里显式指定的 org。
    """
    row = query_one(
        "SELECT org_id FROM memberships WHERE user_id = ? AND org_id <> ? ORDER BY org_id ASC LIMIT 1",
        (_user_id(user), UNASSIGNED_ORG),
    )
    return int(row["org_id"]) if row else UNASSIGNED_ORG


def dataset_row(user: dict[str, Any], dataset_id: int, mode: str = SHARED) -> dict[str, Any] | None:
    """按 id 取一行数据集，归属判据同 `scope`。

    为什么也放这儿：`app/runner.py` 在**读取时**要重做归属校验（认领跨用户是共享队列的意义），
    它和路由层判的必须是**同一条**——否则"提交时能选到、跑的时候说不是你的"。
    """
    from app.db import query_one

    sql, params = scope(user, mode)
    return query_one(f"SELECT * FROM datasets WHERE id = ?{sql}", (dataset_id, *params))


def can_read(row: dict[str, Any] | None, user: dict[str, Any]) -> bool:
    """先按 id 把行取出来、再判可见性的场合（媒体 token、报告文件路径）。

    判据与 `scope(SHARED)` 是同一条，只是用 Python 写——**两份实现迟早算出两个结论**，
    所以这里只拿来判**一行**，任何"一批行"的过滤都走 SQL 那条。
    """
    if row is None:
        return False
    uid = _user_id(user)
    if int(row.get("user_id") or 0) == uid:
        return True
    org_id = int(row.get("org_id") or UNASSIGNED_ORG)
    if org_id == UNASSIGNED_ORG:
        return False
    return bool(
        query("SELECT 1 FROM memberships WHERE user_id = ? AND org_id = ?", (uid, org_id))
    )
