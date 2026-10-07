"""型号可用性接口：把预检结论给到 Web 侧。只读、不打上游、不出凭据。

为什么需要这一层（2026-10-06/07 实测）：同一台网关上型号之间行为差得很远，
"能不能用"只能实测；而实测结论以前只存在于命令行输出里，页面上看不到，
配了第三方 API 的人能做的决定就只剩"猜"。

三条边界，动这条接口前先读：

1. **这里不发真实调用**。GET 接口一点就烧钱是本项目的红线（`mode` 字段未校验那次
   就是这么被利用的，缺陷 #14）。唯一的预检入口是 `scripts/preflight_llm.py`，
   它会先打印计划调用次数再动手。
2. **不返回凭据，也不返回正文**。预检缓存按设计就没存这些（见 `core/llm_preflight.py`
   模块头），本接口只是把它转述出去，转述时也不新增任何一处外印。
3. **没缓存一律报 `unprobed`**，不替用户猜成"可用"。把没测过说成可用，等于
   前端替用户做了一个没有实测背书的决定。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from app import config as app_config
from app.deps import get_current_user
from agentflow.core.config import load_config
from agentflow.core.llm_preflight import (
    PROBE_VERSION,
    host_of,
    is_fresh,
    lookup,
    mask_secrets,
    read_cache,
)

router = APIRouter(prefix="/api/llm", tags=["llm"])


def _cache_path(config: dict[str, Any]) -> Path:
    """缓存位置与命令行默认同一个：`llm.preflight_cache` 可覆盖，否则 `.appdata/`。"""
    configured = (config.get("llm", {}) or {}).get("preflight_cache")
    return Path(configured) if configured else app_config.app_data_dir() / "llm_preflight.json"


def _effective(config: dict[str, Any]) -> tuple[str, str]:
    """当前实际会用的 (base_url, model)：配置留空退环境变量，与 pipeline 同一套优先级。"""
    llm_cfg = config.get("llm", {}) or {}
    base_url = llm_cfg.get("base_url") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = llm_cfg.get("model") or os.getenv("LLM_MODEL") or "gpt-4o-mini"
    return str(base_url), str(model)


@router.get("/models")
def models(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """这台端点上每个型号的预检结论；没有属于这台主机的缓存时报 `unprobed`。

    要登录：型号清单 + 端点主机名合起来就是"这系统接的是哪家上游"的能力面地图，
    与 `/api/packs` 同级，不是公开信息。
    """
    config = load_config()
    base_url, configured_model = _effective(config)
    report = read_cache(_cache_path(config))
    host = host_of(base_url)
    # 缓存按主机归组：拿别的站的结论给这个站放行/劝退，是预检最不该变成的那种样子
    same_host = bool(report) and report.get("base_host") == host
    # 出口再抹一遍：按主机归组的缓存可能写于"落盘前抹凭据"这道修好之前，
    # 而网关回显在备注里的 token 一旦被这个接口原样转述，就等于把 key 挂到了页面上。
    entries = []
    if same_host:
        for entry in report.get("models") or []:
            levels = entry.get("levels") or {}
            entries.append(
                {
                    "model": str(entry.get("model") or ""),
                    "verdict": str(entry.get("verdict") or "unprobed"),
                    "note": mask_secrets(str(entry.get("note") or "")),
                    "seconds": (levels.get("default") or {}).get("seconds"),
                }
            )
    verdict, note = lookup(report, base_url, configured_model)
    return {
        "base_host": host,
        "status": "probed" if entries else "unprobed",
        "fresh": same_host and bool(report and is_fresh(report)),
        "checked_at": (report or {}).get("generated_at_text") if same_host else None,
        "probe_version": (report or {}).get("probe_version") if same_host else None,
        # 旧版探针的结论不能当新版用：判据换过一次，形状就对不上（PROBE_VERSION 的意义）
        "probe_version_current": (report or {}).get("probe_version") == PROBE_VERSION,
        "cache_file": _cache_path(config).name,
        "configured": {"model": configured_model, "verdict": verdict, "note": mask_secrets(note)},
        "thinking_config": (config.get("llm", {}) or {}).get("thinking") or None,
        "models": sorted(entries, key=lambda item: item["model"]),
        "hint": (
            ""
            if entries
            else f"这台端点（{host}）还没有预检结论。跑 `python scripts/preflight_llm.py` 生成——它会先打印计划调用次数"
        ),
    }
