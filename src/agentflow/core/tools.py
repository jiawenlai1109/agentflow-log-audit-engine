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


def ensure_authorized(
    ctx: Any,
    path: str | Path,
    task_id: int | None = None,
    grants: list[str] | None = None,
) -> Path:
    """两级授权（安全与隔离设计 §3，v1.2）：

    1. ensure_allowed：run 根目录 / 源数据；
    2. 任务级（依赖边 = 授权边）：task-scoped 调用（executor/visualizer）不可读
       兄弟任务的 work/ 目录；其他任务的 artifacts 必须命中 grants 清单。
       task_id 为空 = 运行级角色（explorer/inspector/critic/reporter），维持运行级放行。
    """
    target = ensure_allowed(ctx, path)
    run_root = Path(ctx.outputs_dir).resolve()
    work_root = run_root / "work"
    art_root = run_root / "artifacts"

    if target == work_root or work_root in target.parents:
        if task_id is None:
            return target
        own = (work_root / str(task_id)).resolve()
        if target != own and own not in target.parents:
            raise PathViolationError(
                f"路径越界：{path} 位于其他任务的 work 目录（work 目录任务私有）"
            )
        return target

    if target == art_root or art_root in target.parents:
        if task_id is None:
            return target
        owner = _artifact_owner(target)
        if owner is not None and owner != int(task_id) and str(target) not in (grants or []):
            raise PathViolationError(
                f"路径越界：{path} 属于任务 {owner} 的产物，未在授权清单（grants）内——依赖边即授权边"
            )
    return target


def _artifact_owner(target: Path) -> int | None:
    """从产物文件名解析归属任务：step_<id>_result.json / chart_task_<id>.png。"""
    import re

    match = re.search(r"(?:^|/)step_(\d+)_result\.json$|(?:^|/)chart_task_(\d+)\.png$", str(target).replace("\\", "/"))
    if not match:
        return None
    return int(match.group(1) or match.group(2))


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

FILTER_HINTS = (
    "筛选",
    "某个",
    "某天",
    "某地区",
    "特定",
    "只有",
    "低于",
    "高于",
    "小于",
    "大于",
    "退款",
    "异常",
    "超出",
)


def _has_filter_hint(text: str) -> bool:
    return any(hint in text for hint in FILTER_HINTS)


# 默认白名单（与 config/agents.yaml 对应；未绑定 config 时作为运行时强制依据）
DEFAULT_TOOL_WHITELIST = {
    "explorer": ["profile_csv"],
    "planner": [],
    "executor": ["execute_python", "read_artifact"],
    "inspector": ["validate_rules", "verify_aggregate", "verify_findings"],
    "visualizer": ["execute_python", "read_artifact"],
    "reporter": [],
    "critic": ["check_report"],
}


