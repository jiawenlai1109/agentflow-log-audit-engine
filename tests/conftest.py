import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 测试会话的数据目录一次性指到临时位置，**在 app.config 被导入之前**。
# 为什么现在必须做：P2 把队列的真相搬进库以后，单元测试与集成测试读写的是同一个
# `jobs` 表——它们原来打在开发机的 `.appdata/app.db` 上（实测堆积 536 条 success、
# 57 条 pending），而 `stats()` 会把那些残留当成"当前排队深度"报给前端。
# 用环境变量而不是 monkeypatch 模块属性：认领 job 的可能是另一个进程（scripts/worker.py），
# 它只认环境变量；测试与生产走同一条配置路径，才不会出现"测试通过而部署是坏的"。
_RUN_DIR = Path(tempfile.mkdtemp(prefix="agentflow_test_"))
os.environ.setdefault("APP_DATA_DIR", str(_RUN_DIR / "appdata"))
os.environ.setdefault("OUTPUTS_ROOT", str(_RUN_DIR / "outputs"))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema_once():
    """会话开始时把 schema 建出来一次。

    原来这一步是"借来的"：测试直连的是开发机的真库，那里早就有表。改成临时库之后
    单跑 `pytest tests/test_job_queue.py` 就报 `no such table: users`——那才是它本来的样子。
    每个用例自己要不要重建，仍然由它的 fixture 决定（`init_db()` 是幂等的）。
    """
    from app.db import init_db

    init_db()
    yield


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """每个用例面前进程级的限流账清零。

    为什么必须有：登录与建号的计数住在**进程**里，键是"来源 IP + 用户名/管理员 id"。
    整个测试会话里 TestClient 的来源永远是同一个值，`admin` 也永远是同一个 id，
    所以不清就会跨用例累加——表现是跑到后面随机冒出一堆 429，而且**红在哪个用例取决于
    跑了哪些用例**（与 `_fresh_gate` 那一条同族：进程级状态必须显式交接，不然就是串味）。
    要测"连续尝试会被拒"那条性质，请在**单个用例内**制造那几次尝试
    （见 `tests/test_rate_limit.py`）。
    """
    from app import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()
