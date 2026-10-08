"""产物文件读取：/outputs 不再是公开静态目录。

`<img>` 带不了自定义请求头，所以这里认的是报告接口签发的**只读媒体 token**：
scope 与 API token 互斥、900 秒过期、绑定单个 run、只放行图片扩展名。

地址里没有企业段（`/outputs/<run_id>/<文件>`），org 段只在服务器本地的解析结果里——
把 org_id 拼进 URL 换不来任何功能，只是把内部编号与存储布局交给客户端。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import config, paths
from app.deps import get_media_principal

router = APIRouter(tags=["media"])


@router.get("/outputs/{run_id}/{rel_path:path}")
def read_media(run_id: str, rel_path: str, request: Request) -> FileResponse:
    user = get_media_principal(request, run_id)
    target = paths.media_target(run_id, rel_path, user)
    if target.suffix.lower() not in config.MEDIA_EXTENSIONS or not target.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(target)

