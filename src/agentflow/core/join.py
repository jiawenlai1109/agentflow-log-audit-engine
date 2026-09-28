"""join 预检器（M2-3）：**派发前**用确定性基数判断拦住坏 join。

为什么放在派发前而不是执行后：笛卡尔积一旦被 LLM 写出来并执行，代价是内存与一条
已经被下游引用的错误结果；而"这个键连得上吗、连上会膨胀几倍"根本不需要执行——
只读两张表的键列做 value_counts 相乘就能得到**精确**的结果行数。

三条判定（顺序敏感）：
1. `no_key`：两表没有同名键列（也没给别名）→ 计划层面的错，交给 Planner 修订；
2. `no_overlap`：键值零重叠 → 该 join 恒为空。注意这与场景包的"空结果语义反转"
   （规则查不到 = 通过）**不是一回事**：那条说的是 finding 的语义，这里说的是
   "键根本选错了"，属于计划缺陷，不能当发现放过去；
3. `expansion`：结果行数 / 两侧最大行数 超阈值，或绝对行数超上界 → 笛卡尔积风险。

基数按 pandas 默认读取口径计算（不做 str 归一），与执行器里真正 `merge` 的口径一致，
否则预检与执行会算出两个数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentflow.core.bundle import canonical_column, column_for_canonical

# 结果膨胀比上界：join 后行数 / 两侧最大行数。合法的多对一汇总通常 <2，
# 4 倍已经足够宽松地放行"一个主机多条事件"这类真实多对一
MAX_EXPANSION_RATIO = 4.0
# 结果绝对行数上界（与 Bundle 的 MAX_TABLE_ROWS 同量级，防住"比例合法但行数爆炸"）
MAX_JOIN_ROWS = 200_000
# 低于该重叠率只记警告，不拒绝：部分匹配在事实表↔维表里是正常的
LOW_OVERLAP_WARNING = 0.05


@dataclass
class PairCheck:
    """一对表在某个键上的预检结果。"""

    left: str
    right: str
    key: str
    left_rows: int
    right_rows: int
    expected_rows: int
    expansion_ratio: float
    overlap: float
    left_key_unique: float
    right_key_unique: float
    ok: bool
    reason_code: str = "ok"
    detail: str = ""
    # 规范名之外的实际列名（包内别名场景：t1.src_ip ↔ t2.主机，规范名 主机）
    left_column: str = ""
    right_column: str = ""

    def __post_init__(self) -> None:
        self.left_column = self.left_column or self.key
        self.right_column = self.right_column or self.key

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "key": self.key,
            "left_column": self.left_column,
            "right_column": self.right_column,
            "left_rows": self.left_rows,
            "right_rows": self.right_rows,
            "expected_rows": self.expected_rows,
            "expansion_ratio": round(self.expansion_ratio, 3),
            "overlap": round(self.overlap, 3),
            "ok": self.ok,
            "reason": self.reason_code,
            "detail": self.detail,
        }


@dataclass
class JoinPreflight:
    """一次任务的 dataset_refs 预检总判定（任一配对被拒即整体不通过）。"""

    refs: list[str]
    pairs: list[PairCheck] = field(default_factory=list)
    ok: bool = True
    reason_code: str = "ok"
    detail: str = ""

    @property
    def rejected(self) -> list[PairCheck]:
        return [pair for pair in self.pairs if not pair.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "refs": self.refs,
            "ok": self.ok,
            "reason": self.reason_code,
            "detail": self.detail,
            "pairs": [pair.as_dict() for pair in self.pairs],
            "expected_rows": max((pair.expected_rows for pair in self.pairs), default=0),
        }

    def __str__(self) -> str:  # 进 transcript / 错误路由消息，一行可读
        if self.ok:
            return f"join 预检通过 {self.refs}：" + "；".join(
                f"{p.left}⋈{p.right}({p.key}) 期望 {p.expected_rows} 行 / 膨胀 {p.expansion_ratio:.2f}x"
                for p in self.pairs
            )
        return f"join 预检拒绝 {self.refs}：[{self.reason_code}] {self.detail}"


def _read_keys(path: Any, column: str) -> tuple[Any, int]:
    """只读键列，返回 (整列含空值, 该表总行数)。

    不 dropna：pandas `merge` 会把两侧的空值当相等（实测 NaN×NaN 出一行），预检要
    与执行口径一致，否则算出的行数与真正 join 的结果不同。
    """
    import pandas as pd

    frame = pd.read_csv(path, usecols=[column])
    return frame[column], int(len(frame))


def _key_kind(dtype: Any) -> str:
    import pandas as pd

    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype):
        return "text"
    return "other"


def _incompatible(left_dtype: Any, right_dtype: Any) -> bool:
    """数值列 join 文本列会被 pandas 直接拒绝（实测 int64 vs str 抛 ValueError）。

    这类键不是"零重叠"，而是根本连不上——单独给 reason，别把执行期崩溃说成空结果。
    """
    kinds = {_key_kind(left_dtype), _key_kind(right_dtype)}
    return kinds in ({"number", "text"}, {"bool", "text"})


def check_pair(
    left_id: str,
    left_path: Any,
    right_id: str,
    right_path: Any,
    key: str,
    *,
    left_columns: list[str] | None = None,
    right_columns: list[str] | None = None,
    left_column: str | None = None,
    right_column: str | None = None,
) -> PairCheck:
    """一对表在 `key` 上的精确基数预检。

    `key` 是规范名；两表的实际列名由 `left_column` / `right_column` 给出（包内别名场景）。
    不传就要求两侧都存在同名的 `key`——这是没有别名时的旧语义，一个字节没变。
    """
    left_columns = left_columns or []
    right_columns = right_columns or []
    left_column = left_column or (key if key in left_columns else "")
    right_column = right_column or (key if key in right_columns else "")
    if not left_column or not right_column:
        return PairCheck(
            left=left_id,
            right=right_id,
            key=key,
            left_rows=0,
            right_rows=0,
            expected_rows=0,
            expansion_ratio=0.0,
            overlap=0.0,
            left_key_unique=0.0,
            right_key_unique=0.0,
            ok=False,
            reason_code="no_key",
            left_column=left_column or key,
            right_column=right_column or key,
            detail=(
                f"键列 {key} 不是两表共有：{left_id} 有 {left_columns}，{right_id} 有 {right_columns}"
                "（跨表列名不统一时需要在场景包 data_convention.column_aliases 里给出映射）"
            ),
        )

    left_keys, left_rows = _read_keys(left_path, left_column)
    right_keys, right_rows = _read_keys(right_path, right_column)

    if left_rows == 0 or right_rows == 0:
        # 空表是合法输入（空语义反转靠它）：join 恒为空不是计划错误
        return PairCheck(
            left=left_id,
            right=right_id,
            key=key,
            left_rows=left_rows,
            right_rows=right_rows,
            expected_rows=0,
            expansion_ratio=0.0,
            overlap=0.0,
            left_key_unique=0.0,
            right_key_unique=0.0,
            ok=True,
            reason_code="empty_side",
            left_column=left_column,
            right_column=right_column,
            detail=f"{left_id if left_rows == 0 else right_id} 是空表（0 行），该 join 恒为空——按空语义放行",
        )

    if _incompatible(left_keys.dtype, right_keys.dtype):
        return PairCheck(
            left=left_id,
            right=right_id,
            key=key,
            left_rows=left_rows,
            right_rows=right_rows,
            expected_rows=0,
            expansion_ratio=0.0,
            overlap=0.0,
            left_key_unique=0.0,
            right_key_unique=0.0,
            ok=False,
            reason_code="dtype_mismatch",
            left_column=left_column,
            right_column=right_column,
            detail=(
                f"{left_id}.{left_column} 是 {left_keys.dtype} 而 {right_id}.{right_column} 是 "
                f"{right_keys.dtype}：数值键与文本键在 pandas 里无法 join（执行期会直接报错），"
                "需要先在包内做列映射/归一"
            ),
        )

    left_counts = left_keys.value_counts(dropna=False)
    right_counts = right_keys.value_counts(dropna=False)
    # 精确结果行数 = Σ 左键计数 × 右键计数（等价于 inner join 的行数，但不物化）
    common = left_counts.index.intersection(right_counts.index)
    expected_rows = (
        int((left_counts.loc[common] * right_counts.loc[common]).sum()) if len(common) else 0
    )

    # 重叠率只看非空键：全空列不该被说成"命中得很好"
    left_values = set(left_keys.dropna().unique())
    right_values = set(right_keys.dropna().unique())
    left_unique = len(left_values)
    overlap = len(left_values & right_values) / left_unique if left_unique else 0.0
    baseline = max(left_rows, right_rows, 1)
    ratio = expected_rows / baseline
    # 消息里点名实际列名：别名场景下只说规范名，读的人无法判断哪一侧叫什么
    label = f"{left_id}.{left_column} ↔ {right_id}.{right_column}" + (
        f"（规范名 {key}）" if left_column != right_column else ""
    )
    # 一行的表（汇总/单例）不该因"整表复制到每一行"被判膨胀
    if baseline > 1 and ratio > MAX_EXPANSION_RATIO:
        ok, code, detail = False, "expansion", (
            f"{label}：期望 {expected_rows} 行 = 两侧最大行数 {baseline} 的 "
            f"{ratio:.2f} 倍（上界 {MAX_EXPANSION_RATIO}x）——键在多侧都不唯一，接近笛卡尔积"
        )
    elif expected_rows > MAX_JOIN_ROWS:
        ok, code, detail = False, "expansion", (
            f"{label}：期望 {expected_rows} 行超过 join 上界 {MAX_JOIN_ROWS}"
        )
    elif expected_rows == 0:
        ok, code, detail = False, "no_overlap", (
            f"{label}：键值零重叠"
            f"（左表非空键值 {left_unique} 个，无一命中右表{'' if left_unique else '或键列全为空值'}），"
            "该 join 恒为空——通常是键选错或跨表列名不统一"
        )
    else:
        ok, code, detail = True, "ok", ""

    return PairCheck(
        left=left_id,
        right=right_id,
        key=key,
        left_rows=left_rows,
        right_rows=right_rows,
        expected_rows=expected_rows,
        expansion_ratio=ratio,
        overlap=overlap,
        left_key_unique=float(left_unique) / max(1, int(left_keys.notna().sum())),
        right_key_unique=float(int(right_keys.dropna().nunique())) / max(
            1, int(right_keys.notna().sum())
        ),
        ok=ok,
        reason_code=code,
        detail=detail,
        left_column=left_column,
        right_column=right_column,
    )


def resolve_key(
    bundle: Any,
    refs: list[str],
    keys: list[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> str | None:
    """任务声明的 join_keys 优先；没声明时确定性给出候选键：优先在所有 refs 表里共有（含包内
    别名认定的同义列）、且取值重叠最高的规范名（与 Bundle.join_candidates 同一口径，
    不另起一套判断）。
    """
    declared = [key for key in (keys or []) if key]
    if declared:
        return declared[0]
    tables = [table for table in bundle.tables if table.id in refs]
    if len(tables) < 2:
        return None
    # 规范名集合的交集：别名让 `src_ip` 与 `主机` 算同一个实体
    canonicals = {canonical_column(column, aliases) for column in tables[0].columns}
    for table in tables[1:]:
        canonicals &= {canonical_column(column, aliases) for column in table.columns}
    candidates_all = bundle.join_candidates(aliases)
    if canonicals:
        candidates = [
            candidate
            for candidate in candidates_all
            if candidate["column"] in canonicals
            and {candidate["left"], candidate["right"]} <= set(refs)
        ]
        usable = [candidate for candidate in candidates if candidate["usable"]]
        if usable:
            return str(usable[0]["column"])
        if candidates:
            return str(candidates[0]["column"])
        return str(sorted(canonicals)[0])
    return None


def preflight(
    bundle: Any,
    refs: list[str],
    keys: list[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> JoinPreflight:
    """按任务的 dataset_refs / join_keys 做派发前预检。

    多表（refs ≥ 3）按"声明顺序的两两相邻配对"检查：这是配对级基数的上界估计，
    不是整条 join 链的精确行数——链式基数需要物化中间结果，代价与收益不匹配。
    `aliases`（实际列名 → 规范名）来自场景包，让 `src_ip ↔ 主机` 这类跨源同义键可被认出来。
    """
    declared = list(refs or [])
    if len(declared) < 2:
        return JoinPreflight(refs=declared, ok=True, reason_code="single_table")
    unknown = [ref for ref in declared if ref not in {table.id for table in bundle.tables}]
    if unknown:
        return JoinPreflight(
            refs=declared,
            ok=False,
            reason_code="unknown_ref",
            detail=f"任务声明的表 id 不在 Bundle 里：{unknown}（Bundle 只有 {[t.id for t in bundle.tables]}）",
        )

    table_by_id = {table.id: table for table in bundle.tables}
    key_list = [key for key in (keys or []) if key]
    fallback_key = resolve_key(bundle, declared, [], aliases)
    pairs: list[PairCheck] = []
    for index in range(len(declared) - 1):
        left = table_by_id[declared[index]]
        right = table_by_id[declared[index + 1]]
        # 声明的键按配对顺序逐个用；不够用时沿用最后一个（同键链式 join）
        if key_list:
            key = key_list[index] if index < len(key_list) else key_list[-1]
        else:
            key = fallback_key
        if not key:
            return JoinPreflight(
                refs=declared,
                ok=False,
                reason_code="no_key",
                detail=(
                    f"{left.id} 与 {right.id} 没有可对应的键列（含包内别名后仍无交集），"
                    "任务也未声明 join_keys"
                ),
            )
        pairs.append(
            check_pair(
                left.id,
                left.path,
                right.id,
                right.path,
                key,
                left_columns=left.columns,
                right_columns=right.columns,
                left_column=column_for_canonical(left.columns, key, aliases),
                right_column=column_for_canonical(right.columns, key, aliases),
            )
        )

    for pair in pairs:
        if not pair.ok:
            return JoinPreflight(
                refs=declared, pairs=pairs, ok=False, reason_code=pair.reason_code, detail=pair.detail
            )
    warning = next(
        (
            pair
            for pair in pairs
            if pair.overlap < LOW_OVERLAP_WARNING
        ),
        None,
    )
    detail = ""
    if warning is not None:
        detail = (
            f"警告：{warning.left}⋈{warning.right} on {warning.key} 键值重叠率仅 "
            f"{warning.overlap:.3f}（低于 {LOW_OVERLAP_WARNING}），结果可能只剩极少量行"
        )
    return JoinPreflight(refs=declared, pairs=pairs, ok=True, reason_code="ok", detail=detail)