class ToolRegistry:
    """统一工具注册表：实现一次，按 Agent 白名单注入可见性并运行时强制（v1.2）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._config = config

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
        whitelist = self._whitelist_for(agent_name, config)
        return [self._tools[name] for name in whitelist if name in self._tools]

    def _whitelist_for(
        self, agent_name: str, config: dict[str, Any] | None = None
    ) -> list[str]:
        source = config or self._config
        if source:
            entry = (source.get("agents", {}).get(agent_name, {}) or {}).get("tools")
            if entry is not None:
                # 显式声明（含空列表 = 明确不给工具）优先
                return list(entry)
        # 未声明时回落默认白名单——配置缺 agents 段不应导致全员被拒
        return list(DEFAULT_TOOL_WHITELIST.get(agent_name, []))

    def _check_whitelist(self, agent_name: str, tool_name: str, ctx: Any) -> None:
        if tool_name in self._whitelist_for(agent_name):
            return
        self._audit(ctx, {"event": "tool_denied_whitelist", "agent": agent_name, "tool": tool_name})
        raise ToolError(f"越权工具调用：{agent_name} 未被授权使用 {tool_name}")

    def _audit(self, ctx: Any, record: dict[str, Any]) -> None:
        transcript = getattr(ctx, "transcript", None)
        if transcript is not None:
            transcript.write(record)

    def call(
        self, agent_name: str, tool_name: str, ctx: Any, _scope: dict[str, Any] | None = None, **params: Any
    ) -> Any:
        """调用工具。

        v1.2 强制：①白名单运行时校验（越权抛 ToolError 并审计）；
        ②带路径语义的参数经 ensure_authorized 两级校验；
        ③task-scoped 调用经 _scope={"task_id", "grants"} 声明授权（依赖边=授权边）。
        """
        tool = self.get(tool_name)
        self._check_whitelist(agent_name, tool_name, ctx)
        scope = _scope or {}
        task_id = scope.get("task_id")
        grants = scope.get("grants")
        guarded: dict[str, Any] = {}
        for key, value in params.items():
            if key in PATH_LIKE_KEYS and isinstance(value, (str, Path)):
                guarded[key] = ensure_authorized(ctx, value, task_id=task_id, grants=grants)
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
        # 样例值是数据原文进 prompt 的通道（提示词注入防线）：截断后仅作画像展示
        samples = [str(value)[:50] for value in series.dropna().head(3).tolist()]
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
    if task.get("rule_params"):
        return _validate_rule_task(result, task)
    summary = result.get("summary") or {}
    rows = summary.get("rows")
    # 防御脏数据：rows 必须是数字，LLM 可能输出数组/字符串，一律按"缺行数"处理，绝不抛异常
    if not isinstance(rows, (int, float)) or isinstance(rows, bool):
        rows = None
    if rows is None:
        if summary.get("aggregate"):
            checks.append(
                {
                    "rule": "empty_check",
                    "level": "WARN",
                    "message": "结果未提供行数信息，但含关键指标（aggregate）",
                }
            )
        else:
            checks.append({"rule": "empty_check", "level": "FAIL", "message": "结果缺少行数信息"})
    elif rows == 0:
        context = f"{task.get('description', '')} {question}"
        if _has_filter_hint(context):
            checks.append(
                {
                    "rule": "empty_check",
                    "level": "WARN",
                    "message": "查询条件较窄，0 行可能为合法结果，请确认筛选条件",
                }
            )
        else:
            checks.append(
                {"rule": "empty_check", "level": "FAIL", "message": "结果为空，建议扩大时间范围或检查字段名"}
            )
    else:
        checks.append({"rule": "empty_check", "level": "PASS", "message": f"结果包含 {rows} 行"})

    required = task.get("required_columns", [])
    columns = summary.get("columns", [])
    missing = [col for col in required if col not in columns]
    if missing:
        # 分组聚合/筛选会消费用于过滤的列（如 groupby 后不再含"地区"），非空结果不判 FAIL
        level = "FAIL" if not rows or rows == 0 else "WARN"
        checks.append(
            {
                "rule": "column_completeness_check",
                "level": level,
                "message": f"结果缺少列：{missing}" + ("（可能被分组/筛选消费，请确认输出完整）" if level == "WARN" else ""),
            }
        )
    else:
        checks.append({"rule": "column_completeness_check", "level": "PASS", "message": "必需列齐全"})

    head = summary.get("head") or []
    if head and isinstance(head[0], dict):
        first = head[0]
        negative = [
            f"{k}={v}" for k, v in first.items() if isinstance(v, (int, float)) and v < 0
        ]
        if negative:
            checks.append(
                {"rule": "negative_value_check", "level": "WARN", "message": f"发现负值（可能为退款）：{negative}"}
            )

    if not summary.get("aggregate"):
        checks.append(
            {
                "rule": "aggregate_check",
                "level": "WARN",
                "message": "结果未提供可验证的关键指标（aggregate），建议输出汇总值",
            }
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


def _validate_rule_task(
    result: dict[str, Any], task: dict[str, Any]
) -> list[dict[str, str]]:
    """场景包规则任务的审核分支（工作规划 §6.2）。

    与通用规则的两处语义差异：
    - 空结果语义反转：安全审计"无发现"是好消息，rows==0 → PASS；
    - required_columns 是输入列而 summary.columns 是输出列，
      列完整性改由 pack 装载预检（planner 阶段）保证，此处跳过。
    """
    checks: list[dict[str, str]] = []
    summary = result.get("summary") or {}
    rows = summary.get("rows")
    if not isinstance(rows, (int, float)) or isinstance(rows, bool):
        rows = None
    findings = summary.get("findings")
    if rows is None:
        checks.append(
            {"rule": "empty_check", "level": "FAIL", "message": "规则任务未提供行数信息"}
        )
    elif rows == 0 and not findings:
        checks.append(
            {
                "rule": "empty_check",
                "level": "PASS",
                "message": "安全审计无发现（空结果语义反转：规则未命中即通过）",
            }
        )
    else:
        checks.append({"rule": "empty_check", "level": "PASS", "message": f"命中 {rows} 条发现"})
    if not summary.get("aggregate"):
        checks.append(
            {
                "rule": "aggregate_check",
                "level": "WARN",
                "message": "规则任务未提供命中数（aggregate），无法进入报告数字核对",
            }
        )
    return checks


def _check_report(
    ctx: Any,
    report_path: str | Path,
    question: str,
    results: dict[int, dict[str, Any]],
    sections: list[str] | None = None,
) -> list[dict[str, str]]:
    """Critic 确定性检查：存在性 / 章节 / 图表路径 / 关键数字。

    sections 由报告方声明（场景包审计模板的章节与零售不同）；缺省保持零售四章节。
    """
    import re

    issues: list[dict[str, str]] = []
    path = ensure_allowed(ctx, report_path)
    if not path.exists():
        return [{"severity": "high", "section": "整体", "message": "报告文件不存在"}]
    text = path.read_text(encoding="utf-8")
    for section in sections or ("总体概况", "数据详情", "趋势分析", "结论与建议"):
        if section not in text:
            issues.append({"severity": "medium", "section": section, "message": f"缺少章节：{section}"})
    for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        ref_path = ensure_allowed(ctx, ref)
        if not ref_path.exists():
            issues.append({"severity": "high", "section": "图表", "message": f"图表引用无效：{ref}"})
    expected: list[float] = []
    for result in results.values():
        summary = result.get("summary") or {}
        aggregate = summary.get("aggregate") or {}
        if isinstance(aggregate, dict):
            for value in aggregate.values():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    expected.append(float(value))
    # 数值解析比对：仅校验"关键指标"（aggregate）必须出现在报告中，样例行不强制
    actual_numbers = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    for number in expected[:10]:
        matched = any(
            abs(number - actual) <= max(1e-6, 1e-4 * abs(number))
            for actual in actual_numbers
        )
        if not matched:
            issues.append(
                {"severity": "medium", "section": "数据详情", "message": f"关键数字 {number} 未出现在报告中"}
            )
    return issues


def _read_artifact(ctx: Any, path: str | Path) -> str:
    target = ensure_allowed(ctx, path)
    return target.read_text(encoding="utf-8")


def _verify_aggregate(
    ctx: Any,
    result: dict[str, Any],
    task: dict[str, Any],
    data_path: str | Path,
    schema_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """独立校验（v1.2，producer ≠ verifier）：按任务类别用确定性模板重算关键指标。"""
    from agentflow.core.verification import run_verification

    return run_verification(
        task=task, result=result, data_path=str(data_path), schema_profile=schema_profile
    )


def _verify_findings(
    ctx: Any,
    task: dict[str, Any],
    result: dict[str, Any],
    data_path: str | Path,
    pack: Any = None,
) -> dict[str, Any]:
    """独立校验（场景包，producer ≠ verifier）：规则 verify_code 异构重算 findings 比对。"""
    from agentflow.core.pack import verify_findings as run_finding_verification

    if pack is None:
        return {"status": "skipped", "message": "未提供场景包，无法校验", "expected": None}
    return run_finding_verification(pack=pack, task=task, result=result, data_path=str(data_path))


def build_default_registry(config: dict[str, Any] | None = None) -> ToolRegistry:
    """注册全部内置工具（与 config/agents.yaml 白名单对应）。"""
    registry = ToolRegistry(config)
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
            name="verify_aggregate",
            description="独立校验模板：重算关键指标并与上报 aggregate 容差比对",
            handler=_verify_aggregate,
        )
    )
    registry.register(
        Tool(
            name="verify_findings",
            description="场景包独立校验：规则 verify_code 异构重算 findings 并按 subject 比对",
            handler=_verify_findings,
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
