"""工具系统：统一 ToolRegistry + 能力授权 + 路径守卫 + 内置工具处理器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class ToolError(RuntimeError):
    """工具调用失败。"""


class PathViolationError(ToolError):
    """路径越界：请求路径不在授权范围内。"""


def ensure_within(root: Path, path: str | Path) -> Path:
    """校验 path 位于 root 之内（解析后），拒绝 .. 与越界绝对路径。"""
    root = Path(root).resolve()
    raw = Path(path)
    target = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if target != root and root not in target.parents:
        raise PathViolationError(f"路径越界：{path} 不在 {root} 内")
    return target


def ensure_allowed(ctx: Any, path: str | Path) -> Path:
    """运行级授权：outputs_dir 之内，或等于源数据路径（data_path）。"""
    root = Path(ctx.outputs_dir).resolve()
    raw = Path(path)
    target = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if target == root or root in target.parents:
        return target
    if ctx.data_path and target == Path(ctx.data_path).resolve():
        return target
    raise PathViolationError(f"路径越界：{path} 不在授权范围内")


@dataclass
class Tool:
    """工具定义：名称、描述、参数 JSON Schema、处理函数。"""

    name: str
    description: str
    handler: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=dict)


PATH_LIKE_KEYS = {
    "path",
    "file",
    "dir",
    "file_path",
    "output_path",
    "data_path",
    "work_dir",
    "result_path",
    "chart_path",
    "report_path",
}


class ToolRegistry:
    """统一工具注册表：实现一次，按 Agent 白名单注入可见性。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"未注册的工具：{name}") from exc

    def allowed_tools(
        self, agent_name: str, config: dict[str, Any] | None = None
    ) -> list[Tool]:
        whitelist: list[str] = []
        if config:
            whitelist = (config.get("agents", {}).get(agent_name, {}) or {}).get(
                "tools", []
            )
        return [self._tools[name] for name in whitelist if name in self._tools]

    def call(self, agent_name: str, tool_name: str, ctx: Any, **params: Any) -> Any:
        """调用工具；所有带路径语义的参数先做运行级授权校验。"""
        tool = self.get(tool_name)
        guarded: dict[str, Any] = {}
        for key, value in params.items():
            if key in PATH_LIKE_KEYS and isinstance(value, (str, Path)):
                guarded[key] = ensure_allowed(ctx, value)
            else:
                guarded[key] = value
        return tool.handler(ctx=ctx, **guarded)


# ---------------------------------------------------------------- handlers


def _detect_encoding(path: Path) -> str:
    for encoding in ("utf-8-sig", "gbk", "gb18030", "utf-8"):
        try:
            path.read_text(encoding=encoding)
            return encoding
        except (UnicodeDecodeError, OSError):
            continue
    return "utf-8"


def _try_date(series: Any) -> tuple[bool, str | None, str | None]:
    import pandas as pd
    import warnings

    try:
        parsed = pd.to_datetime(series.dropna(), format="%Y-%m-%d", errors="raise")
    except (ValueError, TypeError, OverflowError):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                parsed = pd.to_datetime(series.dropna(), errors="raise")
            except (ValueError, TypeError, OverflowError):
                return False, None, None
    if parsed.empty:
        return False, None, None
    return True, str(parsed.min().date()), str(parsed.max().date())


def _profile_csv(ctx: Any, data_path: str | Path) -> dict[str, Any]:
    """确定性数据画像：编码检测 → 读取 → SchemaProfile。"""
    import pandas as pd

    from agentflow.schemas.profile import ColumnProfile, SchemaProfile

    path = ensure_allowed(ctx, data_path)
    encoding = _detect_encoding(path)
    df = pd.read_csv(path, encoding=encoding)
    columns: list[ColumnProfile] = []
    for col in df.columns:
        series = df[col]
        missing_rate = float(series.isna().mean())
        unique_rate = float(series.nunique()) / max(1, len(series))
        samples = [str(value) for value in series.dropna().head(3).tolist()]
        is_date, cmin, cmax = _try_date(series)
        columns.append(
            ColumnProfile(
                name=str(col),
                dtype=str(series.dtype),
                missing_rate=round(missing_rate, 4),
                unique_rate=round(unique_rate, 4),
                sample=samples,
                is_date=is_date,
                min=cmin,
                max=cmax,
            )
        )
    issues = [
        f"列 {c.name} 缺失率 {c.missing_rate:.0%}"
        for c in columns
        if c.missing_rate > 0.5
    ]
    profile = SchemaProfile(
        file_path=str(path),
        encoding=encoding,
        row_count=len(df),
        column_count=len(df.columns),
        columns=columns,
        suggested_date_column=next((c.name for c in columns if c.is_date), None),
        issues=issues,
    )
    return profile.model_dump(mode="json")


