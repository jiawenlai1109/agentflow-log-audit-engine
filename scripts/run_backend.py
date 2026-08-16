"""独立后端启动器：内部切换到项目根再启动 uvicorn（供 schtasks 等独立调用）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn  # noqa: E402
from agentflow.core.config import load_dotenv  # noqa: E402


def main() -> None:
    # 后端进程必须加载项目根 .env（计划任务环境不含用户变量）
    load_dotenv(PROJECT_ROOT / ".env")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
