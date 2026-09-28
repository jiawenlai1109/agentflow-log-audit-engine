"""场景包机制：装载领域规则包 + finding 独立校验（工作规划 §6.2）。

框架只提供机制，领域知识全部在包内：检测规则、阈值、处置建议、
生产参考实现（reference_code）与异构独立校验器（verify_code）均随包版本化。
核心立场：LLM 不判危险——任务规划与校验全部确定性，LLM 只写实现与研判叙述。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # core/pack.py → agentflow → src → 项目根
PACKS_DIR = PROJECT_ROOT / "packs"

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class PackRule:
    """单条检测规则：判定标准（确定性）+ 处置建议 + 两套异构实现。"""

    id: str
    name: str
    severity: str
    disposition: str
    params: dict[str, Any]
    detection_spec: str
    reference_code: str
    verify_code: str


@dataclass
class ScenarioPack:
    """场景包：Inspector 领域规则包 + Reporter 审计模板 + 数据约定。"""

    name: str
    version: str
    description: str
    required_columns: list[str]
    time_format: str
    rules: list[PackRule]
    report_template: str
    path: Path
    # 实际列名 → 规范名（M3-1）：跨源数据同一实体常有三种叫法
    column_aliases: dict[str, str] = field(default_factory=dict)

    def rule(self, rule_id: str) -> PackRule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(f"场景包 {self.name} 中不存在规则 {rule_id}")

    def canonical(self, column: str) -> str:
        """列的规范名（用于报告与规则表达），没有别名映射时就是原名。"""
        return str(self.column_aliases.get(str(column), column))


def load_pack(name: str) -> ScenarioPack:
    """装载场景包；目录/规则缺失即抛 ValueError（由编排器走降级路径）。"""
    import yaml

    root = PACKS_DIR / name
    rules_path = root / "rules.yaml"
    template_path = root / "report_template.md"
    if not rules_path.exists() or not template_path.exists():
        raise ValueError(f"场景包不完整：{root}（需 rules.yaml 与 report_template.md）")
    data = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    conv = data.get("data_convention") or {}
    rules = [
        PackRule(
            id=str(entry["id"]),
            name=str(entry.get("name", entry["id"])),
            severity=str(entry.get("severity", "medium")),
            disposition=str(entry.get("disposition", "")),
            params=entry.get("params") or {},
            detection_spec=str(entry.get("detection_spec", "")).strip(),
            reference_code=str(entry.get("reference_code", "")).strip(),
            verify_code=str(entry.get("verify_code", "")).strip(),
        )
        for entry in (data.get("rules") or [])
    ]
    if not rules:
        raise ValueError(f"场景包 {name} 未定义检测规则")
    return ScenarioPack(
        name=str(data.get("name", name)),
        version=str(data.get("version", "1")),
        description=str(data.get("description", "")),
        required_columns=[str(c) for c in (conv.get("required_columns") or [])],
        time_format=str(conv.get("time_format", "%Y-%m-%d %H:%M:%S")),
        rules=rules,
        report_template=template_path.read_text(encoding="utf-8"),
        path=root,
        # 别名按列名匹配，不按表 id：id 取决于用户先传哪个文件，按 id 写会静默失效
        column_aliases={
            str(actual): str(canonical)
            for actual, canonical in (conv.get("column_aliases") or {}).items()
        },
    )


def pack_plan_tasks(pack: ScenarioPack) -> list[dict[str, Any]]:
    """按规则目录确定性生成检测任务（不调 LLM——LLM 不判危险）。"""
    return [
        {
            "task_id": index,
            "description": f"检测规则{rule.id}：{rule.name}",
            "required_columns": list(pack.required_columns),
            "code_hint": f"rule_pack:{rule.id}",
            "chart_type": "none",
            "depends_on": [],
            "upstream_refs": [],
            "rule_params": {"id": rule.id},
        }
        for index, rule in enumerate(pack.rules, start=1)
    ]


def verify_findings(
    pack: ScenarioPack,
    task: dict[str, Any],
    result: dict[str, Any],
    data_path: str,
) -> dict[str, Any]:
    """producer ≠ verifier（场景包版）：用规则自带 verify_code 独立重算并按 subject 比对。

    返回 {status: pass|fail|skipped, message, expected}，语义与 core/verification.run_verification 对齐。
    """
    rule_id = str((task.get("rule_params") or {}).get("id", ""))
    try:
        rule = pack.rule(rule_id)
    except KeyError:
        return {"status": "skipped", "message": f"任务规则 {rule_id} 不在场景包内", "expected": None}
    summary = (result or {}).get("summary") or {}
    reported = summary.get("findings")
    if not isinstance(reported, list):
        return {"status": "skipped", "message": "结果未提供 findings 数组，无法校验", "expected": None}

    expected = _run_verify_code(rule, data_path)
    if expected is None:
        return {"status": "skipped", "message": "独立校验器执行失败或输出不可解析", "expected": None}

    exp_map = {str(f.get("subject")): f for f in expected if isinstance(f, dict)}
    rep_map = {str(f.get("subject")): f for f in reported if isinstance(f, dict)}
    mismatches: list[str] = []
    for subject, exp in exp_map.items():
        got = rep_map.get(subject)
        if got is None:
            mismatches.append(f"漏报 {rule_id} {subject}（独立复算={exp.get('value')}）")
        elif not _close(got.get("value"), exp.get("value")):
            mismatches.append(
                f"{rule_id} {subject} 上报={got.get('value')} 独立复算={exp.get('value')}"
            )
    for subject in rep_map:
        if subject not in exp_map:
            mismatches.append(f"误报 {rule_id} {subject}")

    if mismatches:
        return {"status": "fail", "message": "；".join(mismatches)[:300], "expected": expected}
    message = (
        f"{len(expected)} 条命中，独立复算一致" if expected else "无发现，独立复算一致"
    )
    return {"status": "pass", "message": message, "expected": expected}


def _close(actual: Any, expected: Any) -> bool:
    """数值容差比对（与 verification._compare 一致）；非数值退化为字符串相等。"""
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError):
        return str(actual) == str(expected)
    return math.isclose(a, b, rel_tol=0.0, abs_tol=max(1e-6, 0.001 * abs(b)))


def _run_verify_code(rule: PackRule, data_path: str) -> list[Any] | None:
    """子进程执行独立校验器，返回期望 findings 列表；失败返回 None（skipped 语义）。"""
    import tempfile

    from agentflow.core.executor import LocalBackend

    if not rule.verify_code.strip():
        return None
    work_dir = Path(tempfile.mkdtemp(prefix="pack_verify_"))
    backend = LocalBackend()
    outcome = backend.execute(
        rule.verify_code,
        work_dir=work_dir,
        env={"DATA_PATH": str(data_path)},
        timeout=60,
    )
    if not outcome.success:
        return None
    try:
        start = outcome.stdout.index("{")
        data = json.loads(outcome.stdout[start : outcome.stdout.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    findings = data.get("findings")
    return findings if isinstance(findings, list) else None
