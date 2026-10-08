"""闸门的上限从哪来：`环境变量 > 这台站/这个型号的实测干净档 > 保守占位`。

为什么这一层在 Web 端而不是引擎里（引擎只负责"几路在飞"）：读哪份缓存、缓存属不属于
当前这台端点、型号换了算不算数、多新算新鲜——这些是**策略**，而策略要能配置、能被打断点测。
引擎管机制，Web 管策略，这条分工和 #15（归属判据在引擎外也只有一份）是同一个意思。

一处口径值得单独说：**占位值不冒充实测**。`limit_source` 会一路跟着读数走
（SSE 的 queue 块、`/api/jobs`、evaluation.json 里都带），所以"闸门定在 4"这句话
永远带着"这个 4 是没人量过的保守值"或者"这个 16 是 2026-10-08 量出来的"。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agentflow.core import gate as llm_gate
from agentflow.core.config import load_config
from agentflow.core.llm_preflight import host_of, measured_concurrency, read_cache

from app import config as app_config

CACHE_NAME = "llm_preflight.json"


def cache_path(config: dict[str, Any] | None = None) -> Path:
    """预检缓存的位置：`llm.preflight_cache` 可覆盖，否则 `$APP_DATA_DIR/llm_preflight.json`。

    这条从 `app/routers/llm.py` 搬过来，不复制一份——"缓存在哪"有两条式子的下场，
    就是接口报"没有预检结论"而闸门却按一份旧缓存开闸（或反过来）。
    """
    loaded = config if config is not None else load_config()
    configured = (loaded.get("llm", {}) or {}).get("preflight_cache")
    return Path(configured) if configured else app_config.app_data_dir() / CACHE_NAME


def effective(config: dict[str, Any] | None = None) -> tuple[str, str]:
    """当前实际会用的 (base_url, model)：配置留空退环境变量，与 pipeline 同一套优先级。"""
    loaded = config if config is not None else load_config()
    llm_cfg = loaded.get("llm", {}) or {}
    base_url = llm_cfg.get("base_url") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = llm_cfg.get("model") or os.getenv("LLM_MODEL") or "gpt-4o-mini"
    return str(base_url), str(model)


def measured_limit(config: dict[str, Any] | None = None) -> tuple[int | None, str]:
    """从缓存里取这台站/这个型号的实测干净档数。取不到就回 None，不编一个数。"""
    loaded = config if config is not None else load_config()
    base_url, model = effective(loaded)
    report = read_cache(cache_path(loaded))
    if not report or report.get("base_host") != host_of(base_url):
        # 主机不同就是另一次测量：别台站量出的档数在这台不作数
        return None, "缓存不属于当前端点"
    limit, note = measured_concurrency(report, base_url, model)
    return limit, note


def snapshot() -> dict[str, Any]:
    """给接口与事件流用的闸门读数（含"这个上限从哪来"）。"""
    return llm_gate.snapshot()


def apply_for_process(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """按三级解析开闸，返回读数（启动日志与 `/api/jobs` 的 queue 块都印它）。

    幂等：已经由测试或运维显式配过的闸门（`configure_for_run`）不被启动逻辑覆盖——
    否则"谁最后写了一次闸门"就没人说得清了。
    """
    loaded = config if config is not None else load_config()
    measured, note = measured_limit(loaded)
    limit, source = llm_gate.limit_and_source(measured=measured)
    gate = llm_gate.configure_for_run(limit, source=source)
    snapshot = gate.snapshot()
    snapshot["note"] = note  # 把"为什么是这个数"的那句话一起带回去
    return snapshot
