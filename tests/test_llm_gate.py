"""P4 的闸门：读数、排队形状，以及最重要的一条——**绕不过去**。

这片要防的不是"闸门算错数"，而是"闸门装在一处、别的来路没装"：真调用只有一条出口
（`OpenAILLM._request`），所以判据也必须打在那条出口上——用假 `urlopen` 数"同时在飞几路"，
比读代码确认"应该都过了闸"可靠得多。

`llm_gate_wait` 那条事件走的是**客户端实例上的 `event_sink`**（由 pipeline 注入 run 的
`on_event`，按角色克隆的客户端也带着它）。闸门不去认识 run，也不靠"当前是哪个线程"猜收件人：
登记表那种形状，忘登记一处的症状是事件静默消失，而计数仍然对。
"""

from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from agentflow.core import gate as gate_module
from agentflow.core.gate import (
    ConcurrencyGate,
    LLMGateTimeoutError,
    limit_and_source,
)
from agentflow.core.llm import OpenAILLM


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def make_client(**kwargs: Any) -> OpenAILLM:
    return OpenAILLM(api_key="test-key", base_url="https://upstream.test/v1", model="m1", **kwargs)


def flying_urlopen(state: dict[str, int], hold_s: float = 0.05):
    """假 urlopen：进入时 +1、离开时 -1，中间睡一会儿——这样"同时在飞几路"是可测的量。

    `calls` 单独记：排队排爆那一路**不该发出 HTTP**，只数"同时在飞几路"看不出这一点
    （它一直是 1 也算成功）。
    """

    def fake(request, timeout=None):  # noqa: ANN001 - 与被替换函数的形状一致
        with state["lock"]:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            state["calls"] = state.get("calls", 0) + 1
        try:
            time.sleep(hold_s)
        finally:
            with state["lock"]:
                state["now"] -= 1
        body = {"choices": [{"message": {"content": '{"answer": "ok"}', "reasoning_content": None}, "finish_reason": "stop"}]}
        return _Response(json.dumps(body).encode("utf-8"))

    return fake


def new_state(**extra: Any) -> dict[str, Any]:
    return {"lock": threading.Lock(), "now": 0, "max": 0, **extra}


@pytest.fixture(autouse=True)
def _fresh_gate():
    """闸门是进程级的：不清掉就会把上一个用例的在飞计数串给下一个（模块级单例的老账）。"""
    gate_module._gate = None
    yield
    gate_module._gate = None


# ---------------------------------------------------------------- 绕不过去


def test_the_transport_is_the_only_way_in(monkeypatch):
    """引擎内部多路并发也必须过闸：闸装在出口上，而不是装在调用方自觉上。

    这里同时打开 3 路（对应 `execution.max_concurrency`），闸门上限设成 1。
    判据是"假网关亲眼看到的最同时在飞路数"== 1。把闸门从 `_request` 里摘掉，
    这个数就变成 3——那位点会红，而不是靠读代码确认"应该都过了闸"。
    """
    state = new_state()
    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", flying_urlopen(state))
    gate_module.configure_for_run(1, source="measured")
    client = make_client(timeout=5, max_retries=0)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: client.complete("sys", [{"role": "user", "content": "hi"}]), range(3)))

    assert state["max"] == 1, f"闸门被绕过了：网关同时看到 {state['max']} 路在飞"


def test_limit_from_env_wins_over_everything_else(monkeypatch):
    """运维显式写的数优先，且优先级要能被打断点验证（不是"看起来生效"）。"""
    state = new_state()
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", flying_urlopen(state))
    gate_module._gate = None  # 让 get_gate 走 env 解析这一条路
    client = make_client(timeout=5, max_retries=0)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: client.complete("sys", [{"role": "user", "content": "hi"}]), range(4)))

    assert state["max"] == 2, state


# ---------------------------------------------------------------- 排队与失败形状


