"""产物文件读取：/outputs 不再是公开静态目录。

`<img>` 带不了自定义请求头，所以这里认的是报告接口签发的**只读媒体 token**：
scope 与 API token 互斥、900 秒过期、绑定单个 run、只放行图片扩展名。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.config import MEDIA_EXTENSIONS, OUTPUTS_ROOT
from app.deps import ensure_run_access, get_media_principal, guard_within

router = APIRouter(tags=["media"])

RUN_ID_PATTERN = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{8}$")


@router.get("/outputs/{run_id}/{rel_path:path}")
def read_media(run_id: str, rel_path: str, request: Request) -> FileResponse:
    if not RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=404, detail="资源不存在")
    user = get_media_principal(request, run_id)
    ensure_run_access(run_id, user)
    target = guard_within(OUTPUTS_ROOT / run_id, rel_path)
    if target.suffix.lower() not in MEDIA_EXTENSIONS or not target.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(target)
