"""每企业配额：只在**受理**那一刻判，判定不进引擎，也不进 worker。

为什么住在受理层（P4-2 的分工）：引擎要回答"这份数据怎么分析"，配额要回答"这家企业现在
还能不能再收一个作业"。后者跟着**人**（user → org）走，而且只有一个出口能拒绝——受理那一刻。
worker 认领之后已经没有"拒绝"这个出口了，那时才发现超配额只能把作业跑成失败，
于是"配额"变成"失败计数"，而 P4 的退出判据要的恰恰是失败计数不涨。

三条口径：

1. **配额住在库里**（`org_quotas`），不在配置文件里：它是按客户谈的东西，改了不该发版、
   不该在重启时丢。`limit_*` 为 None = 这一项不设限（不是 0，也不是"随便"——见下条）。
2. **判据的输入是库里的全局事实**（这家企业此刻几个在排队/在跑、今天提交了多少个），
   不是本进程的簿记：多进程部署下后者只看得见自己那几个。
3. **哪几项没生效要自曝**。表里有四列，这一片只接了两项（并发作业数、每日作业数）；
   `limit_llm_calls_per_day` 需要作业行带上每次运行的 LLM 用量（现在只在 `evaluation.json`
   里，属迁移范围），`limit_upload_bytes` 要接在上传入口。设了值不生效比不设更坏——
   运维以为拦住了，其实没有。所以这两项在 `UNENFORCED` 里，admin 写配额时当场看到。

被拒的留痕在哪：一条 `agentflow.accept` 的 warning 日志（企业号、判据、当前用量与上限）。
**没有进库**，因为受理被拒不留 `jobs` 行——这正是这条路径要的性质：没跑成的事不能变成
一个看起来合法的失败作业计数。要给运维一张持久账，得加一张表，那与 #95（登录/建号限流）
是同一个需求，那片一起做。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.db import execute, query_one

logger = logging.getLogger("agentflow.accept")

# 表里的列 → 这一项判的是哪条用量。顺序即判据顺序：先判"此刻占着坑的"，再判"今天累计的"。
ENFORCED = {
    "limit_concurrent_jobs": "active",
    "limit_jobs_per_day": "today",
}
# 认得、但不参与判定：设了值会在 admin 的响应里当场说明"这条现在不生效"。
UNENFORCED = ("limit_llm_calls_per_day", "limit_upload_bytes")
LIMIT_COLUMNS = (*ENFORCED, *UNENFORCED)

# 被拒时回给客户端的退避建议（秒）。**不是预测**——没人知道前面那个作业还要跑多久，
# 它是给客户端的一个数：比"立刻重试"长，比一次分析的耗时短。真实排队时间看闸门读数。
DEFAULT_RETRY_AFTER_S = int(os.getenv("QUOTA_RETRY_AFTER_S", "30"))


def quota_for(org_id: int) -> dict[str, Any]:
    """这家企业的配额行。没有行 = 一条都没设（全不设限），返回空 dict 而不是造一个默认值。

    为什么不发默认值：发一个"看起来合理"的默认上限，等于让每个没谈过配额的企业都被同一
    个数拦下，而那件事没人会去查——它表现为"提交时不时 429"，排查的人第一反应是网关坏了。
    """
    row = query_one(
        "SELECT org_id, limit_concurrent_jobs, limit_jobs_per_day, "
        "limit_llm_calls_per_day, limit_upload_bytes, updated_at "
        "FROM org_quotas WHERE org_id = ?",
        (int(org_id),),
    )
    return dict(row) if row else {}


def decide(org_id: int, usage: dict[str, int] | None = None) -> dict[str, Any]:
    """能不能收这一个。返回结论 + 可行动原因，**不返回 HTTP 状态码**（那是路由的事）。"""
    from app import queueing  # 在这里取而不是模块级：quota 与 queueing 会互相指（用量查在队列那侧）

    caps = quota_for(org_id)
    used = usage if usage is not None else queueing.org_usage(org_id)
    for column, key in ENFORCED.items():
        cap = caps.get(column)
        if cap is None:
            continue
        cap = int(cap)
        if cap <= 0:
            # 0 与负数不是"锁死这家企业"，是配错了：停一家企业应该有显式的停用位，而不是把上限写 0。
            # 判成"拦住"会让人以为生效了，判成"不设限"更是直接放行——所以这里报成一条配置错误。
            return {
                "allowed": False,
                "kind": "quota_misconfigured",
                "limit": column,
                "cap": cap,
                "used": int(used.get(key) or 0),
                "retry_after": DEFAULT_RETRY_AFTER_S,
                "reason": (
                    f"这家企业的 {column} 被设成 {cap}，那不是上限而是配错："
                    "0 与负数不当「禁止提交」用。请管理员把它改成正确条数或留空（不设限）。"
                ),
            }
        if int(used.get(key) or 0) >= cap:
            return {
                "allowed": False,
                "kind": "quota_exceeded",
                "limit": column,
                "cap": cap,
                "used": int(used.get(key) or 0),
                "retry_after": DEFAULT_RETRY_AFTER_S,
                "reason": _exceeded_reason(column, cap, int(used.get(key) or 0)),
            }
    return {"allowed": True, "kind": "ok", "limit": None, "used": used, "cap": None}


def _exceeded_reason(column: str, cap: int, used: int) -> str:
    if column == "limit_concurrent_jobs":
        return (
            f"这家企业同时最多 {cap} 个作业（排队 + 在跑都算），此刻已有 {used} 个。"
            f"前面的跑完就会轮到——同企业内部按提交顺序，跨企业按谁占的坑少先领谁。"
        )
    return f"这家企业每天最多提交 {cap} 个作业，今天已经提交了 {used} 个（明天零点重置）。"


def note_refusal(decision: dict[str, Any], org_id: int, actor: dict[str, Any] | None = None) -> None:
    """被拒要留痕：一句 warning，带企业、判据、当前用量与上限、发起人。

    这条不许升成异常，也不许吞掉判定——留痕失败不能让一个该拒的请求变成放行（fail-open 必留痕
    是反面的同一件事：这里是 fail-closed + 留痕）。
    """
    logger.warning(
        "配额拒绝：org=%s 判据=%s 当前=%s 上限=%s 原因类=%s 发起人=%s",
        org_id,
        decision.get("limit"),
        decision.get("used"),
        decision.get("cap"),
        decision.get("kind"),
        (actor or {}).get("username"),
    )


def set_quota(org_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """写配额（admin 用）。**PATCH 语义**：`values` 里出现的列才写，没出现的保持原值。

    为什么不是整行覆盖：漏传一列就把它悄悄清成"不设限"，表现是"配额忽然失效了而没人改过"
    ——这种事故查起来最贵，因为它看起来什么都没发生。反过来，**显式传 null 就是清除**：
    区分"没提"与"提了但要清空"是调用方（路由用 `exclude_unset`）的事，这里按传进来的键判。

    列名只取自 `LIMIT_COLUMNS` 这份白名单，取值一律走参数绑定：拼进语句的只有列名，
    而列名不是用户能写的东西。
    """
    provided = {column: _positive_or_none(column, values[column]) for column in LIMIT_COLUMNS if column in values}
    if not provided:
        return quota_for(org_id)
    stamp = "datetime('now', 'localtime')"
    existing = query_one("SELECT id FROM org_quotas WHERE org_id = ?", (int(org_id),))
    if existing:
        assignments = ", ".join(f"{column} = ?" for column in provided)
        execute(
            f"UPDATE org_quotas SET {assignments}, updated_at = {stamp} WHERE org_id = ?",
            (*provided.values(), int(org_id)),
        )
    else:
        columns = ", ".join(("org_id", *provided))
        marks = ", ".join("?" for _ in range(len(provided) + 1))
        execute(
            f"INSERT INTO org_quotas ({columns}, updated_at) VALUES ({marks}, {stamp})",
            (int(org_id), *provided.values()),
        )
    return quota_for(org_id)


def _positive_or_none(column: str, raw: Any) -> int | None:
    """配额只接受正整数或 None（不设限）。0 与负数是配错，当场报，不留进库里等下一次判定。"""
    if raw is None:
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{column} 必须是正整数或留空（留空 = 不设限），收到 {value}")
    return value


def capabilities() -> dict[str, Any]:
    """这一片**实际拦得住**的是哪几项、认得但不生效的是哪几项。给 admin 响应与文档用。"""
    return {
        "enforced": sorted(ENFORCED),
        "unenforced": list(UNENFORCED),
        "note": (
            "limit_llm_calls_per_day 与 limit_upload_bytes 现在只存不判：前者要作业行带上每次运行的 "
            "LLM 用量（属迁移范围），后者要接在上传入口。设了它们不会生效，这条在响应里当场说。"
        ),
    }