def _execute_python(
    ctx: Any,
    code: str,
    work_dir: str | Path,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> Any:
    """本地子进程执行生成代码（LocalBackend）。"""
    from agentflow.core.executor import LocalBackend

    backend = LocalBackend()
    return backend.execute(code, work_dir, env=env or {}, timeout=timeout)


def _validate_rules(
    ctx: Any,
    result: dict[str, Any],
    task: dict[str, Any],
    question: str,
    schema_profile: dict[str, Any],
) -> list[dict[str, str]]:
    """Inspector 确定性规则：空结果 / 列完整性 / 负值 / 行数合理性。"""
    checks: list[dict[str, str]] = []
    summary = result.get("summary") or {}
    rows = summary.get("rows")
    if rows is None:
        checks.append({"rule": "empty_check", "level": "FAIL", "message": "结果缺少行数信息"})
    elif rows == 0:
        checks.append(
            {"rule": "empty_check", "level": "FAIL", "message": "结果为空，建议扩大时间范围或检查字段名"}
        )
    else:
        checks.append({"rule": "empty_check", "level": "PASS", "message": f"结果包含 {rows} 行"})

    required = task.get("required_columns", [])
    columns = summary.get("columns", [])
    missing = [col for col in required if col not in columns]
    if missing:
        checks.append(
            {"rule": "column_completeness_check", "level": "FAIL", "message": f"结果缺少列：{missing}"}
        )
    else:
        checks.append({"rule": "column_completeness_check", "level": "PASS", "message": "必需列齐全"})

    head = summary.get("head") or []
    if head:
        first = head[0]
        negative = [
            f"{k}={v}" for k, v in first.items() if isinstance(v, (int, float)) and v < 0
        ]
        if negative:
            checks.append(
                {"rule": "negative_value_check", "level": "WARN", "message": f"发现负值（可能为退款）：{negative}"}
            )

    total = (schema_profile or {}).get("row_count")
    if rows is not None and total is not None:
        if rows > total:
            checks.append({"rule": "row_count_check", "level": "FAIL", "message": "结果行数超过源数据"})
        elif rows == total and "全部" not in str(question):
            checks.append(
                {"rule": "row_count_check", "level": "WARN", "message": "结果行数与源数据一致，疑似未按条件筛选"}
            )
    return checks


def _check_report(
    ctx: Any,
    report_path: str | Path,
    question: str,
    results: dict[int, dict[str, Any]],
) -> list[dict[str, str]]:
    """Critic 确定性检查：存在性 / 章节 / 图表路径 / 关键数字。"""
    import re

    issues: list[dict[str, str]] = []
    path = ensure_allowed(ctx, report_path)
    if not path.exists():
        return [{"severity": "high", "section": "整体", "message": "报告文件不存在"}]
    text = path.read_text(encoding="utf-8")
    for section in ("总体概况", "数据详情", "趋势分析", "结论建议"):
        if section not in text:
            issues.append({"severity": "medium", "section": section, "message": f"缺少章节：{section}"})
    for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        ref_path = ensure_allowed(ctx, ref)
        if not ref_path.exists():
            issues.append({"severity": "high", "section": "图表", "message": f"图表引用无效：{ref}"})
    numbers: list[str] = []
    for result in results.values():
        summary = result.get("summary") or {}
        for row in (summary.get("head") or [])[:3]:
            for key, value in row.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numbers.append(str(value))
    for number in numbers[:5]:
        if number not in text:
            issues.append(
                {"severity": "medium", "section": "数据详情", "message": f"关键数字 {number} 未出现在报告中"}
            )
    return issues


def _read_artifact(ctx: Any, path: str | Path) -> str:
    target = ensure_allowed(ctx, path)
    return target.read_text(encoding="utf-8")


def build_default_registry() -> ToolRegistry:
    """注册全部内置工具（与 config/agents.yaml 白名单对应）。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="profile_csv",
            description="读取 CSV 并生成真实 schema 画像（列名/类型/缺失率/日期）",
            handler=_profile_csv,
            parameters={"type": "object", "properties": {"data_path": {"type": "string"}}},
        )
    )
    registry.register(
        Tool(
            name="execute_python",
            description="在本地子进程执行生成代码（超时/静态扫描/env 白名单）",
            handler=_execute_python,
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "work_dir": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "env": {"type": "object"},
                },
            },
        )
    )
    registry.register(
        Tool(
            name="validate_rules",
            description="Inspector 确定性审核规则",
            handler=_validate_rules,
        )
    )
    registry.register(
        Tool(
            name="check_report",
            description="Critic 确定性报告检查（章节/图表/数字）",
            handler=_check_report,
        )
    )
    registry.register(
        Tool(
            name="read_artifact",
            description="读取授权范围内产物文件内容",
            handler=_read_artifact,
        )
    )
    return registry
