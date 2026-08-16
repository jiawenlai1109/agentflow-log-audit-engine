"""分析任务：提交、状态查询、SSE 进度流。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import OUTPUTS_ROOT
from app.db import execute, query_one
from app.jobs import JobManager
from app.schemas import AnalyzeRequest, JobOut
from agentflow.pipeline import run_analysis

router = APIRouter(prefix="/api", tags=["jobs"])
manager = JobManager(max_workers=2)

PHASE_PROGRESS = {"explore": 10, "plan": 20, "execute": 60, "report": 85, "review": 95}


def submit_analysis(question: str, dataset_path: str, mode: str, session_id: str | None) -> str:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    execute(
        "INSERT INTO jobs (job_id, question, mode, session_id, status) VALUES (?, ?, ?, ?, 'pending')",
        (job_id, question, mode, session_id),
    )

    def worker() -> None:
        def on_event(event: dict[str, Any]) -> None:
            manager.publish(job_id, event)
            if event.get("type") == "phase":
                progress = PHASE_PROGRESS.get(event.get("phase"), 50)
                execute(
                    "UPDATE jobs SET status='running', progress=? WHERE job_id=?",
                    (progress, job_id),
                )
            if event.get("type") == "done":
                execute(
                    "UPDATE jobs SET status=?, run_id=?, finished_at=?, progress=100 WHERE job_id=?",
                    (event.get("status"), event.get("run_id"), datetime.now().isoformat(), job_id),
                )

        try:
            result = run_analysis(
                question=question,
                data_path=dataset_path,
                mode=mode,
                outputs_root=OUTPUTS_ROOT,
                session_id=session_id,
                on_event=on_event,
            )
            if result.get("status") != "success":
                execute(
                    "UPDATE jobs SET status=?, run_id=?, finished_at=?, progress=100 WHERE job_id=?",
                    (result.get("status"), result.get("run_id"), datetime.now().isoformat(), job_id),
                )
        except Exception as exc:  # noqa: BLE001
            execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE job_id=?",
                (str(exc)[:500], datetime.now().isoformat(), job_id),
            )
            manager.publish(job_id, {"type": "error", "error": str(exc)[:500]})

    manager.submit(job_id, worker)
    return job_id


@router.post("/analyze", response_model=JobOut)
def analyze(payload: AnalyzeRequest) -> dict:
    dataset = query_one("SELECT * FROM datasets WHERE id = ?", (payload.dataset_id,))
    if not dataset:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if payload.session_id:
        session = query_one("SELECT * FROM sessions WHERE session_id = ?", (payload.session_id,))
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
    job_id = submit_analysis(payload.question, dataset["path"], payload.mode, payload.session_id)
    return query_one("SELECT job_id, status, progress, run_id, error, question FROM jobs WHERE job_id = ?", (job_id,))


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str) -> dict:
    job = query_one("SELECT job_id, status, progress, run_id, error, question FROM jobs WHERE job_id = ?", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
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