def test_waiting_is_visible_and_bounded():
    """槽满时：在飞 = 上限、在等 = 差额；放开之后等待者真的进得来。

    深度是给运维看的——如果只报 `inflight` 不报 `waiting`，"排在外面的人"在读数里就是隐形的，
    而那正是"点了没反应"与"排在第几"两种体验的分界。
    """
    gate = ConcurrencyGate(2, wait_timeout_s=8)
    release = threading.Event()
    entered = []

    def hold(tag: str) -> None:
        with gate.slot():
            entered.append(tag)
            release.wait(8)

    holders = [threading.Thread(target=hold, args=(f"h{i}",)) for i in range(2)]
    for thread in holders:
        thread.start()
    deadline = time.time() + 4
    while time.time() < deadline and gate.snapshot()["inflight"] < 2:
        time.sleep(0.01)
    assert gate.snapshot()["inflight"] == 2, gate.snapshot()

    waiter_errors: list[BaseException] = []

    def guarded() -> None:
        try:
            hold("waiter")
        except BaseException as exc:  # noqa: BLE001 - 带回主线程判定，线程里的异常不许静默
            waiter_errors.append(exc)

    waiter = threading.Thread(target=guarded)
    waiter.start()
    deadline = time.time() + 3
    while time.time() < deadline and gate.snapshot()["waiting"] == 0:
        time.sleep(0.01)
    snapshot = gate.snapshot()
    assert snapshot["waiting"] == 1, snapshot
    assert snapshot["max_inflight"] == 2, snapshot

    release.set()
    for thread in holders + [waiter]:
        thread.join(timeout=10)
    assert not waiter_errors, waiter_errors
    assert "waiter" in entered, entered  # 放开之后真的排进来了，不是永远排不上
    final = gate.snapshot()
    assert final["inflight"] == 0 and final["timeouts"] == 0, final


def test_a_lost_wakeup_costs_the_poll_interval_not_the_whole_timeout():
    """丢一次「叫醒」只许多等一小段，不许把整条超时睡满。

    这条用例在测试里**把 `notify` 换成空操作**，模拟"将来谁改了释放路径忘了叫醒"：
    槽位空了但等待者收不到信号。有 50ms 轮询兜底时它自己醒过来就进得去；没有兜底时
    它要睡到 `wait_timeout_s` 结束。后者不是假设——2026-10-08 的变异复测把 `notify()`
    摘掉之后，这一片测试 **600 秒没返回**：症状是停顿而不是红，那种缺陷最难查，
    所以兜底要有一条自己能红的用例守着，而不是靠变异把它卡出来。
    """
    gate = ConcurrencyGate(1, wait_timeout_s=2.0)
    entered: list[str] = []
    errors: list[BaseException] = []

    def hold(tag: str, hold_s: float) -> None:
        try:
            with gate.slot():
                entered.append(tag)
                time.sleep(hold_s)
        except BaseException as exc:  # noqa: BLE001 - 带回主线程判定
            errors.append(exc)

    holder = threading.Thread(target=hold, args=("holder", 0.3))
    holder.start()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and gate.snapshot()["inflight"] == 0:
        time.sleep(0.005)
    assert "holder" in entered, entered

    waiter = threading.Thread(target=hold, args=("waiter", 0.0))
    waiter.start()
    started = time.monotonic()
    try:
        gate._condition.notify = lambda *args, **kwargs: None  # 就叫不醒
        while time.monotonic() - started < 1.0 and "waiter" not in entered:
            time.sleep(0.01)
    finally:
        gate._condition.notify = threading.Condition.notify.__get__(gate._condition)
        for thread in (holder, waiter):
            thread.join(timeout=8)

    assert not errors, errors
    waited = round(time.monotonic() - started, 3)
    assert "waiter" in entered, f"丢了叫醒就再也进不来（等了 {waited}s）：轮询兜底没生效"
    assert waited < 1.0, f"等待者靠超时才醒（{waited}s）：释放路径的 notify 一旦丢失就是整条超时"


