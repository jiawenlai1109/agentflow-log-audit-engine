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


def primary_path(ctx: Any, task: dict[str, Any]) -> str:
    """本任务的"主表"路径：跨表任务 = 它点名的第一张表，否则 = Bundle 主表。

    这一条是"授权真的生效"的关键：prompt 与 `DATA_PATH_<id>` 都收窄了，但若 `DATA_PATH`
    仍递未声明的主表，等于放行只在文档上成立。
    """
    tables = task_tables(ctx, task)
    return str(tables[0].path) if tables else str(ctx.data_path)


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
