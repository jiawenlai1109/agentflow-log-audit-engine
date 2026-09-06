"""评估聚合：扫描 outputs/run_*/evaluation.json，输出汇总并更新评估记录.md。

用法：
  python scripts/evaluate.py [--batch "2026-08-07 离线验收"]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 独立校验 PASS 的检查名前缀：通用 aggregate 模板 + 场景包 finding 校验
VERIFY_PASS_PREFIXES = ("aggregate_match_check:PASS", "finding_match_check:PASS")


def collect(outputs_root: Path) -> list[dict]:
    runs: list[dict] = []
    for ev_file in sorted(outputs_root.glob("run_*/evaluation.json")):
        try:
            runs.append(json.loads(ev_file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return runs


def summarize(runs: list[dict]) -> dict:
    total = len(runs)
    status_count = {s: sum(1 for r in runs if r.get("status") == s) for s in ("success", "partial", "degraded", "failed")}
    chart_total = sum(1 for r in runs if r.get("chart_success") is not None)
    chart_ok = sum(1 for r in runs if r.get("chart_success"))
    critic_total = sum(1 for r in runs if r.get("critic_pass") is not None)
    critic_ok = sum(1 for r in runs if r.get("critic_pass"))

    # v1.2 指标：token 成本 / 校验覆盖率 / 路由重规划 / 澄清 / 审核重做
    tokens = {"prompt": 0, "completion": 0}
    verify_checked = verify_ok = 0
    redos = 0
    for run in runs:
        for usage in (run.get("token_usage") or {}).values():
            tokens["prompt"] += int(usage.get("prompt", 0) or 0)
            tokens["completion"] += int(usage.get("completion", 0) or 0)
        for result in (run.get("results") or {}).values():
            verdict = result.get("verdict") or {}
            if verdict.get("verification") == "ok":
                verify_checked += 1
                if any(check.startswith(VERIFY_PASS_PREFIXES) for check in verdict.get("checks", [])):
                    verify_ok += 1
            redos += int(verdict.get("redos", 0) or 0)
    return {
        "total": total,
        "status_count": status_count,
        "degraded_reasons": {
            reason: sum(1 for r in runs if r.get("degraded_reason") == reason)
            for reason in sorted({r.get("degraded_reason") for r in runs if r.get("degraded_reason")})
        },
        "chart": {"total": chart_total, "ok": chart_ok},
        "critic": {"total": critic_total, "ok": critic_ok},
        "verification": {"checked": verify_checked, "ok": verify_ok},
        "replan_runs": sum(1 for r in runs if (r.get("replan_used") or 0) > 0),
        "clarify_runs": sum(1 for r in runs if r.get("clarify")),
        "inspector_redos": redos,
        "tokens": tokens,
        "avg_llm_calls": round(sum(r.get("llm_calls", 0) for r in runs) / max(1, total), 1),
        "avg_duration": round(sum(r.get("duration_seconds", 0) for r in runs) / max(1, total), 2),
    }


def update_record(record_path: Path, batch: str, runs: list[dict]) -> None:
    summary = summarize(runs)
    status_count = summary["status_count"]
    rows = [
        "| run_id | 状态 | 降级原因 | 图表 | 评审 | 校验一致 | 重规划 | 澄清 | LLM调用 | token(p/c) | 耗时 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for run in runs:
        chart = run.get("chart_success")
        verdicts = [
            result.get("verdict") or {} for result in (run.get("results") or {}).values()
        ]
        checked = sum(1 for verdict in verdicts if verdict.get("verification") == "ok")
        passed = sum(
            1
            for verdict in verdicts
            if verdict.get("verification") == "ok"
            and any(c.startswith(VERIFY_PASS_PREFIXES) for c in verdict.get("checks", []))
        )
        usage = run.get("token_usage") or {}
        tokens = (
            "{}/{}".format(
                sum(int(entry.get("prompt", 0) or 0) for entry in usage.values()),
                sum(int(entry.get("completion", 0) or 0) for entry in usage.values()),
            )
            if usage
            else "-"
        )
        rows.append(
            "| {run} | {status} | {reason} | {chart} | {critic} | {verify} | {replan} | {clarify} | {llm} | {tokens} | {dur} |".format(
                run=run.get("run_id", "-"),
                status=run.get("status", "-"),
                reason=run.get("degraded_reason") or "-",
                chart=chart if chart is not None else "-",
                critic=run.get("critic_pass") if run.get("critic_pass") is not None else "-",
                verify=f"{passed}/{checked}" if checked else "-",
                replan=run.get("replan_used") or "-",
                clarify="有" if run.get("clarify") else "-",
                llm=run.get("llm_calls", "-"),
                tokens=tokens,
                dur=run.get("duration_seconds", "-"),
            )
        )
    verification = summary["verification"]
    header = (
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ 运行数：{len(runs)} ｜ "
        f"success {status_count.get('success', 0)} / partial {status_count.get('partial', 0)} / "
        f"degraded {status_count.get('degraded', 0)} / failed {status_count.get('failed', 0)} ｜ "
        f"图表 {summary['chart']['ok']}/{summary['chart']['total']} ｜ "
        f"评审 {summary['critic']['ok']}/{summary['critic']['total']} ｜ "
        f"校验一致 {verification['ok']}/{verification['checked']} ｜ "
        f"重规划run {summary['replan_runs']} ｜ 澄清run {summary['clarify_runs']} ｜ "
        f"审核重做 {summary['inspector_redos']} 次 ｜ "
        f"平均LLM {summary['avg_llm_calls']} ｜ "
        f"token {summary['tokens']['prompt']}/{summary['tokens']['completion']} ｜ "
        f"平均耗时 {summary['avg_duration']}s"
    )
    section = (
        f"\n## {batch}\n\n"
        + header
        + "\n\n"
        + "\n".join(rows)
        + "\n"
    )
    with record_path.open("a", encoding="utf-8") as fh:
        fh.write(section)


def main() -> int:
    parser = argparse.ArgumentParser(description="评估聚合")
    parser.add_argument("--outputs", default=None, help="outputs 根目录（默认 <项目根>/outputs）")
    parser.add_argument("--batch", default=None, help="批次名称（默认自动生成）")
    parser.add_argument("--no-update-record", action="store_true", help="不更新评估记录.md")
    args = parser.parse_args()

    outputs_root = Path(args.outputs) if args.outputs else PROJECT_ROOT / "outputs"
    runs = collect(outputs_root)
    if not runs:
        print("未找到 evaluation.json（outputs 为空？）")
        return 1
    summary = summarize(runs)
    print("=" * 56)
    print(f"运行总数      : {summary['total']}")
    print(f"success       : {summary['status_count'].get('success', 0)}")
    print(f"partial       : {summary['status_count'].get('partial', 0)}")
    print(f"degraded      : {summary['status_count'].get('degraded', 0)}")
    print(f"failed        : {summary['status_count'].get('failed', 0)}")
    print(f"降级原因      : {summary['degraded_reasons'] or '-'}")
    print(f"图表成功率    : {summary['chart']['ok']}/{summary['chart']['total']}")
    print(f"评审通过率    : {summary['critic']['ok']}/{summary['critic']['total']}")
    print(f"校验一致率    : {summary['verification']['ok']}/{summary['verification']['checked']}")
    print(f"重规划/澄清   : {summary['replan_runs']} / {summary['clarify_runs']} runs")
    print(f"审核重做次数  : {summary['inspector_redos']}")
    print(f"token 合计    : p={summary['tokens']['prompt']} c={summary['tokens']['completion']}")
    print(f"平均 LLM 调用 : {summary['avg_llm_calls']}")
    print(f"平均耗时      : {summary['avg_duration']}s")
    print("=" * 56)
    if not args.no_update_record:
        batch = args.batch or f"批次 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        update_record(PROJECT_ROOT / "评估记录.md", batch, runs)
        print(f"已更新 评估记录.md（{batch}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
