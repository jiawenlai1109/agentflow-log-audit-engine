"""后端配置：路径、鉴权开关、限制。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
APP_DATA_DIR = PROJECT_ROOT / ".appdata"
DB_PATH = APP_DATA_DIR / "app.db"
DATASETS_DIR = APP_DATA_DIR / "datasets"
SESSIONS_ROOT = OUTPUTS_ROOT / "sessions"

# 本地单机默认关闭强制鉴权（接口仍发 Token，便于多用户时开启）
AUTH_ENABLED = False
SECRET = "dev-secret-change-me"
MAX_UPLOAD_MB = 50
ALLOWED_EXTENSIONS = {".csv"}