def test_gate_timeout_is_its_own_failure_shape(monkeypatch):
    """排不上必须报成"配额满了"，而且**一次 HTTP 都不许发**。

    后半句才是这条用例的要点：排不上的调用如果被退避循环吃掉，成本读数会多出请求次数、
    延迟读数会多出重试时间，"配额到了"就会被读成"网络不好"——那正是挂账里
    "没跑成不许伪装成合法数值"的同族。
    """
    state = new_state()
    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", flying_urlopen(state, hold_s=0.3))
    gate = ConcurrencyGate(1, source="measured", wait_timeout_s=0.05)
    gate_module._gate = gate
    client = make_client(timeout=5, max_retries=0)

    def holder() -> None:
        # 只调客户端：槽位由 `_request` 自己取。手动 `with gate.slot()` 再调客户端会**嵌套取槽**
        # （闸门不可重入），那时测的就是自己锁死自己，不是排队。
        client.complete("sys", [{"role": "user", "content": "hi"}])

    holder_errors: list[BaseException] = []

    def guarded_holder() -> None:
        try:
            holder()
        except BaseException as exc:  # noqa: BLE001 - 带回主线程：持有者不许自己失败
            holder_errors.append(exc)

    holding = threading.Thread(target=guarded_holder)
    holding.start()
    deadline = time.time() + 2
    while time.time() < deadline and gate.snapshot()["inflight"] == 0:
        time.sleep(0.005)

    with pytest.raises(LLMGateTimeoutError) as refused:
        client.complete("sys", [{"role": "user", "content": "hi"}])
    holding.join(timeout=5)

    assert not holder_errors, holder_errors

    message = str(refused.value)
    assert "上游配额已满" in message, message
    assert "上限来源=measured" in message, "失败原因要带上限的出处，不然运维以为这就是网关公布的配额"
    assert refused.value.gate["limit"] == 1
    assert refused.value.waited_ms >= 0
    assert gate.snapshot()["timeouts"] == 1
    # 只有持有槽位的那一路真的发出去了；排不上的这一路一次都没发
    assert state["calls"] == 1, f"排不上的调用不该发出 HTTP：{state}"
    assert state["max"] == 1, state


def test_wait_over_threshold_emits_one_event(monkeypatch):
    """等过阈值要留痕，且带"等了多久 / 几个在等 / 上限多少 / 上限从哪来"。"""
    state = new_state()
    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", flying_urlopen(state, hold_s=0.25))
    gate_module.configure_for_run(1, source="measured")
    gate = gate_module.get_gate()
    gate.notice_ms = 20.0
    gate.wait_timeout_s = 5.0
    client = make_client(timeout=5, max_retries=0)

    events: list[dict[str, Any]] = []
    holder_errors: list[BaseException] = []

    def holder() -> None:
        try:
            client.complete("sys", [{"role": "user", "content": "hi"}])
        except BaseException as exc:  # noqa: BLE001 - 带回主线程
            holder_errors.append(exc)

    holding = threading.Thread(target=holder)
    holding.start()
    deadline = time.time() + 2
    while time.time() < deadline and gate.snapshot()["inflight"] == 0:
        time.sleep(0.005)

    # 留痕的去处挂在客户端上（谁发调用谁提供出口），不是按线程登记猜出来的
    client.event_sink = events.append
    client.complete("sys", [{"role": "user", "content": "hi"}])
    holding.join(timeout=8)

    assert not holder_errors, holder_errors
    assert len(events) == 1, events
    event = events[0]
    assert event["type"] == "llm_gate_wait"
    assert event["waited_ms"] >= 20, event
    assert event["limit"] == 1 and event["limit_source"] == "measured", event
    assert event["model"] == "m1", event
    # 排队确实发生了：网关同时只看到 1 路，两路一共发了 2 次
    assert state["max"] == 1, state
    assert state["calls"] == 2, state


def test_notice_failure_does_not_steal_a_slot():
    """留痕那条路坏了，真实调用照旧、槽位照旧释放——事件推送永远不是主路。"""
    gate = ConcurrencyGate(1, on_wait=lambda event: (_ for _ in ()).throw(RuntimeError("sink 炸了")), notice_ms=0.0)
    with gate.slot():
        pass
    with gate.slot():
        pass
    assert gate.snapshot()["inflight"] == 0, gate.snapshot()


# ---------------------------------------------------------------- 上限的出处


