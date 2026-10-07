"""会话与轮次管理（复用 SessionStore，按用户归属收口）。"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app import config
from app.db import execute, query, query_one
from app.deps import get_current_user
from app.routers.jobs import submit_analysis
from app.schemas import (
    JobOut,
    MessageCreateRequest,
    MessageOut,
    SessionCreateRequest,
    SessionOut,
)
from agentflow.core.context import SessionContext

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _owned_session(session_id: str, user: dict) -> dict:
    """不区分"不存在"与"别人的"，一律 404。"""
    row = query_one(
        "SELECT * FROM sessions WHERE session_id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


@router.post("", response_model=SessionOut)
def create_session(payload: SessionCreateRequest, user: dict = Depends(get_current_user)) -> dict:
    session_id = f"session_{uuid.uuid4().hex[:10]}"
    dataset_path = None
    if payload.dataset_id:
        dataset = query_one(
            "SELECT * FROM datasets WHERE id = ? AND user_id = ?",
            (payload.dataset_id, user["id"]),
        )
        if not dataset:
            raise HTTPException(status_code=404, detail="数据集不存在")
        dataset_path = dataset["path"]
    execute(
        "INSERT INTO sessions (session_id, user_id, title, dataset_path) VALUES (?, ?, ?, ?)",
        (session_id, user["id"], payload.title, dataset_path),
    )
    return {
        "session_id": session_id,
        "title": payload.title,
        "turn_count": 0,
    }


@router.get("", response_model=list[SessionOut])
def list_sessions(user: dict = Depends(get_current_user)) -> list[dict]:
    rows = query(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY id DESC", (user["id"],)
    )
    result = []
    for row in rows:
        session = SessionContext(row["session_id"], config.sessions_root() / row["session_id"])
        result.append(
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "turn_count": len(session.read_turns()),
            }
        )
    return result


@router.get("/{session_id}/messages", response_model=list[MessageOut])
def session_messages(
    session_id: str, user: dict = Depends(get_current_user)
) -> list[dict]:
    _owned_session(session_id, user)
    session = SessionContext(session_id, config.sessions_root() / session_id)
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
def post_message(
    session_id: str, payload: MessageCreateRequest, user: dict = Depends(get_current_user)
) -> dict:
    session = _owned_session(session_id, user)
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    dataset_path = session["dataset_path"]
    if not dataset_path or not Path(dataset_path).exists():
        raise HTTPException(status_code=400, detail="会话未绑定数据集或数据集已删除")
    # 会话这条线只有路径可用（sessions.dataset_path 本来就存的是路径）。存成 `path:` 引用
    # 不算新开一个泄漏面——这一列今天已经在库里了；新写的分析入口一律用 bundle:/dataset: id。
    job_id = submit_analysis(
        question,
        dataset_path,
        payload.mode,
        session_id,
        user["id"],
        source_ref=f"path:{dataset_path}",
    )
    execute(
        "UPDATE sessions SET updated_at = datetime('now','localtime') "
        "WHERE session_id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    return query_one(
        "SELECT job_id, status, progress, run_id, error, question FROM jobs "
        "WHERE job_id = ? AND user_id = ?",
        (job_id, user["id"]),
    )


@router.delete("/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)) -> dict:
    _owned_session(session_id, user)
    execute("DELETE FROM sessions WHERE session_id = ? AND user_id = ?", (session_id, user["id"]))
    session_dir = config.sessions_root() / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return {"ok": True}
