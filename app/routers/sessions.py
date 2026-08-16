"""会话与轮次管理（复用 SessionStore）。"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.config import OUTPUTS_ROOT, SESSIONS_ROOT
from app.db import execute, query, query_one
from app.routers.jobs import submit_analysis
from app.schemas import JobOut, MessageOut, SessionCreateRequest, SessionOut
from agentflow.core.context import SessionContext

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut)
def create_session(payload: SessionCreateRequest) -> dict:
    session_id = f"session_{uuid.uuid4().hex[:10]}"
    dataset_path = None
    if payload.dataset_id:
        dataset = query_one("SELECT * FROM datasets WHERE id = ?", (payload.dataset_id,))
        if dataset:
            dataset_path = dataset["path"]
    execute(
        "INSERT INTO sessions (session_id, title, dataset_path) VALUES (?, ?, ?)",
        (session_id, payload.title, dataset_path),
    )
    return {
        "session_id": session_id,
        "title": payload.title,
        "turn_count": 0,
        "dataset_path": dataset_path,
    }


@router.get("", response_model=list[SessionOut])
def list_sessions() -> list[dict]:
    rows = query("SELECT * FROM sessions ORDER BY id DESC")
    result = []
    for row in rows:
        session = SessionContext(row["session_id"], SESSIONS_ROOT / row["session_id"])
        result.append(
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "turn_count": len(session.read_turns()),
                "dataset_path": row["dataset_path"],
            }
        )
    return result


@router.get("/{session_id}/messages", response_model=list[MessageOut])
def session_messages(session_id: str) -> list[dict]:
    if not query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,)):
        raise HTTPException(status_code=404, detail="会话不存在")
    session = SessionContext(session_id, SESSIONS_ROOT / session_id)
    return [
        {
            "turn": turn.get("turn"),
            "question": turn.get("question", ""),
            "run_id": turn.get("run_id"),
            "answer_summary": turn.get("answer_summary", ""),
            "key_numbers": turn.get("key_numbers", {}),
        }
        for turn in session.read_turns()
    ]


@router.post("/{session_id}/messages", response_model=JobOut)
def post_message(session_id: str, payload: dict) -> dict:
    session = query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    question = str(payload.get("question", "")).strip()
    mode = str(payload.get("mode", "mock"))
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    dataset_path = session["dataset_path"]
    if not dataset_path or not Path(dataset_path).exists():
        raise HTTPException(status_code=400, detail="会话未绑定数据集或数据集已删除")
    job_id = submit_analysis(question, dataset_path, mode, session_id)
    execute("UPDATE sessions SET updated_at = datetime('now','localtime') WHERE session_id = ?", (session_id,))
    return query_one("SELECT job_id, status, progress, run_id, error, question FROM jobs WHERE job_id = ?", (job_id,))


@router.delete("/{session_id}")
def delete_session(session_id: str) -> dict:
    if not query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,)):
        raise HTTPException(status_code=404, detail="会话不存在")
    execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    session_dir = SESSIONS_ROOT / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return {"ok": True}
