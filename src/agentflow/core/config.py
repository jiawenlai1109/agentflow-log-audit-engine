"""配置加载：YAML + 环境变量。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = {
    "llm": {"model": "gpt-4o-mini", "temperature": 0.2},
    "execution": {
        "max_concurrency": 3,
        "task_timeout_seconds": 30,
        "total_budget_seconds": 120,
        "max_llm_calls": 30,
    },
    "agents": {},
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载 config/agents.yaml；未提供路径时使用默认配置。"""
    config = dict(DEFAULT_CONFIG)
    if path is None:
        return config
    config_path = Path(path)
    if not config_path.exists():
        return config
    with config_path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    for key in ("llm", "execution", "agents"):
        if key in loaded:
            config[key] = loaded[key]
    return config
