"""独立校验模板（v1.2）：producer ≠ verifier。

按任务类别（code_hint / description 关键词分类）生成确定性对照计算，在独立子进程
重算关键指标，与 Executor 上报的 aggregate 容差比对（相对误差 ≤ 0.1%）。
无模板覆盖的类别返回 skipped，计入评估指标"校验覆盖率"。
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from agentflow.core.executor import LocalBackend

# 校验模板覆盖的任务类别 → 生成对照代码所需的信息
CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "sum": ("总", "合计", "求和", "总额", "总销售额", "总利润"),
    "count": ("多少笔", "笔数", "订单数", "多少条", "行数", "多少个"),
    "date_trend": ("趋势", "走势", "每日", "按日期", "按天"),
    "category_top": ("对比", "最高", "最好", "排名", "top", "前三", "前3", "哪个"),
}


def classify_task(task: dict[str, Any]) -> str | None:
    """按 code_hint + description 判定任务类别（无模板覆盖返回 None）。"""
    text = f"{task.get('code_hint', '')} {task.get('description', '')}".lower()
    if task.get("code_hint") == "memory_answer":
        return None
    for category, keywords in CATEGORY_HINTS.items():
        if any(k in text for k in keywords):
            return category
    return None


def _numeric_column(schema_profile: dict[str, Any], question: str, task: dict[str, Any]) -> str | None:
    """选择对照计算用的数值列：优先问题中提到的指标词，其次任务必需列，再次首个数值列。"""
    columns = (schema_profile or {}).get("columns") or []
    numeric = [
        c["name"]
        for c in columns
        if isinstance(c.get("dtype"), str) and ("int" in c["dtype"] or "float" in c["dtype"])
    ]
    for keyword in ("利润", "利润率", "销售额", "销量", "金额"):
        if keyword in question:
            for name in numeric:
                if keyword in name:
                    return name
    for col in task.get("required_columns") or []:
        if col in numeric:
            return col
    return numeric[0] if numeric else None


def build_check_code(category: str, numeric_col: str | None) -> str | None:
    """生成确定性对照代码（不接触 Executor 的代码与结果）。"""
    col = numeric_col or "df.columns[0]"
    if category == "sum":
        return (
            "import json, os\n"
            "import pandas as pd\n"
            "df = pd.read_csv(os.environ['DATA_PATH'])\n"
            f"col = {col!r}\n"
            "value = float(pd.to_numeric(df[col], errors='coerce').sum())\n"
            "print(json.dumps({'aggregate': {'合计_' + col: value}, 'rows': int(len(df))}))\n"
        )
    if category == "count":
        return (
            "import json, os\n"
            "import pandas as pd\n"
            "df = pd.read_csv(os.environ['DATA_PATH'])\n"
            "value = float(len(df))\n"
            "print(json.dumps({'aggregate': {'总行数': value}, 'rows': int(len(df))}))\n"
        )
    if category == "date_trend":
        return (
            "import json, os\n"
            "import pandas as pd\n"
            "df = pd.read_csv(os.environ['DATA_PATH'])\n"
            f"col = {col!r}\n"
            "date_col = next((c for c in df.columns if any(k in str(c) for k in ('日期','date','时间'))), None)\n"
            "if date_col is None:\n"
            "    print(json.dumps({'skipped': True}))\n"
            "else:\n"
            "    df[date_col] = pd.to_datetime(df[date_col], errors='coerce')\n"
            "    agg = df.groupby(df[date_col].dt.date)[col].sum().sort_index()\n"
            "    out = {'aggregate': {'合计_' + col: float(agg.sum()), '天数': float(len(agg))},"
            " 'head': [{'日期': str(k), col: float(v)} for k, v in agg.tail(7).items()]}\n"
            "    print(json.dumps(out))\n"
        )
    if category == "category_top":
        return (
            "import json, os\n"
            "import pandas as pd\n"
            "df = pd.read_csv(os.environ['DATA_PATH'])\n"
            f"col = {col!r}\n"
            "cat_cols = [c for c in df.select_dtypes(include=['object']).columns if c != col]\n"
            "cat_cols = [c for c in cat_cols if not any(k in str(c) for k in ('日期','date','时间'))]\n"
            "if not cat_cols:\n"
            "    print(json.dumps({'skipped': True}))\n"
            "else:\n"
            "    agg = df.groupby(cat_cols[0])[col].sum().sort_values(ascending=False)\n"
            "    top1_key = str(agg.index[0]); top1_value = float(agg.iloc[0])\n"
            "    print(json.dumps({'aggregate': {'Top1类别': top1_key, 'Top1数值': top1_value}}))\n"
        )
    return None


def _compare(reported: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, str]:
    """比对上报 aggregate 与重算 aggregate：对每个数值型重算键，找上报中容差匹配项。"""
    matched = 0
    checked = 0
    for key, exp_value in expected.items():
        if isinstance(exp_value, str):
            # 类别型键（Top1类别）：要求上报中出现相同字符串值
            if any(str(v) == exp_value for v in reported.values()):
                matched += 1
                checked += 1
            continue
        if not isinstance(exp_value, (int, float)) or isinstance(exp_value, bool):
            continue
        checked += 1
        tol = max(1e-6, 0.001 * abs(float(exp_value)))
        ok = any(
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isclose(float(v), float(exp_value), rel_tol=0.0, abs_tol=tol)
            for v in reported.values()
        )
        if ok:
            matched += 1
    if checked == 0:
        return False, "重算结果中没有可比对的数值指标"
    if matched == checked:
        return True, f"独立重算 {checked} 项指标全部与上报一致"
    return False, f"独立重算仅 {matched}/{checked} 项与上报一致（容差 0.1%），疑似计算错误"


def _column_from_reported_keys(
    reported: dict[str, Any], schema_columns: list[dict[str, Any]]
) -> str | None:
    """从上报的指标名解析计算列（如 合计_销售额 → 销售额）。

    校验模板应对照"生产者声称计算的指标"重算，而不是按用户问题猜列。
    """
    names = [str(c.get("name")) for c in schema_columns or []]
    for key in reported:
        # 优先长列名匹配，避免"销售额"误吞"净利润销售额"之类
        for col in sorted((n for n in names if n and n in str(key)), key=len, reverse=True):
            return col
    return None


def run_verification(
    task: dict[str, Any],
    result: dict[str, Any],
    data_path: str,
    schema_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行独立校验：返回 {status: pass|fail|skipped, message, expected}。"""
    summary = (result or {}).get("summary") or {}
    reported = summary.get("aggregate") if isinstance(summary.get("aggregate"), dict) else {}
    if not reported:
        return {"status": "skipped", "message": "结果未提供 aggregate，无可校验指标", "expected": None}

    category = classify_task(task)
    if category is None:
        return {"status": "skipped", "message": "任务类别无校验模板覆盖", "expected": None}

    question = task.get("_question", "")
    columns = (schema_profile or {}).get("columns") or []
    numeric_col = _column_from_reported_keys(reported, columns) or _numeric_column(
        schema_profile or {}, question, task
    )
    code = build_check_code(category, numeric_col)
    if code is None:
        return {"status": "skipped", "message": "任务类别无校验模板覆盖", "expected": None}

    work_dir = Path(tempfile.mkdtemp(prefix="verify_"))
    backend = LocalBackend()
    outcome = backend.execute(
        code, work_dir=work_dir, env={"DATA_PATH": str(data_path)}, timeout=60
    )
    if not outcome.success:
        # 校验代码自身失败不判产品代码错，标记 skipped 供人工跟进
        return {
            "status": "skipped",
            "message": f"校验模板执行失败：{(outcome.stderr or '')[-200:]}",
            "expected": None,
        }
    try:
        start = outcome.stdout.index("{")
        expected = json.loads(outcome.stdout[start : outcome.stdout.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {"status": "skipped", "message": "校验模板输出无法解析", "expected": None}
    if expected.get("skipped"):
        return {"status": "skipped", "message": "数据缺少对照计算所需列", "expected": None}

    ok, message = _compare(reported, expected.get("aggregate") or {})
    return {
        "status": "pass" if ok else "fail",
        "message": message,
        "expected": expected.get("aggregate"),
    }
