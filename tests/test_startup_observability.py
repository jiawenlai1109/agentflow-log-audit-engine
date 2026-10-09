"""启动读数在**部署形态**下必须真能被读到（P5-3 补的缺陷）。

原来那三行（LLM 闸门 / 入口限流 / 执行形态）写在 `app/main.py` 的 lifespan 里，用 `logger.info`。
P4-3 那轮我以为"接进启动日志"就等于有人能读——2026-10-08 分进程压测的日志把这句话打穿了：
uvicorn 只给它自己那族 logger 配 handler，root 上没有 handler，而 Python 的 `lastResort`
只处理 WARNING 以上。于是部署起来的 `server.log` 里**一行都没有**，只有访问行：

    INFO:     Started server process [30168]
    INFO:     Waiting for application startup.
    使用默认口令 admin/admin 种子账号，…        ← 这条是 WARNING，所以出来了
    INFO:     Application startup complete.

运维因此看不到两个直接决定行为的事：闸门那个数是量来的还是占位值，以及这个进程吃不吃作业。
这一族的通用形状是**"写了但没人读的读数"**，判据只能按"运维实际能看到什么"来写，
所以这里起真的 uvicorn 子进程读它的日志，而不是在测试进程里调 `lifespan`——
在测试里 root logger 早就有 handler（pytest 挂的），那条假出口路径根本走不到。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from app.main import ensure_operator_visible_logging

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_uvicorn(tmp_path: Path, *, web_dispatch: str | None = None, timeout_s: float = 45.0) -> str:
    """真的起一座塔，把它的 stdout+stderr 收进文件，读到"启动完成"就杀掉。

    用文件而不是管道：uvicorn 会持续写访问日志，读管道容易把自己卡住（load_test 用的是同一招）。
    """
    port = _free_port()
    env = dict(os.environ)
    env["APP_DATA_DIR"] = str(tmp_path / "appdata")
    env["OUTPUTS_ROOT"] = str(tmp_path / "outputs")
    env["APP_SECRET"] = "startup-observability-test-secret"
    env["PYTHONIOENCODING"] = "utf-8"
    if web_dispatch is not None:
        env["WEB_DISPATCH"] = web_dispatch
    log_path = tmp_path / "uvicorn.log"
    with log_path.open("w", encoding="utf-8", errors="replace") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "info"],
            cwd=str(ROOT), env=env, stdout=sink, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout_s
        text = ""
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
                if "Application startup complete" in text:
                    break
                time.sleep(0.2)
            # 再多等一下：那三行在 "Application startup complete" **之前**打，但磁盘上的
            # 缓冲不一定已经落全，读完再判比"读到就判"诚实。
            time.sleep(0.4)
            return log_path.read_text(encoding="utf-8", errors="replace")
        finally:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - 杀掉就行，不等它
                pass


def test_the_three_startup_readings_reach_the_operator(tmp_path):
    """默认形态下，uvicorn 的日志里要能看到闸门、限流、执行形态这三行。

    摘掉 `ensure_operator_visible_logging()` 这一句，三行同时消失——这就是那条缺陷的复现路径。
    """
    text = _run_uvicorn(tmp_path)
    assert "Started server process" in text, text[-600:]
    for marker in ("LLM 闸门", "入口限流", "执行形态"):
        assert marker in text, f"{marker} 这行没打出来（运维读不到的读数＝假出口）\n----\n{text[-900:]}"


def test_startup_log_states_whether_this_process_claims_jobs(tmp_path):
    """形态那一行要说清"本进程认领循环 0"：分进程部署下运维第一眼就该看到这个。"""
    text = _run_uvicorn(tmp_path, web_dispatch="off")
    assert "本进程认领循环 0" in text, text[-900:]
    assert "分进程" in text, text[-900:]
    assert "作业会一直停在队列里" in text, text[-900:]


def test_existing_logging_config_is_left_alone():
    """部署方自己配过日志（root 有 handler）时不许再加一个——那会变成每行打两遍。

    这条测的是**不改行为**那一半：`propagate = False` 那种"顺手修好"的写法会把人家的
    handler 也一起关掉，把"看得见"换成"更看不见"。

    前置条件必须**自己摆**（本轮 PD11 因此 MISSED 过一次）：第一版只把"当时 `agentflow` 上
    有没有 handler"当基线，而同一会话里前面某个 `TestClient` 的 lifespan 可能已经给它挂上了
    一份 ⇒ `before` 非空、函数里那句 `if not target.handlers` 永不成立 ⇒ 断言永远成立，
    "有 handler 还硬加"这个缺陷怎么改都绿。那是**空转用例**，与"前置条件是借来的"同族。
    """
    target = logging.getLogger("agentflow")
    root = logging.getLogger()
    saved_target, saved_root = list(target.handlers), list(root.handlers)
    target.handlers.clear()
    root.handlers[:] = [logging.NullHandler()]
    try:
        ensure_operator_visible_logging()
        assert target.handlers == [], "root 已经有 handler，却还是加了自己的 ⇒ 每行打两遍"
        assert target.propagate is True, "不许关掉 propagate：那会让部署方的 handler 收不到记录"
    finally:
        # 进程级状态要显式交接回去（与 `_fresh_gate`、`_fresh_rate_limits` 同一条纪律）：
        # 这个用例动的是全局 logger，不还回去就是给后面的用例留一份"看起来是它们自己慢/吵"的账。
        target.handlers[:] = saved_target
        root.handlers[:] = saved_root
