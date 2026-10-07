"""dataset_scope：一个任务"能碰哪几张表"的单一口径（#19）。

三个消费方必须问同一份答案，否则迟早出现三种"授权范围"：
- executor：注入 prompt 的可用列、env 里的表路径、工具参数守卫的 `dataset_refs`；
- visualizer：画图子进程拿到的 `DATA_PATH`（跨表任务必须是本任务的第一张表）；
- inspector：独立校验时按声明的表重放 join（producer ≠ verifier）。

策略也只在这里定一次：**声明少于两张表 = 单表语义，不做表级收窄**——既有计划与场景包
规则任务都没有 refs，收窄它们等于偷偷改语义。
"""

from __future__ import annotations

from typing import Any

# 声明两张以上才叫"跨表任务"
MIN_MULTI_REFS = 2


def declared_refs(task: dict[str, Any]) -> list[str]:
    """任务原样声明的表 id（含只声明一张的情况）。"""
    return [str(ref) for ref in (task.get("dataset_refs") or [])]


def scope_refs(task: dict[str, Any]) -> list[str]:
    """参与表级放行的 id：不足两张返回空集 = 不收窄。"""
    refs = declared_refs(task)
    return refs if len(refs) >= MIN_MULTI_REFS else []


def is_multi_table(task: dict[str, Any]) -> bool:
    return bool(scope_refs(task))


def task_tables(ctx: Any, task: dict[str, Any]) -> list[Any]:
    """按声明顺序取 Bundle 里的表对象；未构成跨表任务时为空列表。"""
    refs = scope_refs(task)
    if not refs:
        return []
    bundle = getattr(ctx, "bundle", None)
    by_id = {str(table.id): table for table in getattr(bundle, "tables", []) or []}
    return [by_id[ref] for ref in refs if ref in by_id]


def table_paths(ctx: Any, task: dict[str, Any]) -> dict[str, str]:
    """声明表的归一化 CSV 路径，键是表 id（env 与校验器都用这份）。"""
    return {str(table.id): str(table.path) for table in task_tables(ctx, task)}


def aliases_of(ctx: Any) -> dict[str, str]:
    """包内列别名（实际列名 → 规范名）；通用分析没有领域知识判断同义列，故为空前。"""
    return dict(getattr(getattr(ctx, "pack", None), "column_aliases", {}) or {})


def join_pairs(ctx: Any, task: dict[str, Any]) -> list[dict[str, Any]]:
    """把 (声明顺序相邻的表对, 规范键) 落到两侧**实际列名**。

    这是别名机制唯一被消费的地方：执行器据此写"先 rename 再 merge"的指令，
    校验器据此重放。列名对不上时预检已经报 `no_key`，所以这里不做二次判断。
    """
    from agentflow.core.bundle import column_for_canonical

    tables = task_tables(ctx, task)
    if len(tables) < 2:
        return []
    aliases = aliases_of(ctx)
    keys = [str(key) for key in (task.get("join_keys") or []) if key]
    out: list[dict[str, Any]] = []
    for index in range(len(tables) - 1):
        left, right = tables[index], tables[index + 1]
        canonical = (
            keys[index] if index < len(keys) else (keys[-1] if keys else None)
        ) or canonical_column_of(left, right, aliases)
        if canonical is None:
            continue
        out.append(
            {
                "left": str(left.id),
                "right": str(right.id),
                "canonical": str(canonical),
                "left_column": column_for_canonical(left.columns, str(canonical), aliases)
                or str(canonical),
                "right_column": column_for_canonical(right.columns, str(canonical), aliases)
                or str(canonical),
            }
        )
    return out


def canonical_column_of(left: Any, right: Any, aliases: dict[str, str]) -> str | None:
    """两张表在别名口径下的第一个共有规范名。"""
    from agentflow.core.bundle import canonical_column

    left_names = {canonical_column(column, aliases) for column in left.columns}
    right_names = {canonical_column(column, aliases) for column in right.columns}
    shared = sorted(left_names & right_names)
    return shared[0] if shared else None