@pytest.mark.parametrize(
    ("env", "measured", "expected"),
    [
        ({"LLM_MAX_CONCURRENCY": "16"}, None, (16, "env")),
        ({"LLM_MAX_CONCURRENCY": "16"}, 4, (16, "env")),  # 显式写的优先于实测
        ({}, 16, (16, "measured")),
        ({}, None, (gate_module.UNMEASURED_FALLBACK_LIMIT, "unmeasured_fallback")),
        ({}, 0, (gate_module.UNMEASURED_FALLBACK_LIMIT, "unmeasured_fallback")),
        # 写坏的配置不当成"闸门锁死"，也不静默变 1：那是运维以为关了闸、其实把吞吐掐到 1
        ({"LLM_MAX_CONCURRENCY": "0"}, 8, (8, "measured")),
        ({"LLM_MAX_CONCURRENCY": "abc"}, 8, (8, "measured")),
        ({"LLM_MAX_CONCURRENCY": "-3"}, None, (gate_module.UNMEASURED_FALLBACK_LIMIT, "unmeasured_fallback")),
    ],
)
def test_limit_source_is_part_of_the_reading(env, measured, expected):
    """每个默认值都要带着"从哪来"。占位值不许冒充实测——那正是本项目最不该再犯的那类事。"""
    assert limit_and_source(env=env, measured=measured) == expected, (env, measured)


def test_percentiles_over_a_truncated_window_say_so():
    """样本只留最近一段时，读数必须自报窗口与"截过没有"。把窗口说成全程是另一种谎。

    每轮都制造一次**真实等待**（另一个线程占着槽），否则等待时长全是 0，
    这条用例就只测到了计数器，测不到分位数。
    """
    gate = ConcurrencyGate(1, wait_window=3, wait_timeout_s=8)
    for _ in range(10):
        busy = threading.Event()

        def hold() -> None:
            with gate.slot():
                busy.set()
                time.sleep(0.02)  # 占 20ms 就放——够主线程排上一段，又不至于拖慢用例

        holder = threading.Thread(target=hold)
        holder.start()
        busy.wait(4)
        with gate.slot():
            pass
        holder.join(timeout=6)

    snapshot = gate.snapshot()
    assert snapshot["waits"] == 10, snapshot
    assert snapshot["takes"] == 20, snapshot  # 每轮两次取槽，其中只有等待者排过队
    assert snapshot["wait_window_size"] == 3, snapshot
    assert snapshot["wait_window_truncated"] is True, snapshot
    # 均值与最大值是全程算的：窗口截断不许把这两个一起截掉
    assert snapshot["wait_max_ms"] > 0 and snapshot["wait_mean_ms"] > 0, snapshot
    assert snapshot["wait_p95_ms"] <= snapshot["wait_max_ms"], snapshot


def test_nested_acquisition_is_bounded_by_the_timeout_not_infinite():
    """嵌套取槽（上限 1）必须**超时后带原因失败**，不是永久卡住。

    现在的真调用不会嵌套（`_request` 是唯一取槽点），这条下限写出来是为了将来：谁把闸门
    上移到 `complete()` 而 `_request` 里还留着一道，症状就是"配额明明没满，作业却报
    上游配额已满"。到那时，静默死锁与"0.1 秒后说出原因"的差别就是能不能查得动。
    """
    gate = ConcurrencyGate(1, wait_timeout_s=0.1)
    started = time.monotonic()
    with gate.slot():
        with pytest.raises(LLMGateTimeoutError) as nested:
            with gate.slot():
                pass
    assert "上游配额已满" in str(nested.value), nested.value
    assert time.monotonic() - started < 3.0, "等超时之后还不放手，那是死锁不是排队"
    assert gate.snapshot()["inflight"] == 0, gate.snapshot()


def test_snapshot_without_any_call_still_reports_the_source():
    """没建过闸门也要报一个数：SSE 首帧可能早于第一次真实调用，而"没有"不等于"没问题"。"""
    gate_module._gate = None
    snapshot = gate_module.snapshot()
    assert snapshot["inflight"] == 0 and snapshot["waits"] == 0
    assert snapshot["limit_source"] in {"env", "unmeasured_fallback"}, snapshot


# ---------------------------------------------------------------- 接线（Web 与产物）


