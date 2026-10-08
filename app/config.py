"""后端配置：路径与限制。鉴权参数见 app/security.py。"""

from __future__ import annotations

import os
from pathlib import Path

from agentflow.core.config import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 必须在 security.py 读环境变量之前完成：直接 `python -m uvicorn app.main:app` 启动时
# 没人加载 .env，APP_SECRET 会静默退化成每次启动随机（重启即踢掉所有已登录会话）。
load_dotenv(PROJECT_ROOT / ".env")


def _env_path(name: str, default: Path) -> Path:
    """目录一律可被环境变量改指：① 压测与多实例不能写在开发机的真库上；
    ② P2 把 worker 拆成独立进程后，两个进程要能对同一份数据说话。
    没配就用仓库默认路径——不设环境变量时的行为与今天逐字节相同。"""
    value = (os.getenv(name) or "").strip()
    return Path(value).expanduser().resolve() if value else default


# 路径**只在调用时读**，不在 import 时算成模块常量。理由不是风格：
# 一个 job 现在可能被另一个进程认领（scripts/worker.py），那个进程只能靠环境变量知道
# 数据在哪；而 import 时定值的常量会被每个模块各复制一份，于是"改一处、另外几处还在读
# 老路径"。测试就是这么把 run 产物写进仓库真 outputs/ 的（见 工作日志 2026-10-07）。
def outputs_root() -> Path:
    return _env_path("OUTPUTS_ROOT", PROJECT_ROOT / "outputs")


def app_data_dir() -> Path:
    return _env_path("APP_DATA_DIR", PROJECT_ROOT / ".appdata")


def db_path() -> Path:
    return _env_path("DB_PATH", app_data_dir() / "app.db")


def datasets_dir() -> Path:
    return _env_path("DATASETS_DIR", app_data_dir() / "datasets")


def bundles_dir() -> Path:
    return _env_path("BUNDLES_DIR", app_data_dir() / "bundles")


# 企业命名空间：**一个企业一棵树**。run 产物、引擎的 `bd_<指纹>` 归一化缓存、会话目录
# 全都在 `$OUTPUTS_ROOT/org/<id>/` 底下。
#
# 为什么要物理分树（P3 之前只有 API 那一层拦着）：
# ① 报告按企业共享读，产物文件却是一棵全局树——隔离只剩"路由记得判归属"这一条依赖，
#    漏一处就是跨企业读文件；分树之后跨企业连"这个文件在不在"都探测不到。
# ② `bd_<内容指纹>` 那份归一化缓存原来是全局共享的：字节完全相同的上传会让两个企业在
#    同一个目录名下相遇，"目录已存在"这个事实本身就在泄露别家传过什么。
# ③ P4 要按企业算磁盘配额，物理分树之后一次目录遍历就是答案，不用先还原本该属于谁。
ORG_NAMESPACE = "org"


def org_outputs_root(org_id: int) -> Path:
    """企业树根。这里 `int()` 一把是刻意的：org_id 来自库里但会进文件路径，
    非数字要当场炸在这里，而不是拼出一个多一层或带 `..` 的位置。"""
    return outputs_root() / ORG_NAMESPACE / str(int(org_id))


def sessions_root(org_id: int) -> Path:
    """会话目录 = 企业树里的 `sessions/`。

    这里原来读一个 `SESSIONS_ROOT` 环境变量，而**它从来没生效过**：引擎算会话目录用的是
    它拿到的 `outputs_root`（`pipeline.run_analysis`），一个字都不读这个变量。所以只要有人
    真去设它，Web 侧读 A、引擎写 B，同一份会话被劈成两个目录——一个看起来能配置、实际会
    把上下文搞丢的旋钮，比没有旋钮更坏。现在位置只有一条推导式，并且由调用方**告知**引擎
    （`sessions_root=` 参数），不让它猜。
    """
    return org_outputs_root(org_id) / "sessions"


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