def primary_path(ctx: Any, task: dict[str, Any]) -> str:
    """本任务的"主表"路径：跨表任务 = 它点名的第一张表；规则任务 = 解析出的第一个角色表；
    其余 = Bundle 主表。

    这一条是"授权真的生效"的关键：prompt 与 `DATA_PATH_<id>` 都收窄了，但若 `DATA_PATH`
    仍递未声明的主表，等于放行只在文档上成立。

    C-14 的两处修正（都在这一函数里，因为口径只该有一处）：

    ① **声明了 `primary_ref` 就按它选主表**。以前 refs 的*顺序*赢过声明，三源场景下
       "把 auth.csv 当主表"这句声明会被 t1=assets 盖掉，规则在错的数据上跑出空结果，
       而 status 照样 success。
    ② **解析不到不许静默退回 Bundle 主表**。声明了两张以上却一张都不在 Bundle 里，
       那是计划错了；让它去读主表等于把"缺表"洗成"用别的表算出来了"——报错，
       由调用方收容成单任务失败（#22 的立场：不作废整条 run）。没有声明的任务照旧走
       主表——这是本模块写死的单表策略，不改。
    ③ 每次解析都留痕（`ctx.table_resolutions` + transcript 事件 `table_resolved`），
       评测层才看得见"实际递给沙箱的是哪张表"。只记表 id 与文件名，**不记绝对路径**（#15）。
    """
    tables = task_tables(ctx, task)
    ref = str(task.get("primary_ref") or "")
    if tables:
        chosen = next((table for table in tables if str(table.id) == ref), None)
        _note(ctx, task, chosen or tables[0], "primary_ref" if chosen else "declared_order")
        return str((chosen or tables[0]).path)
    if ref:
        for table in getattr(getattr(ctx, "bundle", None), "tables", []) or []:
            if str(table.id) == ref:
                _note(ctx, task, table, "primary_ref_only")
                return str(table.path)
    declared = scope_refs(task)
    if declared:
        known = sorted(
            str(table.id) for table in (getattr(getattr(ctx, "bundle", None), "tables", []) or [])
        )
        raise DatasetScopeError(
            f"任务 {task.get('task_id')} 声明的表 {declared} 一张都不在 Bundle 里"
            f"（可用表：{known}）。不能退回 Bundle 主表——那等于用没声明的表算出一个看着很正常的结论。"
        )
    fallback = getattr(getattr(ctx, "bundle", None), "tables", []) or []
    _note(ctx, task, fallback[0] if fallback else None, "bundle_default")
    return str(ctx.data_path)


class DatasetScopeError(RuntimeError):
    """声明的表范围无法满足（C-14②）。错误名原样进 `error_class`，路由按类型不按文案。"""


def _note(ctx: Any, task: dict[str, Any], table: Any, via: str) -> None:
    """解析结果落进 ctx 与 transcript——"实际递了哪张表"从此可断言、可回归。"""
    record = {
        "task_id": task.get("task_id"),
        "declared_refs": declared_refs(task),
        "primary_ref": str(task.get("primary_ref") or ""),
        "via": via,
        "table_id": str(table.id) if table is not None else None,
        "table_file": getattr(table, "source_file", None) if table is not None else None,
    }
    resolutions = getattr(ctx, "table_resolutions", None)
    if isinstance(resolutions, dict):
        # 同一任务会被 env 组装问多次：留最后一次，并按 task_id 归位（不按调用顺序）
        resolutions[str(record["task_id"])] = record
    transcript = getattr(ctx, "transcript", None)
    if transcript is not None:
        key = (str(record["task_id"]), via, record["table_id"])
        seen = getattr(ctx, "_table_resolution_events", None)
        if seen is None:
            seen = set()
            setattr(ctx, "_table_resolution_events", seen)
        if key not in seen:
            seen.add(key)
            transcript.write({"event": "table_resolved", **record})


def column_scope(ctx: Any, task: dict[str, Any]) -> list[str]:
    """可用列口径：单表沿用主表画像；跨表 = 所声明几张表的列并集（不掺未声明的表）。"""
    tables = task_tables(ctx, task)
    if not tables:
        return [str(col["name"]) for col in ((getattr(ctx, "schema_profile", None) or {}).get("columns") or [])]
    ordered: list[str] = []
    for table in tables:
        for column in table.columns:
            if column not in ordered:
                ordered.append(column)
    return ordered
