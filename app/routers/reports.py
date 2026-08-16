"""运行历史、报告文本、评估聚合。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.config import OUTPUTS_ROOT
from app.schemas import EvaluationSummary

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/runs")
def list_runs() -> list[dict]:
    runs = []
    for evaluation_file in sorted(OUTPUTS_ROOT.glob("run_*/evaluation.json")):
        try:
            evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        runs.append(
            {
                "run_id": evaluation.get("run_id"),
                "status": evaluation.get("status"),
                "question": evaluation.get("question"),
                "duration_seconds": evaluation.get("duration_seconds"),
                "llm_calls": evaluation.get("llm_calls"),
                "chart_success": evaluation.get("chart_success"),
                "critic_pass": evaluation.get("critic_pass"),
                "degraded_reason": evaluation.get("degraded_reason"),
                "report_path": str(evaluation_file.parent / "report.md"),
            }
        )
    return list(reversed(runs))


@router.get("/reports/{run_id}")
def get_report(run_id: str) -> dict:
    report_path = OUTPUTS_ROOT / run_id / "report.md"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"run_id": run_id, "content": report_path.read_text(encoding="utf-8")}


@router.get("/evaluations/summary", response_model=EvaluationSummary)
def evaluation_summary() -> dict:
    runs = list_runs()
    total = len(runs)

    def count(status: str) -> int:
        return sum(1 for run in runs if run["status"] == status)

    chart_total = sum(1 for run in runs if run["chart_success"] is not None)
    chart_ok = sum(1 for run in runs if run["chart_success"])
    critic_total = sum(1 for run in runs if run["critic_pass"] is not None)
    critic_ok = sum(1 for run in runs if run["critic_pass"])
    reasons = {}
    for run in runs:
        reason = run.get("degraded_reason")
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "total": total,
        "status_count": {"success": count("success"), "partial": count("partial"), "degraded": count("degraded"), "failed": count("failed")},
        "degraded_reasons": reasons,
        "chart": {"total": chart_total, "ok": chart_ok},
        "critic": {"total": critic_total, "ok": critic_ok},
        "avg_llm_calls": round(sum(run.get("llm_calls") or 0 for run in runs) / max(1, total), 1),
        "avg_duration": round(sum(run.get("duration_seconds") or 0 for run in runs) / max(1, total), 2),
        "runs": runs[:50],
    }
