"""分析任务：提交、状态查询、SSE 进度流（全部按用户归属收口）。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.config import OUTPUTS_ROOT
from app.db import execute, query_one
from app.deps import get_current_user
from app.jobs import JobManager
from app.routers.bundles import load_bundle_for_analysis
from app.schemas import AnalyzeRequest, JobOut
from agentflow.pipeline import run_analysis

router = APIRouter(prefix="/api", tags=["jobs"])
manager = JobManager(max_workers=2)

PHASE_PROGRESS = {"explore": 10, "plan": 20, "execute": 60, "report": 85, "review": 95}
_JOB_FIELDS = "job_id, user_id, status, progress, run_id, error, question"


def _owned_job(job_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """归属过滤写进 SQL 本身，不靠调用方先查再比——将来谁删了比对，这条语句仍然拦得住。

    不区分"不存在"与"别人的"，一律 404，避免 job_id 枚举。
    """
    job = query_one(
        f"SELECT {_JOB_FIELDS} FROM jobs WHERE job_id = ? AND user_id = ?",
        (job_id, user["id"]),
    )
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def submit_analysis(
    question: str, sources: Any, mode: str, session_id: str | None, user_id: int
) -> str:
    """sources 可以是文件路径，也可以是 Bundle——pipeline 里 `as_bundle` 会归一化。"""
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    execute(
        "INSERT INTO jobs (job_id, user_id, question, mode, session_id, status) VALUES (?, ?, ?, ?, ?, 'pending')",
        (job_id, user_id, question, mode, session_id),
    )

    def worker() -> None:
        def on_event(event: dict[str, Any]) -> None:
            manager.publish(job_id, event)
            if event.get("type") == "phase":
                progress = PHASE_PROGRESS.get(event.get("phase"), 50)
                execute(
                    "UPDATE jobs SET status='running', progress=? WHERE job_id=? AND user_id=?",
                    (progress, job_id, user_id),
                )
            if event.get("type") == "done":
                execute(
                    "UPDATE jobs SET status=?, run_id=?, finished_at=?, progress=100 "
                    "WHERE job_id=? AND user_id=?",
                    (
                        event.get("status"),
                        event.get("run_id"),
                        datetime.now().isoformat(),
                        job_id,
                        user_id,
                    ),
                )

        try:
            result = run_analysis(
                question=question,
                sources=sources,
                mode=mode,
                outputs_root=OUTPUTS_ROOT,
                session_id=session_id,
                on_event=on_event,
            )
            if result.get("status") != "success":
                execute(
                    "UPDATE jobs SET status=?, run_id=?, finished_at=?, progress=100 "
                    "WHERE job_id=? AND user_id=?",
                    (
                        result.get("status"),
                        result.get("run_id"),
                        datetime.now().isoformat(),
                        job_id,
                        user_id,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? "
                "WHERE job_id=? AND user_id=?",
                (str(exc)[:500], datetime.now().isoformat(), job_id, user_id),
            )
            manager.publish(job_id, {"type": "error", "error": str(exc)[:500]})

    manager.submit(job_id, worker)
    return job_id


@router.post("/analyze", response_model=JobOut)
def analyze(payload: AnalyzeRequest, user: dict = Depends(get_current_user)) -> dict:
    if payload.bundle_id:
        # 归属、状态、目录包含、快照可读——四步都在 bundles 模块里做一次（同一个 BUNDLES_DIR）
        sources = load_bundle_for_analysis(payload.bundle_id, user)
    else:
        dataset = query_one(
            "SELECT * FROM datasets WHERE id = ? AND user_id = ?",
            (payload.dataset_id, user["id"]),
        )
        if not dataset:
            raise HTTPException(status_code=404, detail="数据集不存在")
        sources = dataset["path"]
    if payload.session_id and not query_one(
        "SELECT * FROM sessions WHERE session_id = ? AND user_id = ?",
        (payload.session_id, user["id"]),
    ):
        raise HTTPException(status_code=404, detail="会话不存在")
    job_id = submit_analysis(
        payload.question, sources, payload.mode, payload.session_id, user["id"]
    )
    return _owned_job(job_id, user)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _owned_job(job_id, user)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, user: dict = Depends(get_current_user)) -> StreamingResponse:
    _owned_job(job_id, user)

    async def event_stream():
        index = 0
        while True:
            events, total = manager.snapshot(job_id, index)
            for event in events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                index += 1
            if manager.is_done(job_id) and index >= total:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
