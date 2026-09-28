"""确定性评分器（评估方案 §3.1 / §7 的执行层）。

立场：**grader 自己也要守 producer ≠ verifier**——本模块只读四份产物证据
（evaluation.json / report.md / transcript.jsonl / plan.json），不调 LLM、
不读 LLM 的自我陈述，golden 数字由 `evals/suite.yaml` 冻结（人写、可独立重算）。

断言一律是可判定谓词（相等 / 集合相等 / 上下界 / 存在性），不用"相似度"。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 报告里允许出现的"非结论数字"：时间戳、日期、行号等运行元数据
_META_NUMBER_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}:\d{2}:\d{2}\b")
# 点分四段（IPv4 等）不是"数字"：不先挖掉会把 198.51.100.23 拆成三个假未追溯数
_DOTTED_QUAD_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

TIER_GATE = "gate"          # 破了就红
TIER_KNOWN_GAP = "known_gap"  # 已知该模式做不到：期望红，红不算回归；绿则报 XPASS 提醒重新分类


@dataclass
class Check:
    """单条断言的执行结果。"""

    kind: str
    passed: bool
    detail: str
    tier: str = TIER_GATE


@dataclass
class CaseResult:
    case_id: str
    mode: str
    tier: str
    status: str = "-"
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    error: str = ""

    @property
    def failed_gates(self) -> list[Check]:
        return [c for c in self.checks if c.tier == TIER_GATE and not c.passed]

    @property
    def expected_failures(self) -> list[Check]:
        return [c for c in self.checks if c.tier == TIER_KNOWN_GAP and not c.passed]

    @property
    def unexpected_passes(self) -> list[Check]:
        return [c for c in self.checks if c.tier == TIER_KNOWN_GAP and c.passed]

    @property
    def verdict(self) -> str:
        if self.error:
            return "error"
        if self.failed_gates:
            return "fail"
        if self.unexpected_passes:
            return "xpass"  # 已知缺口被填上了：需要重新分类
        return "pass"


# ---------------------------------------------------------------- 证据装载


def load_evidence(outputs_dir: str | Path) -> dict[str, Any]:
    root = Path(outputs_dir)
    evaluation: dict[str, Any] = {}
    evaluation_path = root / "evaluation.json"
    if evaluation_path.exists():
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    report = ""
    report_path = root / "report.md"
    if report_path.exists():
        report = report_path.read_text(encoding="utf-8")
    transcript: list[dict[str, Any]] = []
    transcript_path = root / "transcript.jsonl"
    if transcript_path.exists():
        for line in transcript_path.read_text(encoding="utf-8").splitlines():
            try:
                transcript.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    plan = {}
    plan_path = root / "plan.json"
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            plan = {}
    return {
        "outputs_dir": str(root),
        "evaluation": evaluation,
        "report": report,
        "transcript": transcript,
        "plan": plan,
    }


def _results(evidence: dict[str, Any]) -> dict[str, Any]:
    return (evidence.get("evaluation") or {}).get("results") or {}


def _iter_summaries(evidence: dict[str, Any]):
    for task_id, result in sorted(_results(evidence).items(), key=lambda kv: int(kv[0])):
        yield str(task_id), (result or {}).get("summary") or {}


def _aggregates(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {tid: (summary.get("aggregate") or {}) for tid, summary in _iter_summaries(evidence)}


def _findings(evidence: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        tid: (summary.get("findings") or []) for tid, summary in _iter_summaries(evidence)
    }


def _close(actual: Any, expected: Any, tolerance: float) -> bool:
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError):
        return str(actual) == str(expected)
    return abs(a - b) <= max(tolerance, abs(b) * 1e-6)


def _flatten_numbers(payload: Any) -> set[float]:
    numbers: set[float] = set()
    if isinstance(payload, dict):
        for value in payload.values():
            numbers |= _flatten_numbers(value)
    elif isinstance(payload, list):
        for value in payload:
            numbers |= _flatten_numbers(value)
    elif isinstance(payload, bool):
        pass
    elif isinstance(payload, (int, float)):
        numbers.add(float(payload))
    return numbers


# ---------------------------------------------------------------- 谓词


def _p_status(evidence, params, mode):
    actual = (evidence["evaluation"] or {}).get("status", "-")
    allowed = params if isinstance(params, list) else [params]
    return actual in allowed, f"status={actual} 期望∈{allowed}"


def _p_degraded_reason(evidence, params, mode):
    actual = (evidence["evaluation"] or {}).get("degraded_reason")
    return actual == params, f"degraded_reason={actual} 期望={params}"


def _p_aggregate(evidence, params, mode):
    """params: {task: "1"|None, expect: {key: value}, tolerance: 0.01}"""
    tolerance = float(params.get("tolerance", 0.01))
    expect = params.get("expect") or {}
    aggs = _aggregates(evidence)
    task = str(params["task"]) if params.get("task") is not None else None
    pool = {task: aggs.get(task, {})} if task else aggs
    problems = []
    for key, wanted in expect.items():
        hit = next(
            (
                (tid, got)
                for tid, values in pool.items()
                for got in [values.get(key)]
                if got is not None and _close(got, wanted, tolerance)
            ),
            None,
        )
        if hit is None:
            got_any = {tid: values.get(key) for tid, values in pool.items()}
            problems.append(f"{key}≠{wanted}（实得 {got_any}）")
    return not problems, "aggregate " + ("; ".join(problems) if problems else f"命中 {list(expect)}")


def _p_rows(evidence, params, mode):
    """params: {task, expect} 或 {expect_any: n}"""
    rows = {tid: summary.get("rows") for tid, summary in _iter_summaries(evidence)}
    if "task" in params:
        actual = rows.get(str(params["task"]))
        ok = _close(actual, params["expect"], 0)
    else:
        actual = [v for v in rows.values() if v is not None]
        ok = params["expect_any"] in [int(v) for v in actual if v is not None]
    return ok, f"rows={rows} 期望={params}"


def _p_report_contains(evidence, params, mode):
    report = evidence["report"]
    wanted = params if isinstance(params, list) else [params]
    missing = [text for text in wanted if text not in report]
    return not missing, ("缺少 " + "；".join(missing)) if missing else f"{len(wanted)} 处命中"


def _p_report_excludes(evidence, params, mode):
    report = evidence["report"]
    wanted = params if isinstance(params, list) else [params]
    found = [text for text in wanted if text in report]
    return not found, ("命中禁用词 " + "；".join(found)) if found else "无禁用词"


def _p_verifier(evidence, params, mode):
    """每个跑了独立校验的任务都必须一致（producer≠verifier 的收口）。

    params = "pass_all"：有校验且全部一致才绿；params = "skipped_ok"：允许零覆盖
    （用于降级类用例，此时"没跑到校验"是预期路径）。
    """
    checked = []
    for task_id, result in _results(evidence).items():
        verdict = (result or {}).get("verdict") or {}
        if verdict.get("verification") != "ok":
            continue
        matched = [c for c in (verdict.get("checks") or []) if "match_check" in c]
        checked.append((task_id, matched))
    if not checked:
        if params == "skipped_ok":
            return True, "无任务被独立校验（该用例允许）"
        return False, "校验覆盖率 0：没有任何任务被独立复算"
    bad = [
        f"task {tid}:{matched}"
        for tid, matched in checked
        if not matched or not all(c.endswith(":PASS") for c in matched)
    ]
    ok = len(checked) - len(bad)
    return not bad, f"校验一致 {ok}/{len(checked)}" + (f" 破口 {bad}" if bad else "")


def _p_findings(evidence, params, mode):
    """场景包命中数：params = {R1: 1, R2: 1, ...}，按 aggregate 的"规则Rx命中数"核对。"""
    merged: dict[str, Any] = {}
    for values in _aggregates(evidence).values():
        merged.update(values)
    problems = []
    for rule, wanted in (params or {}).items():
        key = f"规则{rule}命中数"
        actual = merged.get(key)
        if actual is None or not _close(actual, wanted, 0):
            problems.append(f"{key}={actual} 期望={wanted}")
    return not problems, ("命中数核对 " + ("；".join(problems) if problems else "全部一致"))


def _p_subjects(evidence, params, mode):
    """证据主体集合相等（漏报/误报都算破）。params = {rule, expect: [subject...]}"""
    subjects = set()
    for items in _findings(evidence).values():
        for item in items:
            if not isinstance(item, dict):
                continue
            rule_hit = str(item.get("rule", "")) or str(item.get("rule_id", ""))
            if params.get("rule") and rule_hit and rule_hit != str(params["rule"]):
                continue
            subjects.add(str(item.get("subject")))
    expect = {str(s) for s in params.get("expect", [])}
    missing, extra = expect - subjects, subjects - expect
    detail = []
    if missing:
        detail.append(f"漏 {sorted(missing)}")
    if extra:
        detail.append(f"多 {sorted(extra)}")
    return not detail, "subjects " + ("；".join(detail) if detail else f"{len(subjects)} 条一致")


def _p_llm_calls_max(evidence, params, mode):
    actual = int((evidence["evaluation"] or {}).get("llm_calls") or 0)
    return actual <= int(params), f"llm_calls={actual} 上界={params}"


def _p_duration_max(evidence, params, mode):
    actual = float((evidence["evaluation"] or {}).get("duration_seconds") or 0)
    return actual <= float(params), f"duration={actual}s 上界={params}s"


def _p_chart(evidence, params, mode):
    actual = (evidence["evaluation"] or {}).get("chart_success")
    if params == "any":
        return True, f"chart_success={actual}"
    if params == "none":
        return not actual, f"chart_success={actual} 期望无图"
    return bool(actual), f"chart_success={actual} 期望有图"


def _p_critic(evidence, params, mode):
    actual = (evidence["evaluation"] or {}).get("critic_pass")
    if params == "any":
        return True, f"critic_pass={actual}"
    return bool(actual) is bool(params == "pass"), f"critic_pass={actual} 期望={params}"


def _p_replan(evidence, params, mode):
    actual = int((evidence["evaluation"] or {}).get("replan_used") or 0)
    if isinstance(params, int):
        return actual == params, f"replan_used={actual} 期望={params}"
    return actual <= int(params["max"]), f"replan_used={actual} 上界={params['max']}"


def _p_clarify(evidence, params, mode):
    actual = bool((evidence["evaluation"] or {}).get("clarify"))
    return actual is bool(params), f"clarify={actual} 期望={params}"


def _p_transcript_has(evidence, params, mode):
    """过程证据里必须出现过某类事件/字段（在整条记录文本里找，不只看事件名）。"""
    wanted = params if isinstance(params, list) else [params]
    blob = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in evidence["transcript"])
    missing = [name for name in wanted if name not in blob]
    return not missing, ("transcript 缺 " + "；".join(missing)) if missing else f"{len(wanted)} 项在场"


def _p_depends_on(evidence, params, mode):
    """数据流结构断言（依赖边即授权边的可验证形式）。params = {task: 2, expect: [1]}"""
    tasks = {int(t.get("task_id", -1)): t for t in (evidence.get("plan") or {}).get("tasks", [])}
    task = int(params["task"])
    actual = sorted(int(d) for d in (tasks.get(task, {}).get("depends_on") or []))
    expect = sorted(int(d) for d in params.get("expect", []))
    return actual == expect, f"task{task}.depends_on={actual} 期望={expect}"


def _p_artifacts_clean(evidence, params, mode):
    """注入/外泄防线：全部产物文本里不得出现敏感串。"""
    banned = params if isinstance(params, list) else [params]
    blob = evidence["report"] + "\n".join(json.dumps(e, ensure_ascii=False) for e in evidence["transcript"])
    blob += json.dumps(evidence["evaluation"], ensure_ascii=False)
    found = [token for token in banned if token in blob]
    return not found, ("产物出现敏感串 " + "；".join(found)) if found else "无敏感串"


def _p_numbers_traceable_min(evidence, params, mode):
    ratio, unexplained = numbers_traceable(evidence)
    return ratio >= float(params), f"可追溯率={ratio:.2%} 下界={float(params):.0%} 未追到={unexplained}"


def _p_tasks_min(evidence, params, mode):
    actual = len(_results(evidence))
    return actual >= int(params), f"任务数={actual} 下界={params}"


PREDICATES: dict[str, Callable[[Any, Any, str], tuple[bool, str]]] = {
    "status": _p_status,
    "degraded_reason": _p_degraded_reason,
    "aggregate": _p_aggregate,
    "rows": _p_rows,
    "tasks_min": _p_tasks_min,
    "depends_on": _p_depends_on,
    "report_contains": _p_report_contains,
    "report_excludes": _p_report_excludes,
    "verifier": _p_verifier,
    "findings": _p_findings,
    "subjects": _p_subjects,
    "llm_calls_max": _p_llm_calls_max,
    "duration_max": _p_duration_max,
    "chart": _p_chart,
    "critic": _p_critic,
    "replan": _p_replan,
    "clarify": _p_clarify,
    "transcript_has": _p_transcript_has,
    "artifacts_clean": _p_artifacts_clean,
    "numbers_traceable_min": _p_numbers_traceable_min,
}


def numbers_traceable(evidence: dict[str, Any]) -> tuple[float, list[str]]:
    """数字可追溯率：报告里的每个数字都要能在证据里找到出处。

    证据池 = evaluation.json 的 results（含 aggregate 与 rows）+ 数据集行数 + plan.json。
    这是"报告数字不是编的"唯一可判定的代理指标，也是本 harness 的核心指标。
    点分四段（IP 等）与运行时间戳先挖掉，否则指标会被非数字文本污染成假红。
    """
    report = evidence["report"]
    if not report:
        return 0.0, ["报告为空"]
    evaluation = evidence.get("evaluation") or {}
    pool = _flatten_numbers(evaluation.get("results") or {})
    if evaluation.get("dataset_rows") is not None:
        pool.add(float(evaluation["dataset_rows"]))
    pool |= _flatten_numbers(evidence.get("plan") or {})
    cleaned = _DOTTED_QUAD_RE.sub(" ", report)
    for token in _META_NUMBER_RE.findall(cleaned):
        cleaned = cleaned.replace(token, " ")
    tokens = [t for t in _NUMBER_RE.findall(cleaned) if _is_claim(t)]
    unexplained = [t for t in tokens if not any(_close(float(t), known, 0.01) for known in pool)]
    total = max(1, len(tokens))
    return (total - len(unexplained)) / total, unexplained[:8]


def _is_claim(token: str) -> bool:
    """过滤掉"不构成结论"的小整数：序号、任务号、严重度计数之类。"""
    value = float(token)
    return not (value == int(value) and abs(value) < 100)


def _run_block(evidence: dict[str, Any], spec: dict[str, Any], block: str) -> list[Check]:
    """跑一个断言块（gate 或 gap）。谓词名拼错一律按 gate 判红——拼错不许静默放行。"""
    tier = TIER_GATE if block == "gate" else TIER_KNOWN_GAP
    checks: list[Check] = []
    for kind, params in (spec.get(block) or {}).items():
        predicate = PREDICATES.get(kind)
        if predicate is None:
            checks.append(Check(kind=kind, passed=False, detail=f"未知谓词 {kind}", tier=TIER_GATE))
            continue
        try:
            passed, detail = predicate(evidence, params, block)
        except Exception as exc:  # noqa: BLE001 - 评分器异常必须冒红而不是当作通过
            passed, detail = False, f"评分器异常：{type(exc).__name__}: {exc}"
        checks.append(Check(kind=kind, passed=bool(passed), detail=detail, tier=tier))
    return checks


ALLOWED_SPEC_KEYS = {"gate", "gap"}


def lint_suite(cases: list[dict[str, Any]]) -> list[str]:
    """冻结集自检：写错块名或谓词名一律报出。

    静默忽略的断言比红着的断言危险得多——它会让一套题看起来在守门，实际上什么都没守。
    """
    problems: list[str] = []
    for case in cases:
        case_id = str(case.get("id", "?"))
        if not (case.get("mock") or case.get("real")):
            problems.append(f"{case_id}: 没有任何模式的断言块")
        for mode in ("mock", "real"):
            spec = case.get(mode)
            if not spec:
                continue
            for key in spec:
                if key not in ALLOWED_SPEC_KEYS:
                    problems.append(f"{case_id}.{mode}: 未知块名 {key}（会被静默忽略）")
            for block in ("gate", "gap"):
                for kind in (spec.get(block) or {}):
                    if kind not in PREDICATES:
                        problems.append(f"{case_id}.{mode}.{block}: 未知谓词 {kind}")
    return problems


def evaluate_case(case: dict[str, Any], evidence: dict[str, Any], mode: str) -> CaseResult:
    """跑一题：`gate` 块破了才红；`gap` 块是已知能力边界——红不阻塞，绿则 XPASS。

    XPASS 单列一种结论是刻意的：已知缺口被填上时必须逼人来重新分类，
    否则评测集会一直把"已经能做"的能力记成"做不到"。
    """
    spec = case.get(mode) or {}
    result = CaseResult(case_id=str(case["id"]), mode=mode, tier="gate+gap")
    result.status = str((evidence.get("evaluation") or {}).get("status", "-"))
    result.checks = _run_block(evidence, spec, "gate") + _run_block(evidence, spec, "gap")
    ratio, unexplained = numbers_traceable(evidence)
    result.metrics = {
        "numbers_traceable_ratio": round(ratio, 4),
        "llm_calls": float((evidence.get("evaluation") or {}).get("llm_calls") or 0),
        "duration_seconds": float((evidence.get("evaluation") or {}).get("duration_seconds") or 0),
        "tasks": float(len(_results(evidence))),
        "unexplained_numbers": float(len(unexplained)),
    }
    return result


# ---------------------------------------------------------------- 归因指纹


def sha28(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return "missing"


def fingerprint(
    agents: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    pack: Any = None,
    pack_name: str | None = None,
    extra_files: list[Path] | None = None,
) -> dict[str, str]:
    """run 的可归因指纹：prompt / config / pack / harness 四者的 hash 一起记。

    分数变化只有配对到这里的某个 diff，才算"可归因"（评估方案 §8 归因规则）。
    """
    out: dict[str, str] = {}
    for name, agent in sorted((agents or {}).items()):
        prompt = getattr(agent, "system_prompt", "") or ""
        out[f"prompt:{name}"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    if config is not None:
        blob = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
        out["config"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    if pack_name:
        root = Path(pack.path) if pack is not None and getattr(pack, "path", None) else None
        if root and root.exists():
            for f in sorted(root.glob("*")):
                if f.is_file():
                    out[f"pack:{f.name}"] = sha28(f)
    for f in extra_files or []:
        if Path(f).exists():
            out[f"harness:{Path(f).name}"] = sha28(Path(f))
    return out
