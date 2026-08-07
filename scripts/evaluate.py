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
    return {
        "total": total,
        "status_count": status_count,
        "degraded_reasons": {
            reason: sum(1 for r in runs if r.get("degraded_reason") == reason)
            for reason in sorted({r.get("degraded_reason") for r in runs if r.get("degraded_reason")})
        },
        "chart": {"total": chart_total, "ok": chart_ok},
        "critic": {"total": critic_total, "ok": critic_ok},
        "avg_llm_calls": round(sum(r.get("llm_calls", 0) for r in runs) / max(1, total), 1),
        "avg_duration": round(sum(r.get("duration_seconds", 0) for r in runs) / max(1, total), 2),
    }


def update_record(record_path: Path, batch: str, runs: list[dict]) -> None:
    rows = ["| run_id | 状态 | 降级原因 | 图表 | 评审 | LLM调用 | 耗时 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"]
    for run in runs:
        chart = run.get("chart_success")
        rows.append(
            "| {run} | {status} | {reason} | {chart} | {critic} | {llm} | {dur} |".format(
                run=run.get("run_id", "-"),
                status=run.get("status", "-"),
                reason=run.get("degraded_reason") or "-",
                chart=chart if chart is not None else "-",
                critic=run.get("critic_pass") if run.get("critic_pass") is not None else "-",
                llm=run.get("llm_calls", "-"),
                dur=run.get("duration_seconds", "-"),
            )
        )
    section = (
        f"\n## {batch}\n\n"
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ 运行数：{len(runs)}\n\n"
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
