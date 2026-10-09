"""P6-3：链路标识交给沙箱子进程，并且念给运维听。

这一片的范围比规划里写的小——摸完线路才发现，MCP 的审计与 LLM 闸门的排队事件在 P6-2
"转录/事件各盖一次章"之后**自动带上**了链路标识，不需要额外接线。真正还缺的只有两件：

1. **子进程那一侧看不见链路**。`execute_python` 跑在独立进程里，它 print 出来的东西、
   它崩在哪一行，将来要能对上"是哪次提交跑的"。交接走环境变量 `TRACE_ID`。
   这里有一条安全边界要写清：`LocalBackend.ALLOWED_ENV_KEYS` 那份白名单**一个字都没动**——
   值来自本次运行的执行上下文，不是从宿主的 `os.environ` 继承的。把 `TRACE_ID` 加进那份
   白名单，表现是"宿主上随便谁设的一个串，被喂给每一次沙箱运行"。
2. **运维日志行念不出链路**。作业失败那一条 warning 原来只有 `job_id`；"凭一个 id 查回"
   如果只能查库、不能 grep 日志，运维在事故现场还是慢一步。

还有一条**决定**要钉住：链路标识**不外传给第三方 server**（见 `tests/test_mcp.py` 里那条
"这一串停在本地"的用例）。它是内部拓扑的一个可推测面，交给外部进程没有收益。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from agentflow.core import trace
from agentflow.core.executor import LocalBackend

CHAIN = "p63sandboxchain00000000000000001"
LOGIN = Path(__file__).resolve().parents[1] / "demo" / "data" / "login_auth.csv"


def _echo_chain(backend: LocalBackend, tmp_path: Path) -> str:
    code = "import os; print(os.environ.get('TRACE_ID', 'ABSENT'))"
    outcome = backend.execute(code, work_dir=tmp_path / "task", timeout=60)
    assert outcome.success, outcome.stderr
    return outcome.stdout.strip()


# ---------------------------------------------------------------- 沙箱子进程


def test_the_sandbox_sees_the_same_chain(tmp_path):
    """绑定了链路 ⇒ 子进程里读到的就是那一个串（真起进程，不是查 `_build_env` 的字典）。"""
    with trace.bind(CHAIN):
        assert _echo_chain(LocalBackend(), tmp_path) == CHAIN


def test_an_unbound_run_leaves_the_key_absent_rather_than_empty(tmp_path):
    """没链路时那一格在子进程里**根本不存在**，不是空串。

    与转录/`evaluation.json` 同一口径：缺席读起来是"这条路本来就没有链路"，
    空串读起来像"有链路但是空的"，两种意思在事故现场会被查成两件不同的事。
    """
    assert trace.ENV_KEY not in LocalBackend()._build_env({})
    assert _echo_chain(LocalBackend(), tmp_path / "unbound") == "ABSENT"


def test_the_runs_own_chain_wins_over_what_a_caller_passed(tmp_path):
    """调用方在 `env=` 里塞一个 `TRACE_ID` 不算数——本次运行的上下文才是那一条权威。

    不守这条的话，"这个沙箱跑在哪条链路上"就有两个答案（一个来自上下文、一个来自调用方
    传的参数），而调用方那一侧是各角色拼出来的字典。
    """
    with trace.bind(CHAIN):
        outcome = LocalBackend().execute(
            "import os; print(os.environ.get('TRACE_ID', 'ABSENT'))",
            work_dir=tmp_path / "spoof",
            env={trace.ENV_KEY: "someone-elses-chain"},
            timeout=60,
        )
    assert outcome.success, outcome.stderr
    assert outcome.stdout.strip() == CHAIN


def test_a_caller_cannot_fabricate_a_chain_when_there_is_none(tmp_path):
    """没有链路时也不许调用方塞一条进来：那会凭空造出一条查不回来的链路。

    与上一条合起来才是完整的一句话——**这一格只由执行侧的上下文决定**：
    绑了就是我那条，没绑就是没有。只写"绑定时我优先"的那一半，
    剩下的半个口子正好是"伪造一条没人能反查的串"最省事的形状。
    """
    assert trace.current() is None, "前置条件要自己摆干净：这条用例必须在没有链路的时候跑"
    outcome = LocalBackend().execute(
        "import os; print(os.environ.get('TRACE_ID', 'ABSENT'))",
        work_dir=tmp_path / "fabricated",
        env={trace.ENV_KEY: "made-up-chain"},
        timeout=60,
    )
    assert outcome.success, outcome.stderr
    assert outcome.stdout.strip() == "ABSENT"


def test_the_host_environment_is_still_not_a_source_of_the_chain(tmp_path, monkeypatch):
    """宿主上恰好有一个 `TRACE_ID` 也**不许**被继承进沙箱。

    这条盯的是"顺手把 `TRACE_ID` 加进 `ALLOWED_ENV_KEYS`"那种改法：加进去之后，
    谁在机器上设过这个变量，每一次沙箱运行都会带着同一个串，于是"凭 trace 查回一次提交"
    在整个部署上变成一句谎——而那份白名单的注释写的正是"不传任何密钥"。
    """
    monkeypatch.setenv(trace.ENV_KEY, "host-level-value")
    env = LocalBackend()._build_env({})
    assert env.get(trace.ENV_KEY) != "host-level-value"
    assert trace.ENV_KEY not in LocalBackend.ALLOWED_ENV_KEYS
    assert _echo_chain(LocalBackend(), tmp_path / "host") == "ABSENT"


# ---------------------------------------------------------------- 运维看得见的日志


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app.db import init_db

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setenv("WEB_DISPATCH", "off")  # 这一组自己认领，不要让后台循环抢走
    init_db()
    return tmp_path


def _claim_row(job_id: str, chain: str | None, tmp_path: Path) -> dict:
    """摆一条可认领的作业行，然后**不去跑它**，只把认领结果交出去。"""
    from app import queueing
    from app.db import query_one

    user = query_one("SELECT * FROM users WHERE username = 'admin'")
    assert user, "测试库里没有 admin，前置条件没摆出来"
    spec = {
        "question": "失败登录次数最高的用户是谁？",
        "mode": "mock",
        "session_id": None,
        "pack": None,
        "mcp_approvals": {},
        "run_origin": {},
        "source_ref": f"path:{LOGIN}",
        "trace_id": chain,
    }
    queueing.accept(
        job_id=job_id,
        user_id=int(user["id"]),
        org_id=int(_primary_org(user)),
        question=spec["question"],
        mode="mock",
        session_id=None,
        pack=None,
        spec=spec,
        trace_id=chain,
    )
    claimed = queueing.claim("p63-test-worker")
    assert claimed and claimed["job_id"] == job_id, "没认领到这条作业"
    return claimed


def _primary_org(user) -> int:
    from app import access

    return access.primary_org(user)


LOGIN = Path(__file__).resolve().parents[1] / "demo" / "data" / "login_auth.csv"


def test_the_failure_log_line_names_the_chain(env, monkeypatch, caplog):
    """作业失败那行 warning 要把链路念出来——运维在现场 grep 的就是这一个串。

    `run_analysis` 在 `app.runner` 里是**模块级**导入的（那一份注释就是为了留一个可替换的缝隙），
    所以这里在缝隙上 stub 一次抛错，测的是 `execute_job` 的日志行为，不是引擎会不会崩。
    """
    import app.runner as runner
    from app import eventlog
    from app.jobs import JobManager

    def boom(**kwargs):
        raise RuntimeError("量具故意抛的错")

    monkeypatch.setattr(runner, "run_analysis", boom)
    claimed = _claim_row("job_p63_log", CHAIN, env)

    with caplog.at_level("WARNING", logger="agentflow.worker"):
        status = runner.execute_job(claimed, JobManager(sink=eventlog.append_event))

    assert status == "failed"
    line = next((rec.getMessage() for rec in caplog.records if "失败" in rec.getMessage()), "")
    assert f"trace={CHAIN}" in line, line
    # 串要完整念出来，不是"有 trace"这种废读数；也不要被截断成对不上号的半截
    assert CHAIN in line


def test_a_job_with_no_chain_reads_as_a_dash_not_a_stray_value(env, monkeypatch, caplog):
    """老作业（P6-1 之前建的）没有链路 ⇒ 日志念 "-"。

    同一条循环上先跑一个有链路的、再跑一个没链路的：第二个要是把第一个的值留在身上，
    "这两次运行是同一条链路"就成了日志里的假关联。断言的是**两条各自念什么**，
    不是"第二条没炸"。
    """
    import app.runner as runner
    from app import eventlog
    from app.jobs import JobManager

    def boom(**kwargs):
        raise RuntimeError("量具故意抛的错")

    monkeypatch.setattr(runner, "run_analysis", boom)

    with caplog.at_level("WARNING", logger="agentflow.worker"):
        runner.execute_job(_claim_row("job_p63_with_chain", CHAIN, env), JobManager(sink=eventlog.append_event))
        runner.execute_job(_claim_row("job_p63_no_chain", None, env), JobManager(sink=eventlog.append_event))

    lines = [rec.getMessage() for rec in caplog.records if "失败" in rec.getMessage()]
    assert len(lines) == 2, lines
    assert f"trace={CHAIN}" in lines[0]
    assert "job_p63_no_chain（trace=-）" in lines[1], lines[1]


def test_the_chain_is_not_written_into_the_event_payload_of_an_unbound_run(env, monkeypatch):
    """没绑链路时事件里那一格**缺席**，而不是带着上一条作业的串。

    `_publish` 的盖章读的是执行上下文；这里把上下文清空、直接调它，验的是"不写"这条分支。
    """
    from app import runner as runner_module
    from app.jobs import JobManager

    manager = JobManager()
    captured: list[dict] = []
    manager.publish = lambda job_id, event: captured.append(event)  # 只收不发，测盖章这一格

    token = trace._current.set(None)
    try:
        runner_module._publish(manager, "job_unbound", {"type": "phase", "phase": "plan"})
    finally:
        trace._current.reset(token)

    assert captured, "根本没走到盖章那一步，这条用例是空转"
    assert "trace_id" not in captured[0], captured[0]
def test_the_lease_loss_line_names_the_chain_too(env, monkeypatch, caplog):
    """续租线程里那一行也必须念得出链路——而它**不能**靠读 contextvar 来念。

    这条用例存在的理由是一个很容易写错的版本：在那里改成 `trace.describe()`。
    续租是 `execute_job` 另起的线程，contextvar 不跨线程继承 ⇒ 那一行永远念成 "-"，
    看着像"这条链路丢了"，实际是读的人站错了线程。把间隔压到 50 毫秒、把续租 stub 成
    "租约已被收走"，就能在秒级把这一行测到（否则没人会为了一行日志等 20 秒，
    它就变成"写了但没人验"那一类）。
    """
    import app.runner as runner
    from app import eventlog, queueing
    from app.jobs import JobManager

    def slow_ok(**kwargs):
        time.sleep(0.6)  # 留出至少一轮续租
        return {"status": "success", "run_id": "run_p63_lease"}

    monkeypatch.setattr(runner, "run_analysis", slow_ok)
    monkeypatch.setattr(queueing, "heartbeat", lambda *args, **kwargs: False)
    claimed = _claim_row("job_p63_lease", CHAIN, env)

    with caplog.at_level("WARNING", logger="agentflow.worker"):
        runner.execute_job(claimed, JobManager(sink=eventlog.append_event), lease_heartbeat_s=0.05)

    line = next((rec.getMessage() for rec in caplog.records if "租约被收走" in rec.getMessage()), "")
    assert line, "根本没走到续租失败那一行，这条用例是空转"
    assert f"job_p63_lease（trace={CHAIN}）" in line, line
