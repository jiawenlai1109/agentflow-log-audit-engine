"""运行历史、报告文本、评估聚合（按用户归属读，run_id 走格式白名单）。"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app import config
from app.db import query
from app.deps import ensure_run_access, get_current_user, guard_within
from app.schemas import EvaluationSummary
from app.security import MEDIA_TOKEN_TTL_SECONDS, SCOPE_MEDIA, make_token

router = APIRouter(prefix="/api", tags=["reports"])

RUN_ID_PATTERN = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{8}$")


def _valid_run_id(run_id: str) -> str:
    """run_id 来自 URL，进文件路径前先按生成规则收紧，杜绝 `..` 与任意段。"""
    if not RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    return run_id


def _user_runs(user: dict[str, Any]) -> list[dict[str, Any]]:
    """归属来自 jobs 表（run 由谁提交），指标来自 evaluation.json（谁跑出了什么）。

    鉴权上线前由 CLI 直跑产生的 run 没有 jobs 记录，因此不出现在列表里——默认拒绝。
    """
    rows = query(
        "SELECT run_id, question, status, created_at FROM jobs "
        "WHERE user_id = ? AND run_id IS NOT NULL ORDER BY id DESC",
        (user["id"],),
    )
    runs: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["run_id"])
        if not RUN_ID_PATTERN.match(run_id):
            continue
        evaluation_file = config.outputs_root() / run_id / "evaluation.json"
        try:
            evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        runs.append(
            {
                "run_id": evaluation.get("run_id", run_id),
                "status": evaluation.get("status"),
                "question": evaluation.get("question") or row["question"],
                "duration_seconds": evaluation.get("duration_seconds"),
                "llm_calls": evaluation.get("llm_calls"),
                "chart_success": evaluation.get("chart_success"),
                "critic_pass": evaluation.get("critic_pass"),
                "degraded_reason": evaluation.get("degraded_reason"),
                "created_at": row.get("created_at"),
                "report_path": str(evaluation_file.parent / "report.md"),
            }
        )
    return runs


def _normalize_report_links(
    run_id: str, content: str, media_token: str = ""
) -> str:
    """把报告内图片引用统一转为可访问的 /outputs/<run_id>/... URL。

    兼容两种写法：相对路径（./artifacts/...，新报告）与绝对本地路径（D:\\...\\outputs\\...，旧报告）。
    media_token 非空时附到 URL 上——产物目录已不再公开，图片需要带只读媒体 token 才取到。
    """

    def _signed(url: str) -> str:
        # 两轮正则可能命中同一张图（先相对、再绝对路径），已签过的不再追加
        return url if not media_token or "?t=" in url else f"{url}?t={media_token}"

    content = re.sub(
        r"!\[([^\]]*)\]\(\s*\.?/?((?:artifacts|work|sessions)/[^)]*)\)",
        lambda m: f"![{m.group(1)}]({_signed(f'/outputs/{run_id}/{m.group(2)}')})",
        content,
    )

    def _absolute_link(match: re.Match) -> str:
        raw = match.group(2).replace("\\", "/")
        marker = "/outputs/"
        idx = raw.find(marker)
        if idx >= 0:
            return f"![{match.group(1)}]({_signed(raw[idx:])})"
        return match.group(0)

    content = re.sub(
        r"!\[([^\]]*)\]\(([^)]*outputs[\\/][^)]+)\)",
        _absolute_link,
        content,
    )
    return content


@router.get("/runs")
def list_runs(user: dict = Depends(get_current_user)) -> list[dict]:
    return list(reversed(_user_runs(user)))


@router.get("/reports/{run_id}")
def get_report(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run_id = _valid_run_id(run_id)
    ensure_run_access(run_id, user)
    report_path = guard_within(config.outputs_root() / run_id, "report.md")
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    content = report_path.read_text(encoding="utf-8")
    token = make_token(user["username"], scope=SCOPE_MEDIA, run_scope=run_id)
    return {
        "run_id": run_id,
        "content": _normalize_report_links(run_id, content, token),
        "media_expires_in": MEDIA_TOKEN_TTL_SECONDS,
    }


@router.get("/evaluations/summary", response_model=EvaluationSummary)
def evaluation_summary(user: dict = Depends(get_current_user)) -> dict:
    runs = _user_runs(user)
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
