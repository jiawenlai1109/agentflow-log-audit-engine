"""场景包机制：装载领域规则包 + finding 独立校验（工作规划 §6.2）。

框架只提供机制，领域知识全部在包内：检测规则、阈值、处置建议、
生产参考实现（reference_code）与异构独立校验器（verify_code）均随包版本化。
核心立场：LLM 不判危险——任务规划与校验全部确定性，LLM 只写实现与研判叙述。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # core/pack.py → agentflow → src → 项目根
PACKS_DIR = PROJECT_ROOT / "packs"

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# 场景包名是**授权面**的一部分，不是展示字符串：一旦 Web/API 收下调用方给的包名，
# `PACKS_DIR / name` 里带 `..` 或路径分隔符就能把仓库上一级目录当成包目录读进来。
# 这里按名字形状先拒（引擎侧下限），API 侧再按"可用包名单"拒（业务侧下限）。
PACK_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


@dataclass
class PackRule:
    """单条检测规则：判定标准（确定性）+ 处置建议 + 两套异构实现。

    `requires` 是跨表规则的关键设计：规则按**角色**声明它需要哪些列
    （`{auth: [auth_result, account], assets: [是否生产]}`），系统在运行时把每个角色解析成
    一张真实的表，并以 `DATA_PATH_<角色大写>` 注入实现与校验器。
    规则里不写 t1/t2，也不写 DATA_PATH_T1——表 id 取决于用户先传哪个文件，
    写死 id 的规则换一次上传顺序就静默指向别的表，而且没人会察觉。
    """

    id: str
    name: str
    severity: str
    disposition: str
    params: dict[str, Any]
    detection_spec: str
    reference_code: str
    verify_code: str
    requires: dict[str, list[str]] = field(default_factory=dict)
    join_keys: list[str] = field(default_factory=list)

    def resolve(self, pack: "ScenarioPack", bundle: Any) -> dict[str, Any] | None:
        """角色 → 表对象。任一角色找不到承载表就返回 None（规则不可执行，不猜）。"""
        if not self.requires or bundle is None:
            return {}
        resolved: dict[str, Any] = {}
        for role, columns in self.requires.items():
            wanted = {pack.canonical(column) for column in columns}
            table = next(
                (
                    item
                    for item in bundle.tables
                    if wanted <= {pack.canonical(column) for column in item.columns}
                ),
                None,
            )
            if table is None:
                return None
            resolved[str(role)] = table
        return resolved


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
    # 领域措辞属于包，不属于框架：reporter 里硬编码"登录日志安全审计"时，
    # 第二个场景包一接进来就会得到一份说错话的报告
    subject_label: str = "审计"
    report_sections: list[str] = field(
        default_factory=lambda: ["发现清单", "处置建议", "研判摘要", "审计说明"]
    )
    # 报告分档承诺（M3-3）：{"fact": [...], "action": [...], "inference": [...]}
    # 没承诺分档的包（如零售报告）不会被检查——检查一个没做承诺的结构等于凭空加判据
    report_layers: dict[str, list[str]] = field(default_factory=dict)
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

    if not PACK_NAME_RE.match(str(name)):
        raise ValueError(
            f"非法场景包名：{name!r}（只允许字母/数字/下划线开头，可含 . 与 -，长度 ≤64，"
            "不接受路径分隔符或 ..）"
        )
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
            requires={
                str(role): [str(column) for column in (columns or [])]
                for role, columns in (entry.get("requires") or {}).items()
            },
            join_keys=[str(k) for k in (entry.get("join_keys") or [])],
        )
        for entry in (data.get("rules") or [])
    ]
    if not rules:
        raise ValueError(f"场景包 {name} 未定义检测规则")
    template_text = template_path.read_text(encoding="utf-8")
    layers = {
        str(kind): [str(name) for name in (names or [])]
        for kind, names in (data.get("report_layers") or {}).items()
    }
    # 声明了档但模板里没有 ⇒ 那一档永远不会被读到，分档检查会静默变成空检查。
    # 这种不一致必须在装载时报出来，不能等到报告出问题再猜。
    for names in layers.values():
        for layer in names:
            if layer not in template_text:
                raise ValueError(
                    f"场景包 {name} 的 report_layers 声明了「{layer}」，"
                    f"但 report_template.md 里没有这一档"
                )
    return ScenarioPack(
        name=str(data.get("name", name)),
        version=str(data.get("version", "1")),
        description=str(data.get("description", "")),
        required_columns=[str(c) for c in (conv.get("required_columns") or [])],
        time_format=str(conv.get("time_format", "%Y-%m-%d %H:%M:%S")),
        rules=rules,
        report_template=template_text,
        path=root,
        subject_label=str(data.get("subject_label", "审计")),
        report_sections=[str(s) for s in (data.get("report_sections") or [])]
        or ["发现清单", "处置建议", "研判摘要", "审计说明"],
        report_layers=layers,
        # 别名按列名匹配，不按表 id：id 取决于用户先传哪个文件，按 id 写会静默失效
        column_aliases={
            str(actual): str(canonical)
            for actual, canonical in (conv.get("column_aliases") or {}).items()
        },
    )


def role_env(pack: ScenarioPack, bundle: Any, rule: PackRule) -> dict[str, str]:
    """规则角色 → `DATA_PATH_<角色大写>`。执行器与校验器都靠它拿到表，
    所以两边必须是同一个解析结果（同一函数），否则校验器在校验另一份数据。
    """
    resolved = rule.resolve(pack, bundle) or {}
    return {
        f"DATA_PATH_{str(role).upper()}": str(table.path)
        for role, table in resolved.items()
    }


def list_packs() -> tuple[list[ScenarioPack], list[dict[str, str]]]:
    """列出可用场景包，返回 (可用的包, 坏掉的目录及原因)。

    坏目录**不静默跳过**：装载失败被藏起来，用户只会看到"没有这个场景包"，
    而那通常是 rules.yaml 少了一行——和"配置静默不生效"是同一类事故。
    这里只读目录，不改变 `load_pack` 的严格性：真要跑，仍然只有可用的包能跑。

    没有 `packs_dir` 参数是刻意的：装载走的是模块级 `PACKS_DIR`，发现若另开一个目录参数，
    就会出现"在 A 目录列出的包、去 B 目录装载"的第二个真源（测试通过 monkeypatch
    `PACKS_DIR` 换目录，两条路径自然同源）。
    """
    root = PACKS_DIR
    if not root.exists():
        return [], []
    packs: list[ScenarioPack] = []
    broken: list[dict[str, str]] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if not PACK_NAME_RE.match(directory.name):
            broken.append({"dir": directory.name, "reason": "目录名不是合法的场景包名"})
            continue
        try:
            packs.append(load_pack(directory.name))
        except Exception as error:  # noqa: BLE001 - 发现过程要把每一份坏包的原因原样带回去
            broken.append({"dir": directory.name, "reason": f"{type(error).__name__}: {str(error)[:200]}"})
    return packs, broken


def pack_names() -> set[str]:
    """可运行的场景包名集合（API 侧的白名单就取这里，不在别处再维护一份）。"""
    packs, _broken = list_packs()
    return {pack.name for pack in packs}


def available_columns_from(
    pack: ScenarioPack, table_columns: Iterable[Sequence[Any]]
) -> set[str]:
    """给定"若干张表各自的列名"，算出这批数据能提供的规范列全集。

    单独抽出来是为了让**边界预检**（HTTP 派发前）与**规划预检**（planner 内）用同一个判据：
    前者手上只有 `datasets.columns` 里存的字符串数组，后者有真 Bundle，两处若各写一遍
    别名归一，就会出现在界面上放行、跑起来才缺列的分叉结果。
    """
    columns: set[str] = set()
    for names in table_columns:
        columns |= {pack.canonical(str(column)) for column in (names or [])}
    return columns


def missing_required(pack: ScenarioPack, available: set[str]) -> list[str]:
    """必需列里这批数据给不出来的那些，保持包内声明顺序。

    判"缺不缺"只有这一处：派发前的边界预检（Web 层）与派发时的规划预检（planner）
    必须给出**同一个清单**，否则会出现界面上放行、跑起来才拒的分叉。
    """
    return [column for column in pack.required_columns if column not in available]


def available_columns(pack: ScenarioPack, bundle: Any) -> set[str]:
    """这个 Bundle 能提供的规范列全集（跨所有表，按别名归一）。

    场景包的必需列校验原先只看主表——多源输入下 `域` 在资产表、`auth_result` 在防火墙表，
    只看主表会把合法的多源包判成"缺列"。
    """
    return available_columns_from(
        pack,
        (getattr(table, "columns", []) or [] for table in (getattr(bundle, "tables", []) or [])),
    )


def pack_plan_tasks(pack: ScenarioPack, bundle: Any = None) -> list[dict[str, Any]]:
    """按规则目录确定性生成检测任务（不调 LLM——LLM 不判危险）。

    传 `bundle` 时，声明了 `requires` 的规则会解析出实际表并带上 `dataset_refs` /
    `join_keys`，于是跨表规则走的是与通用跨表任务同一条通道：派发前基数预检、表级授权、
    独立重放。解析不出来的规则不生成任务（宁缺毋滥：指向错表的规则比没有规则更危险）。
    """
    tasks: list[dict[str, Any]] = []
    index = 0
    for rule in pack.rules:
        resolved = rule.resolve(pack, bundle) if bundle is not None else {}
        if rule.requires and bundle is not None and not resolved:
            continue  # 角色找不到承载表 ⇒ 这条规则在当前数据上不可执行
        index += 1
        refs = []
        if resolved:
            seen: list[str] = []
            for table in resolved.values():
                if str(table.id) not in seen:
                    seen.append(str(table.id))
            refs = seen if len(seen) >= 2 else []
        tasks.append(
            {
                "task_id": index,
                "description": f"检测规则{rule.id}：{rule.name}",
                "required_columns": sorted(
                    {column for columns in rule.requires.values() for column in columns}
                )
                or list(pack.required_columns),
                "code_hint": f"rule_pack:{rule.id}",
                "chart_type": "none",
                "depends_on": [],
                "upstream_refs": [],
                "rule_params": {"id": rule.id, "roles": sorted(str(role) for role in resolved)},
                "dataset_refs": refs,
                # 单角色规则也要有确定主表：不写的话 DATA_PATH 会落到"按文件名排第一张"，
                # 三源场景下那是资产台账，T1 会在错的数据上跑出空结果
                "primary_ref": str(next(iter(resolved.values())).id) if resolved else "",
                "join_keys": list(rule.join_keys),
            }
        )
    return tasks


def verify_findings(
    pack: ScenarioPack,
    task: dict[str, Any],
    result: dict[str, Any],
    data_path: str,
    bundle: Any = None,
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

    expected = _run_verify_code(rule, data_path, pack=pack, bundle=bundle)
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


def _run_verify_code(
    rule: PackRule,
    data_path: str,
    pack: ScenarioPack | None = None,
    bundle: Any = None,
) -> list[Any] | None:
    """子进程执行独立校验器，返回期望 findings 列表；失败返回 None（skipped 语义）。

    跨表规则的两个实现读的是**同一组角色路径**：校验器若只读主表，它算的就不是同一条判断，
    "一致"也就没有意义。
    """
    import tempfile

    from agentflow.core.executor import LocalBackend

    if not rule.verify_code.strip():
        return None
    env = {"DATA_PATH": str(data_path)}
    if pack is not None and bundle is not None:
        env.update(role_env(pack, bundle, rule))
    work_dir = Path(tempfile.mkdtemp(prefix="pack_verify_"))
    backend = LocalBackend()
    outcome = backend.execute(
        rule.verify_code,
        work_dir=work_dir,
        env=env,
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