def test_apply_for_process_follows_env_then_measured_then_placeholder(tmp_path, monkeypatch):
    """开闸那一步的三级解析要能在**应用层**被打断点，而不只是纯函数层。

    中间那级特别值得测：读的是预检缓存，而缓存"属于别台站"或"没量过这个型号"时
    必须退回占位并说明理由——把别台站的 16 路拿来给这台开闸，比没有数更危险。
    """
    import json

    from app import llm_gate as app_gate

    cache = tmp_path / "llm_preflight.json"
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_MAX_CONCURRENCY", raising=False)
    config = {"llm": {"base_url": "https://x.example/v1", "model": "m1"}}

    view = app_gate.apply_for_process(config)
    assert view["limit_source"] == "unmeasured_fallback", view  # 没有缓存 ⇒ 占位，且明说

    cache.write_text(
        json.dumps(
            {
                "probe_version": 1,
                "base_host": "x.example",
                "models": [],
                "concurrency_by_model": {"m1": {"recommended_limit": 16, "waves": []}},
            }
        ),
        encoding="utf-8",
    )
    view = app_gate.apply_for_process(config)
    assert (view["limit"], view["limit_source"]) == (16, "measured"), view

    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "8")
    view = app_gate.apply_for_process(config)
    assert (view["limit"], view["limit_source"]) == (8, "env"), view

    # 缓存是别台站量的：不作数，退回占位并说明
    cache.write_text(
        json.dumps({"probe_version": 1, "base_host": "other.example", "concurrency_by_model": {"m1": {"recommended_limit": 16}}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_MAX_CONCURRENCY", raising=False)
    view = app_gate.apply_for_process(config)
    assert view["limit_source"] == "unmeasured_fallback", view
    assert "不属于当前端点" in view["note"], view


def test_job_payload_and_sse_first_frame_carry_the_gate_view():
    """`queue` 与 `llm_gate` 必须是两个字段：一个是库里的全局事实，一个是本进程的累计量。

    这条测的就是"分字段"这件事本身。合成一个字典，读的人就会把 inflight 当成全局数，
    而那正是 P0 之后我们反复撞的那类错：把"没测过"说成"测过了"。
    """
    from fastapi.testclient import TestClient

    from app.db import execute, query_one
    from app.main import app

    uid = query_one("SELECT id FROM users WHERE username = 'admin'")["id"]
    job_id = "job_gate_view_1"
    execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    execute(
        "INSERT INTO jobs (job_id, user_id, org_id, question, mode, status, progress) "
        "VALUES (?, ?, 0, '闸门读数', 'mock', 'queued', 0)",
        (job_id, uid),
    )
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = f"Bearer {response.json()['token']}"
        payload = client.get(f"/api/jobs/{job_id}").json()

    assert set(payload["queue"]) >= {"workers", "running", "queued"}, payload["queue"]
    gate_view = payload["llm_gate"]
    assert {"limit", "limit_source", "inflight", "waiting", "waits", "timeouts"} <= set(gate_view), gate_view
    assert gate_view["limit"] >= 1 and gate_view["inflight"] == 0, gate_view


def test_cloned_agent_clients_keep_the_event_sink():
    """按角色克隆的客户端要带上事件出口，否则那个角色排队时没人知道。

    这条不是洁癖：`_agent_llm` 是逐字段构造克隆体的，漏一个字段的症状是
    "计数仍然对、事件却没有"——最难查的那类不对称。
    """
    from agentflow.pipeline import _agent_llm

    received: list[dict[str, Any]] = []
    parent = make_client()
    parent.event_sink = received.append
    clone = _agent_llm(parent, {"model": "other-model"})
    assert clone is not parent
    assert clone.event_sink is parent.event_sink, "克隆体丢了 sink：这个角色排队时不会有任何留痕"


def test_evaluation_records_the_gate_reading(tmp_path):
    """闸门读数要进产物。"它生效了"与"它生效过"之间的差别就是一份能翻查的读数。

    mock 模式不打上游，所以等待数天然是 0——这也要能从读数里看出来，
    而不是让"waits=0"被误读成"配额从来没用满"。`limit_source` 就是为此存在的。
    """
    import json

    from agentflow.pipeline import run_analysis

    data = Path(__file__).resolve().parents[1] / "demo" / "data" / "retail_sales.csv"
    result = run_analysis("总销售额是多少？", [str(data)], mode="mock", outputs_root=tmp_path)
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    gate_view = evaluation["llm_gate"]
    assert gate_view["limit"] >= 1 and gate_view["limit_source"] in {"env", "measured", "unmeasured_fallback", "explicit"}
    assert gate_view["takes"] >= 0 and gate_view["inflight"] == 0, gate_view
