"""独立校验模板（v1.2）：producer ≠ verifier。

按任务类别生成确定性对照代码，在独立子进程重算指标，与 Executor 上报的 aggregate
按「指标族 + 列名」取交集比对（相对误差 ≤ 0.1%）。

三条关键语义（都是 real 批次踩坑后固化的）：
1. **只对照双方都算过的同一指标**：生产者常报不同命名/不同口径的指标，无交集 → skipped，
   不计入失败，只计入"校验覆盖率"；
2. **作用域多解**：任务是否受时间窗影响无法总是推断，因此模板同时给出"全量"和"最近 N 天"
   两种口径，上报值命中任一即视为一致；两者都不命中才算真不一致；
3. **不可复现的时间限定**（上周/本月/环比等未量化窗）→ 直接 skipped，不做猜测式判罚。
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from agentflow.core.executor import LocalBackend

# 顺序敏感：更具体的类别先匹配。不用裸"多少"做计数线索（"总销售额是多少"是求和问句）
CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "count": ("多少笔", "笔数", "订单数", "多少条", "行数", "多少个", "多少行", "条数", "count"),
    "date_trend": ("趋势", "走势", "每日", "按日期", "按天", "日度"),
    "category_top": ("对比", "最高", "最好", "排名", "top", "前三", "前3", "哪个", "排行"),
    "sum": ("合计", "总", "求和", "总额", "累计", "sum"),
}

# 上报指标名 → 指标族（顺序敏感：先判计数/类别，避免"总数"被 sum 吞掉）
_FAMILY_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("name", ("类别", "类目", "品类", "类型", "哪种", "哪个", "产品名")),
    ("count", ("行数", "天数", "笔数", "条数", "个数", "订单数", "总数", "数量", "count")),
    ("max", ("最高", "最大", "top1", "top", "峰值", "最多")),
    ("min", ("最低", "最小", "谷值", "最少")),
    ("sum", ("合计", "总额", "总量", "累计", "求和", "sum", "总")),
]

_FAMILY_TO_CATEGORY = {"sum": "sum", "count": "count", "max": "category_top", "name": "category_top"}

_WINDOW_RE = re.compile(r"(?:最近|近|前|过去)?\s*(\d{1,3})\s*天")
# 未量化的时间限定：模板无法确定同一作用域，放弃校验而不是猜
_AMBIGUOUS_SCOPE_RE = re.compile(r"上周|本周|本月|上月|当周|当月|季度|环比|同比|工作日|周末")


def parse_window_days(text: str) -> int | None:
    """从文本解析"最近 N 天"（项目约定：以数据集最大日期为基准，见需求分析 §1.5）。"""
    match = _WINDOW_RE.search(text or "")
    if not match:
        return None
    days = int(match.group(1))
    return days if 0 < days <= 3650 else None


def has_ambiguous_scope(text: str) -> bool:
    return bool(_AMBIGUOUS_SCOPE_RE.search(text or ""))


def classify_task(task: dict[str, Any]) -> str | None:
    """按 code_hint + description 判定任务类别（无模板覆盖返回 None）。"""
    text = f"{task.get('code_hint', '')} {task.get('description', '')}".lower()
    if task.get("code_hint") == "memory_answer":
        return None
    for category, keywords in CATEGORY_HINTS.items():
        if any(k in text for k in keywords):
            return category
    return None


def _parse_family(key: str) -> str | None:
    lowered = str(key).lower()
    for family, words in _FAMILY_WORDS:
        if any(word in lowered for word in words):
            return family
    return None


def _parse_column(key: str, columns: list[str]) -> str | None:
    """从指标名解析它针对哪一列（优先长列名，避免短名误吞）。"""
    text = str(key)
    for col in sorted((c for c in columns if c and c in text), key=len, reverse=True):
        return col
    return None


def _numeric_columns(schema_profile: dict[str, Any], question: str, task: dict[str, Any]) -> list[str]:
    """对照计算可尝试的数值列顺序：问题提到的指标 → 任务必需列 → 其余数值列。"""
    columns = (schema_profile or {}).get("columns") or []
    numeric = [
        str(c.get("name"))
        for c in columns
        if isinstance(c.get("dtype"), str) and ("int" in c["dtype"] or "float" in c["dtype"])
    ]
    for keyword in ("利润", "利润率", "销售额", "销量", "金额"):
        if keyword in question:
            exact = [name for name in numeric if name == keyword]
            partial = [name for name in numeric if keyword in name]
            if exact or partial:
                return exact + partial + [n for n in numeric if n not in exact + partial]
    ordered = [c for c in task.get("required_columns") or [] if c in numeric]
    return ordered + [n for n in numeric if n not in ordered]


def _column_from_reported_keys(reported: dict[str, Any], columns: list[str]) -> str | None:
    """从上报指标名解析计算列（如 合计_销售额 → 销售额）：对照生产者声称算的是什么。"""
    for key in reported:
        col = _parse_column(key, columns)
        if col:
            return col
    return None


def build_check_code(category: str, columns: list[str], window: int | None = None) -> str | None:
    """生成对照代码，输出 metrics=[{family, column, value, scope}]。

    scope ∈ {"full", "window"}：同一指标给出口径变体，命中任一即视为一致。
    """
    col_list = repr(columns)
    header = (
        "import json, os\n"
        "import pandas as pd\n"
        "df = pd.read_csv(os.environ['DATA_PATH'])\n"
        f"_COLS = {col_list}\n"
        f"_WINDOW = {window!r}\n"
        "def pick(cands):\n"
        "    for c in cands:\n"
        "        if c in df.columns:\n"
        "            return c\n"
        "    num = df.select_dtypes(include='number').columns.tolist()\n"
        "    return num[0] if num else None\n"
        "def dated(frame):\n"
        "    dc = next((c for c in frame.columns if any(k in str(c) for k in ('日期','date','时间'))), None)\n"
        "    if dc is None:\n"
        "        return None, frame, dc\n"
        "    parsed = pd.to_datetime(frame[dc], errors='coerce')\n"
        "    if parsed.dropna().empty:\n"
        "        return None, frame, dc\n"
        "    return parsed, frame, dc\n"
        "def windowed(frame, parsed):\n"
        "    if parsed is None or not _WINDOW:\n"
        "        return frame\n"
        "    cutoff = parsed.max() - pd.Timedelta(days=_WINDOW - 1)\n"
        "    out = frame[parsed >= cutoff]\n"
        "    return out if len(out) else frame\n"
        "metrics = []\n"
        "_done = False\n"
        "def add(family, column, value, scope):\n"
        "    if value is None:\n"
        "        return\n"
        "    metrics.append({'family': family, 'column': column, 'value': float(value) if not isinstance(value, str) else value, 'scope': scope})\n"
        "col = pick(_COLS)\n"
        "if col is None:\n"
        "    _done = True\n"
        "    print(json.dumps({'skipped': True}))\n"
    )
    skip_stmts = (
        "        _done = True\n"
        "        print(json.dumps({'skipped': True}))\n"
    )
    body = {
        "sum": (
            "else:\n"
            "    series = pd.to_numeric(df[col], errors='coerce')\n"
            "    add('sum', col, series.sum(), 'full')\n"
            "    parsed, frame, dc = dated(df)\n"
            "    if _WINDOW:\n"
            "        sub = windowed(df, parsed)\n"
            "        add('sum', col, pd.to_numeric(sub[col], errors='coerce').sum(), 'window')\n"
        ),
        "count": (
            "else:\n"
            "    add('count', None, len(df), 'full')\n"
            "    parsed, frame, dc = dated(df)\n"
            "    if _WINDOW:\n"
            "        add('count', None, len(windowed(df, parsed)), 'window')\n"
        ),
        "date_trend": (
            "else:\n"
            "    parsed, frame, dc = dated(df)\n"
            "    if dc is None or parsed is None:\n"
            + skip_stmts
            + "    else:\n"
            "        def agg_of(data):\n"
            "            return data.groupby(pd.to_datetime(data[dc], errors='coerce').dt.date)[col].sum().dropna().sort_index()\n"
            "        variants = [('full', df)]\n"
            "        if _WINDOW:\n"
            "            variants.append(('window', windowed(df, parsed)))\n"
            "        for scope, sub in variants:\n"
            "            agg = agg_of(sub)\n"
            "            if agg.empty:\n"
            "                continue\n"
            "            if scope == 'window':\n"
            "                agg = agg.tail(_WINDOW)\n"
            "            add('sum', col, agg.sum(), scope)\n"
            "            add('count', None, len(agg), scope)\n"
            "            add('max', col, agg.max(), scope)\n"
            "            add('min', col, agg.min(), scope)\n"
        ),
        "category_top": (
            "else:\n"
            "    cats = [c for c in df.select_dtypes(include=['object']).columns\n"
            "            if c != col and not any(k in str(c) for k in ('日期','date','时间'))]\n"
            "    if not cats:\n"
            + skip_stmts
            + "    else:\n"
            "        parsed, frame, dc = dated(df)\n"
            "        variants = [('full', df)]\n"
            "        if _WINDOW:\n"
            "            variants.append(('window', windowed(df, parsed)))\n"
            "        for scope, sub in variants:\n"
            "            agg = sub.groupby(cats[0])[col].sum().sort_values(ascending=False)\n"
            "            if agg.empty:\n"
            "                continue\n"
            "            add('max', col, agg.iloc[0], scope)\n"
            "            add('name', cats[0], str(agg.index[0]), scope)\n"
        ),
    }.get(category)
    if body is None:
        return None
    tail = (
        "if not _done:\n"
        "    print(json.dumps({'metrics': metrics}) if metrics else json.dumps({'skipped': True}))\n"
    )
    return header + body + tail


def _compare(
    reported: dict[str, Any], metrics: list[dict[str, Any]], columns: list[str]
) -> tuple[str, str, int]:
    """按（指标族 + 列名）取交集比对，同族同列的任意口径变体命中即一致。

    返回 (status, message, comparable)；comparable=0 表示无交集（不可判）。
    """
    comparable = 0
    mismatches: list[str] = []
    for key, value in reported.items():
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        family = _parse_family(key)
        if family is None:
            continue
        column = _parse_column(key, columns)
        candidates = []
        for metric in metrics:
            if metric.get("family") != family:
                continue
            metric_column = metric.get("column")
            if column and metric_column and column != metric_column:
                continue
            candidates.append(metric)
        if not candidates:
            continue
        exp_numbers = [
            m["value"] for m in candidates if isinstance(m.get("value"), (int, float)) and not isinstance(m.get("value"), bool)
        ]
        exp_strings = [m["value"] for m in candidates if isinstance(m.get("value"), str)]
        if is_number and exp_numbers:
            comparable += 1
            ok = any(
                math.isclose(float(value), float(exp), rel_tol=0.0, abs_tol=max(1e-6, 0.001 * abs(float(exp))))
                for exp in exp_numbers
            )
            if not ok:
                mismatches.append(f"{key}={value} 对照值={exp_numbers[:3]}")
        elif (not is_number) and exp_strings:
            comparable += 1
            if not any(str(value).strip() == exp.strip() for exp in exp_strings):
                mismatches.append(f"{key}={value} 对照值={exp_strings[:3]}")
        # 类型不一致（如上报"最大日期"是字符串而模板只有数值）不参与对照
    if comparable == 0:
        return "skipped", "上报指标与对照模板无共同指标（命名/口径不同），无法独立校验", 0
    if mismatches:
        return (
            "fail",
            f"{len(mismatches)}/{comparable} 项指标与独立重算不一致：" + "；".join(mismatches[:3]),
            comparable,
        )
    return "pass", f"独立重算 {comparable} 项可比指标全部与上报一致（容差 0.1%）", comparable


def _candidate_categories(task: dict[str, Any], reported: dict[str, Any], question: str) -> list[str]:
    """候选模板：描述+问题判定优先，上报指标族反推兜底（最多两个，控制开销）。"""
    candidates: list[str] = []
    primary = classify_task({**task, "description": f"{task.get('description', '')} {question}"})
    if primary:
        candidates.append(primary)
    for key in reported:
        mapped = _FAMILY_TO_CATEGORY.get(_parse_family(key) or "")
        if mapped and mapped not in candidates:
            candidates.append(mapped)
    return candidates[:2]


def _run_template(
    category: str, columns: list[str], window: int | None, data_path: str
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """执行一个对照模板，返回 (metrics, 不可用原因)。"""
    code = build_check_code(category, columns, window)
    if code is None:
        return None, "任务类别无校验模板覆盖"
    work_dir = Path(tempfile.mkdtemp(prefix="verify_"))
    outcome = LocalBackend().execute(
        code, work_dir=work_dir, env={"DATA_PATH": str(data_path)}, timeout=60
    )
    if not outcome.success:
        return None, f"校验模板执行失败：{(outcome.stderr or '')[-200:]}"
    try:
        start = outcome.stdout.index("{")
        parsed = json.loads(outcome.stdout[start : outcome.stdout.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None, "校验模板输出无法解析"
    metrics = parsed.get("metrics") or []
    if parsed.get("skipped") or not metrics:
        return None, "数据缺少对照计算所需列"
    return metrics, None


def build_join_check_code(paths: list[str], key: str) -> str:
    """多表任务的对照代码（#19）：**换一条算法**重放声明的 join。

    行数用两侧键列 value_counts 相乘得到（与 `core/join.py` 预检同一公式，但与生产者写的
    `merge` 不是同一条路径），同时把 `merge` 的行数一并上报——两个算法互为校验：
    预检、重放、执行三处算出同一个数，这个数字才算立住。
    """
    return (
        "import json\n"
        "import pandas as pd\n"
        f"_PATHS = {paths!r}\n"
        f"_KEY = {key!r}\n"
        "_LEFT = pd.read_csv(_PATHS[0])\n"
        "_RIGHT = pd.read_csv(_PATHS[1])\n"
        "_LC = _LEFT[_KEY].value_counts(dropna=False)\n"
        "_RC = _RIGHT[_KEY].value_counts(dropna=False)\n"
        "_COMMON = _LC.index.intersection(_RC.index)\n"
        "_PRODUCT = int((_LC.loc[_COMMON] * _RC.loc[_COMMON]).sum()) if len(_COMMON) else 0\n"
        "_MERGED = _LEFT.merge(_RIGHT, on=_KEY, how='inner')\n"
        "metrics = [{'family': 'count', 'column': None, 'value': float(_PRODUCT), 'scope': 'join'}]\n"
        "if len(_MERGED):\n"
        "    metrics.append({'family': 'count', 'column': None, 'value': float(len(_MERGED)), 'scope': 'join_merge'})\n"
        "    _NUM = next((c for c in _MERGED.select_dtypes(include='number').columns if c != _KEY), None)\n"
        "    if _NUM is not None:\n"
        "        _total = 0.0\n"
        "        for _v in _MERGED[_NUM].tolist():\n"
        "            _total += float(_v)\n"
        "        metrics.append({'family': 'sum', 'column': _NUM, 'value': _total, 'scope': 'join'})\n"
        "print(json.dumps({'metrics': metrics}) if metrics else json.dumps({'skipped': True}))\n"
    )


def _run_join_template(
    paths: list[str], key: str
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """在独立子进程重放 join（与执行器的实现异构），返回 (metrics, 不可用原因)。"""
    work_dir = Path(tempfile.mkdtemp(prefix="verify_join_"))
    code = build_join_check_code(paths, key)
    outcome = LocalBackend().execute(code, work_dir=work_dir, env={}, timeout=60)
    if not outcome.success:
        return None, f"join 对照模板执行失败：{(outcome.stderr or '')[-200:]}"
    try:
        start = outcome.stdout.index("{")
        parsed = json.loads(outcome.stdout[start : outcome.stdout.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None, "join 对照模板输出无法解析"
    metrics = parsed.get("metrics") or []
    if parsed.get("skipped") or not metrics:
        return None, "join 重放没有产出可比指标"
    return metrics, None


def run_verification(
    task: dict[str, Any],
    result: dict[str, Any],
    data_path: str,
    schema_profile: dict[str, Any] | None = None,
    table_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """执行独立校验：返回 {status: pass|fail|skipped, message, expected}。

    `table_paths` 给出且声明了两张以上表 ⇒ 走多表 join 重放；否则维持单表类别模板。
    """
    summary = (result or {}).get("summary") or {}
    reported = summary.get("aggregate") if isinstance(summary.get("aggregate"), dict) else {}
    if not reported:
        return {"status": "skipped", "message": "结果未提供 aggregate，无可校验指标", "expected": None}

    refs = [str(ref) for ref in (task.get("dataset_refs") or [])]
    if len(refs) >= 2 and table_paths:
        keys = [str(key) for key in (task.get("join_keys") or [])]
        paths = [str(table_paths[ref]) for ref in refs if ref in table_paths]
        if len(paths) < 2:
            return {"status": "skipped", "message": "声明的表路径不全，无法重放 join", "expected": None}
        if not keys:
            return {"status": "skipped", "message": "跨表任务未声明 join_keys，无确定作用域可重放", "expected": None}
        columns = [str(c.get("name")) for c in ((schema_profile or {}).get("columns") or [])]
        metrics, reason = _run_join_template(paths, keys[0])
        if reason:
            return {"status": "skipped", "message": reason, "expected": None}
        status, message, comparable = _compare(reported, metrics, columns)
        if comparable == 0:
            return {
                "status": "skipped",
                "message": "上报指标与 join 重放无共同指标（命名/口径不同），无法独立校验",
                "expected": metrics,
            }
        return {"status": status, "message": f"[join 重放] {message}", "expected": metrics}

    question = task.get("_question", "")
    task_text = f"{task.get('description', '')} {task.get('code_hint', '')}"
    scope_text = f"{task_text} {question}"
    # 未量化的时间限定（上周/环比…）无法确定同一作用域 → 放弃校验，不做猜测式判罚
    if parse_window_days(scope_text) is None and has_ambiguous_scope(scope_text):
        return {"status": "skipped", "message": "任务含未量化的时间限定，无法确定对照作用域", "expected": None}

    candidates = _candidate_categories(task, reported, question)
    if not candidates:
        return {"status": "skipped", "message": "任务类别无校验模板覆盖", "expected": None}

    columns = [str(c.get("name")) for c in ((schema_profile or {}).get("columns") or [])]
    ordered = _numeric_columns(schema_profile or {}, question, task)
    preferred = _column_from_reported_keys(reported, columns)
    check_columns = ([preferred] if preferred else []) + [c for c in ordered if c != preferred]
    window = parse_window_days(scope_text)

    fallback: dict[str, Any] | None = None
    for category in candidates:
        metrics, reason = _run_template(category, check_columns, window, data_path)
        if reason:
            fallback = fallback or {"status": "skipped", "message": reason, "expected": None}
            continue
        status, message, comparable = _compare(reported, metrics, columns)
        if comparable == 0:
            fallback = fallback or {"status": "skipped", "message": message, "expected": metrics}
            continue
        return {"status": status, "message": message, "expected": metrics}
    return fallback or {"status": "skipped", "message": "无可用对照模板", "expected": None}
