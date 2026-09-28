"""配置加载：YAML + 环境变量。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = {
    "llm": {"model": "", "base_url": "", "temperature": 0.2},
    "execution": {
        "max_concurrency": 3,
        "task_timeout_seconds": 30,
        "total_budget_seconds": 120,
        "max_llm_calls": 30,
        "max_executor_attempts": 3,
        "max_inspector_redos": 3,
        "max_review_rounds": 2,
        "allow_partial_results": True,
    },
    "agents": {},
    # skill：能力面开关。关掉一只 skill 是可执行的动作，且必须留下原因（见 core/skill.py）
    "skills": {"disabled": []},
    # mcp：外部 server 白名单与能力分级（见 core/mcp.py）。默认一个都不接。
    "mcp": {"servers": [], "max_calls": 5},
}

# core/config.py → agentflow → src → 项目根
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "agents.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载配置：未显式给路径时用项目默认 config/agents.yaml，文件不存在才退默认。

    合并按 section 逐键覆盖（不是整段替换）：YAML 少写一个 execution 键时，
    该键仍取 DEFAULT_CONFIG 的值，而不是静默消失——否则"改了配置没生效"会
    变成"改了配置把别的配置弄丢了"。
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    config: dict[str, Any] = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in DEFAULT_CONFIG.items()
    }
    if not config_path.exists():
        return config
    with config_path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    for key in ("llm", "execution", "agents", "skills", "mcp"):
        section = loaded.get(key)
        if not isinstance(section, dict):
            continue
        base = config.get(key) if isinstance(config.get(key), dict) else {}
        config[key] = {**base, **section}
    return config


def load_dotenv(path: str | Path | None = None) -> None:
    """极简 .env 加载：KEY=VALUE，已存在的环境变量不覆盖。"""
    env_path = Path(path) if path else Path.cwd() / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
