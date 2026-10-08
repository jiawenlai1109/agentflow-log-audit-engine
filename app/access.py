"""谁能看见哪一行——归属判据**只住在这一个文件里**。

为什么要收成一个函数：改之前 `app/routers/*.py` 里有 22 处各自手写的 `AND user_id = ?`。
那种形状下"加一层企业共享"要改 22 个地方，而漏掉任何一处的表现不是报错，是**该被挡住的
人看见了**——这一类比慢更难查，所以判据不许有第二份。守卫有两条、按 AST 查，都在
`tests/test_auth.py`：`test_every_owned_table_query_is_user_scoped`（路由里不许出现字面量
`user_id = ?`）与 `test_ownership_predicate_has_exactly_one_home`（按**函数**查它有没有引用
`access.scope`）。产物定位那条的守卫在 `tests/test_artifact_namespace.py::test_artifact_location_has_exactly_one_home`（见下面 `visible_run_org` 那条口径）。

口径（用户 2026-10-07 定的四条之一："不同企业隔离，每个企业内部有多个人可以上传文件生成报告"）：

- **SHARED**（读）：`user_id = 我` 或者「这一行的 `org_id <> 0` 且 `org_id` 在我的成员关系里」。
  用在数据集、Bundle、作业与报告——企业内协作共享的就是这几样。
- **OWNER**（删/改，以及会话这类个人上下文）：只有造它的人。删除是破坏性的，默认不给同企业
  的成员；要给的话是单独一条决定（"企业 admin 能删成员的资源"），别顺手放宽。
- `org_id = 0` 表示**未归属企业**：它一律只对 owner 可见。默认拒绝而不是默认放行——
  "没声明归属"不等于"谁都能看"。
- 跨企业/越权统一按"不存在"处理（沿用 M0 的防枚举口径）：调用方拿到 404，不拿到 403。
- **"这条 run 的产物在哪家企业树下"也算归属问题，所以它在这里（`visible_run_org`），不在路由里**。
  把它放进 `app/paths.py` 的话，那个模块就既有判据又有拼接——将来谁只抄拼接那半，就得到一条
  没有归属过滤的定位通道。位置的拼接与回退（`app/paths.py`）不含任何放行判断。

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


def visible_run_org(run_id: str, user: dict[str, Any]) -> int | None:
    """这条 run 的产物在哪家企业的树下；看不见就回 None。

    **先判可见，再给位置**——位置不是第二条判据。所以"取位置"这件事也住在判据家里：
    路由里如果允许出现一条 `SELECT org_id FROM jobs WHERE run_id = ?` 的裸查询，
    它迟早被当成"查得到就是有权限"来用，而那正是把判据写第二遍的后果（该被挡住的人看见了）。

    admin 的旁路沿用 `deps.ensure_run_access` 改造前那条口径：他是**运维主体**，不是
    任何企业的成员，所以对不带归属过滤的查询也拿得到位置。这条例外必须写在这里，
    而且要写明它只回一个整数——它不返回行、不返回内容，读不读得到文件仍由调用方判。

    同一 run_id 挂着多条作业行（重跑、测试残留）时取**最近一条**：最近一次产出它的作业
    决定它落在哪棵树。可见性不依赖这个选择——谓词保证每条候选行本来就是这名调用方看得见的。

    分支顺序是"普通成员先判，运维旁路后走"改成"运维先走自己那一条"：两次都命中不了才是
    看不见。原来那个顺序下 admin 每次读产物要多跑一条必然落空的查询（实测一次查询 4.15ms，
    因为每查都新建连接），在 100 并发那台账上不该白付。
    """
    if user.get("role") == "admin":
        bypass = query_one(
            "SELECT org_id FROM jobs WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
        )
        return int(bypass["org_id"] or UNASSIGNED_ORG) if bypass is not None else None
    sql, params = scope(user)
    row = query_one(
        f"SELECT org_id FROM jobs WHERE run_id = ?{sql} ORDER BY id DESC LIMIT 1",
        (run_id, *params),
    )
    return int(row["org_id"] or UNASSIGNED_ORG) if row is not None else None


def session_org(user_id: int, session_id: str) -> int | None:
    """续轮那次运行要进哪棵会话树：用**作业行的属主**在读取时重做一遍 OWNER 校验。

    为什么运行时要重查（提交时已经查过一次）：job 可能在队列里等一会儿才被认领，
    那段时间里会话可能被人删掉；而"按谁的归属找目录"必须按**作业属于谁**算，
    不能按"此刻正在操作的人"算——认领作业的那个进程根本没有"当前用户"。
    """
    sql, params = scope_by_id(user_id, OWNER)
    row = query_one(
        f"SELECT org_id FROM sessions WHERE session_id = ?{sql}", (session_id, *params)
    )
    return int(row["org_id"] or UNASSIGNED_ORG) if row is not None else None
