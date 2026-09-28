"""数据集：上传/列表/删除（复用 Explorer 画像做校验与元数据）。"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.config import ALLOWED_EXTENSIONS, DATASETS_DIR, MAX_UPLOAD_MB
from app.db import execute, query
from app.deps import get_current_user
from app.schemas import DatasetOut
from agentflow.core.tools import profile_table

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


@router.post("", response_model=DatasetOut)
def upload_dataset(
    file: UploadFile = File(...), user: dict = Depends(get_current_user)
) -> dict:
    suffix = Path(file.filename or "data.csv").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持 CSV 文件")
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    path = DATASETS_DIR / f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    path.write_bytes(file.file.read())
    if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"文件超过 {MAX_UPLOAD_MB}MB 限制")
    try:
        profile = profile_table(path)
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"CSV 解析失败：{exc}") from exc
    dataset_id = execute(
        "INSERT INTO datasets (user_id, filename, path, size, row_count, columns) VALUES (?, ?, ?, ?, ?, ?)",
        (
            user["id"],
            file.filename,
            str(path),
            path.stat().st_size,
            profile["row_count"],
            __import__("json").dumps([c["name"] for c in profile["columns"]], ensure_ascii=False),
        ),
    )
    return {
        "id": dataset_id,
        "filename": file.filename,
        "size": path.stat().st_size,
        "row_count": profile["row_count"],
        "columns": [c["name"] for c in profile["columns"]],
    }


@router.get("", response_model=list[DatasetOut])
def list_datasets(user: dict = Depends(get_current_user)) -> list[dict]:
    rows = query("SELECT * FROM datasets WHERE user_id = ? ORDER BY id DESC", (user["id"],))
    for row in rows:
        row["columns"] = __import__("json").loads(row["columns"] or "[]")
    return rows


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: int, user: dict = Depends(get_current_user)) -> dict:
    rows = query(
        "SELECT * FROM datasets WHERE id = ? AND user_id = ?", (dataset_id, user["id"])
    )
    if not rows:
        raise HTTPException(status_code=404, detail="数据集不存在")
    path = Path(rows[0]["path"])
    if path.exists():
        path.unlink()
    execute("DELETE FROM datasets WHERE id = ? AND user_id = ?", (dataset_id, user["id"]))
    return {"ok": True}
