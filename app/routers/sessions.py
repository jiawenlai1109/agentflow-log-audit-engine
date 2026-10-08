"""会话与轮次管理（复用 SessionStore，按用户归属收口）。"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app import access, paths
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
    """不区分"不存在"与"别人的"，一律 404。

    会话用 OWNER 而不是 SHARED：多轮记忆是**个人上下文**，用户没有说要把对话共享给同事，
    而共享一份能驱动分析的会话历史，等于把别人写的提示词与中间结论也一起给出去。
    数据集/报告走 SHARED，会话不走——这条区分是刻意的，不是漏了。
    """
    sql, params = access.scope(user, access.OWNER)
    row = query_one(f"SELECT * FROM sessions WHERE session_id = ?{sql}", (session_id, *params))
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


@router.post("", response_model=SessionOut)
def create_session(payload: SessionCreateRequest, user: dict = Depends(get_current_user)) -> dict:
    session_id = f"session_{uuid.uuid4().hex[:10]}"
    dataset_path = None
    if payload.dataset_id:
        # 数据集可以是同企业别人的（SHARED），会话本身仍是私有的
        dataset = access.dataset_row(user, payload.dataset_id)
        if not dataset:
            raise HTTPException(status_code=404, detail="数据集不存在")
        dataset_path = dataset["path"]
    execute(
        "INSERT INTO sessions (session_id, user_id, org_id, title, dataset_path) VALUES (?, ?, ?, ?, ?)",
        (session_id, user["id"], access.primary_org(user), payload.title, dataset_path),
    )
    return {
        "session_id": session_id,
        "title": payload.title,
        "turn_count": 0,
    }


@router.get("", response_model=list[SessionOut])
def list_sessions(user: dict = Depends(get_current_user)) -> list[dict]:
    sql, params = access.scope(user, access.OWNER)
    rows = query(f"SELECT * FROM sessions WHERE 1{sql} ORDER BY id DESC", params)
    result = []
    for row in rows:
        session = SessionContext(row["session_id"], paths.session_dir(row))
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
    row = _owned_session(session_id, user)
    session = SessionContext(session_id, paths.session_dir(row))
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
        user,
        source_ref=f"path:{dataset_path}",
        # 作业行的企业归属 = **此刻提交者的企业**（可见性判据用它算）；会话树的位置另按
        # 会话行自己的 org 算（见 app/paths.py）。两条各管各的，谁也不覆盖谁。
        org_id=access.primary_org(user),
    )
    sql, params = access.scope(user, access.OWNER)
    execute(
        "UPDATE sessions SET updated_at = datetime('now','localtime') "
        f"WHERE session_id = ?{sql}",
        (session_id, *params),
    )
    # 作业与报告是共享的，但这一次运行是**这个会话**里发起的：读回时用 OWNER 判据，
    # 别人在同一企业里跑的作业不会串到这条会话的时间线里。
    job_sql, job_params = access.scope(user, access.OWNER)
    return query_one(
        "SELECT job_id, status, progress, run_id, error, question FROM jobs "
        f"WHERE job_id = ?{job_sql}",
        (job_id, *job_params),
    )


@router.delete("/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)) -> dict:
    row = _owned_session(session_id, user)
    sql, params = access.scope(user, access.OWNER)
    execute(f"DELETE FROM sessions WHERE session_id = ?{sql}", (session_id, *params))
    session_dir = paths.session_dir(row)
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return {"ok": True}
