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
    """加载配置。

    两类路径不是一件事，别再混成一条：

    - **不传 path**：读项目默认 `config/agents.yaml`。这份文件不在（比如精简过的部署包）
      就退内置 `DEFAULT_CONFIG`——默认配置是可选的。
    - **显式传 path**：这是声明式意图，"我就是要用这份配置"。文件读不到必须报错，
      绝不能退默认：静默退默认会把"配置根本没生效"伪装成"配置生效了但行为没变"，
      归因链当场断掉。（2026-09-28 实测踩过：拿一个解释器看不见的 `/tmp` 路径跑
      "关停某 skill"的实验，`skills.disabled` 一个字都没读进去，而程序毫无异常。）

    合并按 section 逐键覆盖（不是整段替换）：YAML 少写一个 execution 键时，
    该键仍取 DEFAULT_CONFIG 的值，而不是静默消失——否则"改了配置没生效"会
    变成"改了配置把别的配置弄丢了"。
    """
    config: dict[str, Any] = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in DEFAULT_CONFIG.items()
    }
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(
                f"配置文件不存在：{config_path}。显式给出的路径必须真的读到东西；"
                "要使用内置默认就别传 path（或不传 config_path）"
            )
    else:
        config_path = DEFAULT_CONFIG_PATH
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
