"""冻结评测集 runner + 回归门禁（评估方案 §6/§7/§8 的可执行版本）。

    python scripts/run_eval.py                      # 跑 mock 全量并与基线比对，破了 exit 1
    python scripts/run_eval.py --only E01,E04       # 子集
    python scripts/run_eval.py --check-golden       # 只核对 golden 与数据是否漂移
    python scripts/run_eval.py --update-baseline    # 认可当前结果为基线并重写
    python scripts/run_eval.py --mode real          # nightly 抽样（需 .env，先做凭据预检）

三条纪律写死在这里：
1. **golden 独立重算**——suite.yaml 的字面量与数据文件对不上时报"漂移"而非"系统失败"，
   两类红的原因不同，混在一起会让人去看错的地方；
2. **grader 不读 LLM 自述**——只看 evaluation.json / report.md / transcript.jsonl；
3. **每个 run 落指纹**——prompt / config / pack / harness 的 hash 写进 run_config.json，
   分数变化必须能配对到某个 diff，否则记为"不可归因波动"。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # noqa: E402

from agentflow.core.config import load_config  # noqa: E402
from agentflow.core.grading import (  # noqa: E402
    TIER_GATE,
    TIER_KNOWN_GAP,
    Check,
    evaluate_case,
    fingerprint,
    lint_suite,
    load_evidence,
)
from agentflow.pipeline import run_analysis  # noqa: E402

SUITE_PATH = PROJECT_ROOT / "evals" / "suite.yaml"
BASELINE_PATH = PROJECT_ROOT / "evals" / "baseline.json"
HARNESS_FILES = [SUITE_PATH, PROJECT_ROOT / "src" / "agentflow" / "core" / "grading.py", Path(__file__)]

# 指标回归容差：低于这个幅度的波动视为噪声，不阻塞
METRIC_TOLERANCE = {
    "numbers_traceable_mean": 0.02,   # 绝对差
    "verifier_checked": 0,            # 校验覆盖数只许持平或涨
    "avg_llm_calls": 0.25,            # 相对涨幅 25%
    "avg_duration": 0.5,              # 相对涨幅 50%
}


# ---------------------------------------------------------------- golden 重算


def derive_goldens(data_dir: Path) -> dict[str, Any]:
    """按 suite.yaml 里标注的 pandas 表达式独立重算 golden。

    这段代码**故意与被测系统无关**：只用 pandas 直接读 CSV。
    它和 suite.yaml 字面量不一致 = 数据或评测集漂移，与模型质量无关。
    """
    import pandas as pd

    df = pd.read_csv(data_dir / "retail_sales_with_profit.csv", parse_dates=["订单日期"])
    plain = pd.read_csv(data_dir / "retail_sales.csv")
    last = df["订单日期"].max()
    win = df[df["订单日期"] > last - pd.Timedelta(days=7)]
    daily = win.groupby("订单日期")["销售额"].sum()
    prev = df[(df["订单日期"] > last - pd.Timedelta(days=14)) & (df["订单日期"] <= last - pd.Timedelta(days=7))]
    huadong = df[df["地区"] == "华东"]
    by_day = df.groupby("订单日期")[["销售额", "利润"]].sum()
    peak = by_day["销售额"].idxmax()
    half = df[(df["订单日期"] >= "2024-01-01") & (df["订单日期"] <= "2024-06-30")]
    rate = df["利润"] / df["销售额"]
    category_sales = df.groupby("产品类别")["销售额"].sum()
    region_profit = df.groupby("地区")["利润"].sum()
    huadong_qty = huadong.groupby("产品类别")["销量"].sum()
    return {
        "total_sales_profit": round(float(df["销售额"].sum()), 2),
        "total_sales_plain": round(float(plain["销售额"].sum()), 2),
        "total_profit": round(float(df["利润"].sum()), 2),
        "orders": int(len(df)),
        "last7_sum": round(float(win["销售额"].sum()), 2),
        "last7_max_day": str(daily.idxmax().date()),
        "last7_max": round(float(daily.max()), 2),
        "last7_min_day": str(daily.idxmin().date()),
        "last7_min": round(float(daily.min()), 2),
        "prev7_sum": round(float(prev["销售额"].sum()), 2),
        "wow_pct": round(float((win["销售额"].sum() - prev["销售额"].sum()) / prev["销售额"].sum() * 100), 2),
        "top_category": str(category_sales.idxmax()),
        "top_category_sales": round(float(category_sales.max()), 2),
        "top_region_profit": str(region_profit.idxmax()),
        "top_region_profit_value": round(float(region_profit.max()), 2),
        "huadong_rows": int(len(huadong)),
        "huadong_sales": round(float(huadong["销售额"].sum()), 2),
        "huadong_top_category": str(huadong_qty.idxmax()),
        "huadong_top_quantity": int(huadong_qty.max()),
        "negative_margin_rows": int((rate < 0).sum()),
        "first_half_rows": int(len(half)),
        "first_half_sales": round(float(half["销售额"].sum()), 2),
        "peak_day": str(peak.date()),
        "peak_day_sales": round(float(by_day.loc[peak, "销售额"]), 2),
        "peak_day_profit": round(float(by_day.loc[peak, "利润"]), 2),
    }


def check_golden(suite: dict[str, Any]) -> list[str]:
    expected = suite.get("golden") or {}
    actual = derive_goldens(PROJECT_ROOT / "demo" / "data")
    problems = []
    for key, want in expected.items():
        got = actual.get(key)
        if got is None:
            problems.append(f"golden.{key} 无法重算（derive 表缺项）")
        elif isinstance(want, (int, float)) and isinstance(got, (int, float)):
            if abs(float(want) - float(got)) > 0.011:
                problems.append(f"golden.{key} 漂移：suite={want} 重算={got}")
        elif str(want) != str(got):
            problems.append(f"golden.{key} 漂移：suite={want} 重算={got}")
    return problems


# ---------------------------------------------------------------- 跑题


def _data_path(suite: dict[str, Any], ref: str) -> str:
    return str(PROJECT_ROOT / suite["data"][ref])


def run_case(case: dict[str, Any], suite: dict[str, Any], mode: str, root: Path) -> dict[str, Any]:
    """单题执行：支持多轮（session）与重复（repeat，用于一致性断言）。"""
    case_id = str(case["id"])
    outputs_root = root / case_id
    if outputs_root.exists():
        shutil.rmtree(outputs_root)
    outputs_root.mkdir(parents=True, exist_ok=True)
    session_id = f"eval_{case_id}_{uuid.uuid4().hex[:6]}" if case.get("session") else None

    turns = case.get("turns") or [{"question": case["question"], "data": case.get("data", "profit")}]
    runs: list[dict[str, Any]] = []
    repeats = int(case.get("repeat", 1))
    started = time.monotonic()
    for _ in range(max(1, repeats)):
        for turn in turns:
            result = run_analysis(
                question=str(turn["question"]),
                sources=_data_path(suite, str(turn.get("data", case.get("data", "profit")))),
                mode=mode,
                outputs_root=outputs_root,
                session_id=session_id,
                pack=case.get("pack"),
            )
            runs.append(result)
    elapsed = round(time.monotonic() - started, 2)
    if not runs:
        return {"case_id": case_id, "error": "没有产生任何 run"}

    graded = runs[-1]  # 断言打在最后一轮（多轮题考的是续轮行为）
    evidence = load_evidence(graded["outputs_dir"])
    result = evaluate_case(case, evidence, mode)

    if case.get("require_consistent") and len(runs) > 1:
        signatures = {
            json.dumps(_aggregates_of(load_evidence(run["outputs_dir"])), sort_keys=True, ensure_ascii=False)
            for run in runs
        }
        consistent = len(signatures) == 1
        result.checks.append(
            Check(
                kind="consistent_repeat",
                passed=consistent,
                detail="两次运行关键数字一致" if consistent else f"结论不稳定：{sorted(signatures)}",
                tier=TIER_GATE,
            )
        )

    _write_provenance(graded["outputs_dir"], case, mode, result, runs)
    return {
        "case_id": result.case_id,
        "verdict": result.verdict,
        "status": result.status,
        "seconds": elapsed,
        "metrics": result.metrics,
        "checks": [
            {"kind": c.kind, "tier": c.tier, "passed": c.passed, "detail": c.detail} for c in result.checks
        ],
        "runs": [{"run_id": run.get("run_id"), "status": run.get("status")} for run in runs],
    }


def _aggregates_of(evidence: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for task_id, result in ((evidence.get("evaluation") or {}).get("results") or {}).items():
        summary = (result or {}).get("summary") or {}
        out[task_id] = summary.get("aggregate")
    return out


def agent_prompt_map() -> dict[str, Any]:
    """七个角色的类对象——fingerprint 只取类属性 system_prompt，不实例化、不走 LLM。"""
    from agentflow import agents as agent_module

    return {
        name.lower(): getattr(agent_module, f"{name}Agent")
        for name in ("Explorer", "Planner", "Executor", "Inspector", "Visualizer", "Reporter", "Critic")
        if hasattr(agent_module, f"{name}Agent")
    }


def _write_provenance(outputs_dir: str, case: dict[str, Any], mode: str, result, runs) -> None:
    """§8 归因：把 prompt/config/pack/harness 指纹落到 run 目录，分数变化才有对账对象。"""
    from agentflow.core.config import load_config
    from agentflow.core.pack import load_pack

    pack_obj = load_pack(case["pack"]) if case.get("pack") else None
    payload = {
        "case_id": case["id"],
        "suite_version": case.get("suite_version"),
        "mode": mode,
        "runs": [run.get("run_id") for run in runs],
        "verdict": result.verdict,
        "fingerprints": fingerprint(
            agents=agent_prompt_map(),
            config=load_config(None),
            pack=pack_obj,
            pack_name=case.get("pack"),
            extra_files=HARNESS_FILES,
        ),
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    Path(outputs_dir, "run_config.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------- 汇总与门禁


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [r for r in results if "checks" in r]
    gates = [c for r in graded for c in r["checks"] if c["tier"] == TIER_GATE]
    gaps = [c for r in graded for c in r["checks"] if c["tier"] == TIER_KNOWN_GAP]
    metrics = [r.get("metrics") or {} for r in graded]

    def mean(key: str) -> float:
        values = [m.get(key) for m in metrics if isinstance(m.get(key), (int, float))]
        return round(sum(values) / len(values), 4) if values else 0.0

    return {
        "cases": len(graded),
        "pass": sum(1 for r in graded if r["verdict"] == "pass"),
        "fail": sum(1 for r in graded if r["verdict"] == "fail"),
        "xpass": sum(1 for r in graded if r["verdict"] == "xpass"),
        "error": sum(1 for r in graded if r["verdict"] == "error"),
        "gate_checks": len(gates),
        "gate_failed": sum(1 for c in gates if not c["passed"]),
        "gap_checks": len(gaps),
        "gap_passed": sum(1 for c in gaps if c["passed"]),
        "numbers_traceable_mean": mean("numbers_traceable_ratio"),
        "avg_llm_calls": mean("llm_calls"),
        "avg_duration": mean("duration_seconds"),
        "verifier_checked": sum(
            1
            for r in graded
            for c in r["checks"]
            if c["kind"] == "verifier" and c["passed"]
        ),
    }


def compare_baseline(current: dict[str, Any], baseline: dict[str, Any], check_aggregate: bool = True) -> list[str]:
    """回归判定：逐题 gate 结论 + 聚合指标双路。

    题集不一致时调用方应传 check_aggregate=False——均值在不同题数之间没有可比性，
    只有逐题 gate 结论可比。
    """
    problems: list[str] = []
    previous = {r["case_id"]: r for r in baseline.get("results", [])}
    for case in current["results"]:
        before = previous.get(case["case_id"])
        if before and before.get("verdict") in ("pass", "xpass") and case["verdict"] == "fail":
            broken = [c["kind"] for c in case["checks"] if c["tier"] == TIER_GATE and not c["passed"]]
            problems.append(f"{case['case_id']} 回归：{before['verdict']} → fail，破口 {broken}")
        if before and before.get("verdict") == "pass" and case["verdict"] == "xpass":
            problems.append(f"{case['case_id']} XPASS：已知缺口被填上，请重新分类 gap→gate")
    if not check_aggregate:
        return problems
    base_agg = baseline.get("aggregate") or {}
    for key, tolerance in METRIC_TOLERANCE.items():
        now, was = current["aggregate"].get(key), base_agg.get(key)
        if now is None or was is None:
            continue
        if key in ("numbers_traceable_mean",):
            if was - now > tolerance:
                problems.append(f"指标回归：{key} {was} → {now}（容差 {tolerance}）")
        elif key == "verifier_checked":
            if now < was - tolerance:
                problems.append(f"指标回归：{key} {was} → {now}（校验覆盖率下降）")
        elif was and (was - now) / was > tolerance:
            problems.append(f"指标回归：{key} {was} → {now}（容差 {tolerance:.0%}）")
    return problems


def print_report(results: list[dict[str, Any]], summary: dict[str, Any], problems: list[str]) -> None:
    print("=" * 78)
    for row in results:
        if "checks" not in row:
            print(f"  {row['case_id']:5} ERROR  {row.get('error')}")
            continue
        marks = {"pass": "✔", "fail": "✘", "xpass": "▲", "error": "!"}
        mark = marks.get(row["verdict"], "?")
        trace = row.get("metrics", {}).get("numbers_traceable_ratio")
        print(
            f"  {mark} {row['case_id']:5} {row['verdict']:6} status={row['status']:9} "
            f"追溯率={trace if trace is not None else '-'} {row['seconds']}s"
        )
        for check in row["checks"]:
            if not check["passed"] or check["tier"] == TIER_KNOWN_GAP:
                flag = "GATE" if check["tier"] == TIER_GATE else "gap "
                print(f"        [{flag}] {check['kind']:24} {check['detail'][:150]}")
    print("-" * 78)
    print(
        f"题数 {summary['cases']} ｜ pass {summary['pass']} ｜ fail {summary['fail']} ｜ "
        f"xpass {summary['xpass']} ｜ error {summary['error']}"
    )
    print(
        f"gate 断言 {summary['gate_checks']}（破 {summary['gate_failed']}）｜ "
        f"gap 断言 {summary['gap_checks']}（已达 {summary['gap_passed']}）"
    )
    print(
        f"数字可追溯率均值 {summary['numbers_traceable_mean']:.2%} ｜ 平均 LLM {summary['avg_llm_calls']} ｜ "
        f"平均耗时 {summary['avg_duration']}s ｜ 校验一致题数 {summary['verifier_checked']}"
    )
    if problems:
        print("-" * 78)
        print("门禁判红：")
        for line in problems:
            print(f"  ✘ {line}")
    print("=" * 78)


def preflight_credentials(mode: str) -> str:
    """real 模式跑批前必做凭据预检（历史上 DeepSeek 两次 402 静默断供）。"""
    if mode != "real":
        return ""
    import os

    from agentflow.core.config import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        return "real 模式缺少 OPENAI_API_KEY（.env）"
    try:
        from agentflow.core.llm import OpenAILLM

        OpenAILLM(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            model=os.getenv("LLM_MODEL") or None,
        ).complete(system="ping", messages=[{"role": "user", "content": "回复 ok"}], max_tokens=8)
    except Exception as exc:  # noqa: BLE001 - 预检失败即中止，不烧一整批
        return f"凭据预检失败，未跑批：{type(exc).__name__}: {str(exc)[:200]}"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结评测集 runner + 回归门禁")
    parser.add_argument("--mode", default="mock", choices=["mock", "real"])
    parser.add_argument("--only", default=None, help="逗号分隔用例号，如 E01,E04")
    parser.add_argument("--check-golden", action="store_true", help="只核对 golden 漂移")
    parser.add_argument("--update-baseline", action="store_true", help="把本次结果写成新基线")
    parser.add_argument("--outputs", default=None, help="产物目录（默认 outputs/eval_<ts>）")
    parser.add_argument("--json", default=None, help="把结果另存为 JSON（供 CI 消费）")
    args = parser.parse_args()

    suite = yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))
    typos = lint_suite(suite.get("cases") or [])
    if typos:
        print("评测集自身有问题（先修这个，否则门禁是假门）：")
        for line in typos:
            print(f"  ✘ {line}")
        return 1
    drift = check_golden(suite)
    if drift:
        print("golden 漂移（评测集与数据不同步，先修这个再谈系统质量）：")
        for line in drift:
            print(f"  ✘ {line}")
        if args.check_golden:
            return 1
        print("  → 已中止：golden 不可信时，任何分数都没有意义")
        return 1
    print(f"golden 核对通过：{len(suite.get('golden') or {})} 项已独立重算一致")
    if args.check_golden:
        return 0

    if message := preflight_credentials(args.mode):
        print(message)
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    root = Path(args.outputs) if args.outputs else PROJECT_ROOT / "outputs" / f"eval_{stamp}_{args.mode}"
    root.mkdir(parents=True, exist_ok=True)

    wanted = {s.strip() for s in (args.only or "").split(",")} if args.only else None
    cases = [c for c in suite["cases"] if not wanted or str(c["id"]) in wanted]
    print(f"评测集 v{suite.get('version')} ｜ {len(cases)} 题 ｜ mode={args.mode} ｜ 产物 {root}\n")

    results: list[dict[str, Any]] = []
    for case in cases:
        case["suite_version"] = suite.get("version")
        try:
            row = run_case(case, suite, args.mode, root)
        except Exception as exc:  # noqa: BLE001 - 单题异常不炸整批，但记 error
            row = {"case_id": str(case["id"]), "verdict": "error", "status": "exception", "error": f"{type(exc).__name__}: {str(exc)[:200]}", "checks": [], "metrics": {}, "seconds": 0}
        results.append(row)
        print(f"  {row.get('verdict', 'error'):6} {row.get('case_id')}")

    summary = aggregate(results)
    payload = {
        "suite_version": suite.get("version"),
        "mode": args.mode,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "aggregate": summary,
        "results": results,
    }
    Path(root, "eval_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    problems: list[str] = []
    if args.update_baseline:
        # 重定基线这一次不做回归比对：与一份不可比的旧基线（题数/模式不同）判红没有意义
        print("提示：--update-baseline 本次不做回归比对，仅以当前结果重写基线")
    elif BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        if baseline.get("mode") != args.mode:
            print(f"提示：基线模式 {baseline.get('mode')} 与本次 {args.mode} 不同，跳过比对")
        elif len(baseline.get("results") or []) != len(results):
            print(
                f"提示：基线 {len(baseline.get('results') or [])} 题、本次 {len(results)} 题，"
                "题集不一致时聚合指标不可比，只做逐题 gate 比对"
            )
            problems = compare_baseline(payload, baseline, check_aggregate=False)
        else:
            problems = compare_baseline(payload, baseline)
    else:
        print("提示：还没有基线（evals/baseline.json），本次只出结果不设门禁")

    if args.update_baseline:
        payload["aggregate"]["fingerprints"] = fingerprint(
            agents=agent_prompt_map(), config=load_config(None), extra_files=HARNESS_FILES
        )
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入基线：{BASELINE_PATH}（{len(payload['results'])} 题）")
        if wanted:
            print("  ⚠ 注意：本次是子集运行，基线只覆盖被跑到的题——全量门禁请对全量写基线")

    print_report(results, summary, problems)
    failed_cases = [r for r in results if r.get("verdict") in ("fail", "error", "xpass")]
    return 1 if (problems or failed_cases) else 0


if __name__ == "__main__":
    sys.exit(main())
