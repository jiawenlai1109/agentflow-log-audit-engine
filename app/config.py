"""后端配置：路径与限制。鉴权参数见 app/security.py。"""

from __future__ import annotations

from pathlib import Path

from agentflow.core.config import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 必须在 security.py 读环境变量之前完成：直接 `python -m uvicorn app.main:app` 启动时
# 没人加载 .env，APP_SECRET 会静默退化成每次启动随机（重启即踢掉所有已登录会话）。
load_dotenv(PROJECT_ROOT / ".env")

OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
APP_DATA_DIR = PROJECT_ROOT / ".appdata"
DB_PATH = APP_DATA_DIR / "app.db"
DATASETS_DIR = APP_DATA_DIR / "datasets"
BUNDLES_DIR = APP_DATA_DIR / "bundles"
SESSIONS_ROOT = OUTPUTS_ROOT / "sessions"

MAX_UPLOAD_MB = 50
ALLOWED_EXTENSIONS = {".csv"}

# Bundle 上传（#20）：一次请求最多几个文件、允许哪些扩展名
# （表类走 ingest 归一化，文本/日志类只进"证据"通道——I1 纪律）
MAX_BUNDLE_FILES = 12
BUNDLE_TABLE_EXTENSIONS = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xlsx", ".xlsm", ".xls", ".parquet"}
BUNDLE_DOCUMENT_EXTENSIONS = {".txt", ".log", ".md", ".markdown", ".yaml", ".yml", ".xml", ".ini", ".conf"}
BUNDLE_ALLOWED_EXTENSIONS = BUNDLE_TABLE_EXTENSIONS | BUNDLE_DOCUMENT_EXTENSIONS

# 产物目录只回图片：报告与 evaluation.json 一律走带归属校验的 API
MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
