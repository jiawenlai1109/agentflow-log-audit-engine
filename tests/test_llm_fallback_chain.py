"""G3 型号降级链。全程假传输，零额度。

这条线最容易被写坏的两个地方，用例一一对着钉：

1. **该不该换型号**——判据必须是错误的**类型/状态码**，不是错误文案。文案改一个字，
   按文案匹配的判据就静默失效，而失效的表现是"少降一次级"，没人会当回事。
2. **换了型号要说出来**——降级事件与"本次实际服务过的型号集合"必须落进产物。
   否则换个型号变绿，会被读成代码变好（I3 归因的命门）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib import error as urlerror

import pytest

from agentflow.core.budget import BudgetCounter
from agentflow.core.llm import (
    EmptyContentError,
    LLMError,
    LLMHTTPError,
    LLMTransportError,
    OpenAILLM,
)
from agentflow.pipeline import _agent_llm, run_analysis

PRIMARY = "primary-model"
BACKUP = "backup-model"
SECRET = "sk-chain-secret-never-echo"


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _text_payload(text: str = "print(1)") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


def _blank_eaten_payload() -> dict[str, Any]:
    """思考吃满预算的白卷签名（content 空串或 null 都算）。"""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": "", "reasoning_content": "想想想" * 6},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 300},
    }


def install_by_model(monkeypatch, responses: dict[str, Any]) -> list[dict[str, Any]]:
    """按请求体里的 `model` 回放不同响应；值是 dict 就回放 JSON，是 Exception 就抛它。"""
    sent: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        outcome = responses.get(payload["model"])
        if isinstance(outcome, Exception):
            raise outcome
        body = json.dumps(outcome if outcome is not None else _text_payload()).encode("utf-8")
        return _Response(body)

    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", fake_urlopen)
    return sent


def _client(fallbacks: list[str] | None = None, **kwargs: Any) -> OpenAILLM:
    client = OpenAILLM(
        api_key=SECRET,
        base_url="https://llm.test/v1",
        model=PRIMARY,
        max_retries=0,
        fallback_models=fallbacks if fallbacks is not None else [],
        **kwargs,
    )
    client.budget = BudgetCounter(limit=30)
    return client


def _http_error(code: int) -> urlerror.HTTPError:
    return urlerror.HTTPError(
        "https://llm.test/v1/chat/completions", code, "gateway says no", None, io.BytesIO(b"upstream unhappy")
    )


# ---------------------------------------------------------------- 默认关：一个字节都不多烧


def test_disabled_by_default_costs_exactly_one_call(monkeypatch):
    sent = install_by_model(monkeypatch, {PRIMARY: _http_error(503)})
    client = _client()
    with pytest.raises(LLMError):
        client.complete("你是数据工程师", [{"role": "user", "content": "x"}])
    assert [call["model"] for call in sent] == [PRIMARY], sent
    assert client.budget.fallbacks == []


# ---------------------------------------------------------------- 该换的三种


@pytest.mark.parametrize(
    "outcome, reason_class",
    [
        (_http_error(503), "LLMHTTPError"),
        (_http_error(429), "LLMHTTPError"),
        (urlerror.URLError("connection reset"), "LLMTransportError"),
        (_blank_eaten_payload(), "EmptyContentError"),
    ],
)
def test_eligible_failures_fall_back_to_the_next_model(monkeypatch, outcome, reason_class):
    """5xx/429 重试用尽、连不上、思考吃满预算的白卷——这三种换型号可能真的有用。"""
    sent = install_by_model(monkeypatch, {PRIMARY: outcome, BACKUP: _text_payload("print(2)")})
    client = _client(fallbacks=[BACKUP])
    assert client.complete("你是数据工程师", [{"role": "user", "content": "x"}]) == "print(2)"
    assert [call["model"] for call in sent] == [PRIMARY, BACKUP], sent
    event = client.budget.fallbacks[0]
    assert event["from_model"] == PRIMARY and event["to_model"] == BACKUP, event
    assert event["reason_class"] == reason_class, event
    assert event["tried"] == [PRIMARY], event


# ---------------------------------------------------------------- 不该换的那些


@pytest.mark.parametrize(
    "outcome, label",
    [
        (_http_error(401), "没权限"),
        (_http_error(403), "没权限"),
        (_http_error(404), "没这个端点"),
        (_http_error(400), "请求本身不被接受"),
    ],
)
def test_authorization_and_routing_failures_never_fall_back(monkeypatch, outcome, label):
    """换个型号也一样没权限/一样没这个路径——降级只会多烧一次钱、并把真因洗白。"""
    sent = install_by_model(monkeypatch, {PRIMARY: outcome, BACKUP: _text_payload("print(2)")})
    client = _client(fallbacks=[BACKUP])
    with pytest.raises(LLMError):
        client.complete("你是评审", [{"role": "user", "content": "x"}])
    assert [call["model"] for call in sent] == [PRIMARY], f"{label} 却降了级：{sent}"
    assert client.budget.fallbacks == []


def test_non_json_gateway_page_never_falls_back(monkeypatch):
    """200 + 配额页：这张页对任何型号都一样，换型号是白烧。"""

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        sent.append(json.loads(request.data.decode("utf-8")))
        return _Response(b"<html>quota exceeded, recharge at https://gateway.test</html>")

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", fake_urlopen)
    client = _client(fallbacks=[BACKUP])
    with pytest.raises(LLMError) as raised:
        client.complete("你是评审", [{"role": "user", "content": "x"}])
    assert "响应不是 JSON" in str(raised.value)
    assert [call["model"] for call in sent] == [PRIMARY], sent


def test_empty_without_the_signature_never_falls_back(monkeypatch):
    """没有 reasoning、也不是 length ⇒ 不是"预算被吃满"，换型号也一样交白卷。"""
    payload = {
        "choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 0},
    }
    sent = install_by_model(monkeypatch, {PRIMARY: payload, BACKUP: _text_payload("print(2)")})
    client = _client(fallbacks=[BACKUP])
    with pytest.raises(EmptyContentError):
        client.complete("你是评审", [{"role": "user", "content": "x"}])
    assert [call["model"] for call in sent] == [PRIMARY], sent


def test_budget_guard_lives_at_the_agent_layer_not_the_chain(monkeypatch):
    """预算是在 Agent 层判的（`BaseAgent.complete` 里 `budget.spend()`），换型号绕不过它。

    这条用例原先写的是"客户端层预算耗尽要 raise"——那是我对代码的错猜：客户端的
    `_spend()` 只在 `complete_structured` 里走。前提改了，要守的东西不变：
    **降级链不许成为绕过预算的后门**。
    """
    sent = install_by_model(monkeypatch, {PRIMARY: _text_payload(), BACKUP: _text_payload()})
    client = _client(fallbacks=[BACKUP])
    client.budget = BudgetCounter(limit=0)
    with pytest.raises(LLMError) as raised:
        client.complete_structured("你是评审", [{"role": "user", "content": "x"}], schema=dict)
    assert "预算耗尽" in str(raised.value)
    assert sent == []


def test_extra_real_requests_are_counted_not_hidden(monkeypatch):
    """降级与提额重试会让真实请求多于逻辑调用——两个口径必须分开记，否则成本隐身。"""
    install_by_model(monkeypatch, {PRIMARY: _http_error(503), BACKUP: _text_payload("print(6)")})
    client = _client(fallbacks=[BACKUP])
    client.complete("你是数据工程师", [{"role": "user", "content": "x"}])
    assert client.budget.used == 0, "逻辑调用由 Agent 层计数，客户端不该动它（两套口径会打架）"
    assert client.budget.http_attempts == 2, client.budget.http_attempts


# ---------------------------------------------------------------- 归因与留痕


def test_models_used_records_the_model_that_actually_served(monkeypatch):
    """跨批次比 real 数字之前，先要用它排除"其实是换了型号"。"""
    install_by_model(monkeypatch, {PRIMARY: _http_error(502), BACKUP: _text_payload("print(3)")})
    client = _client(fallbacks=[BACKUP])
    client.complete("你是数据工程师", [{"role": "user", "content": "x"}])
    assert client.budget.models_used == [BACKUP], client.budget.models_used


def test_fallback_event_masks_the_key(monkeypatch):
    """降级原因里带着网关正文；网关可能回显请求头，所以截断之前先抹凭据。"""
    install_by_model(
        monkeypatch,
        {PRIMARY: urlerror.HTTPError("https://llm.test/v1/chat/completions", 500, "boom", None,
                                     io.BytesIO(f"echo header: Bearer {SECRET}".encode())),
         BACKUP: _text_payload("print(4)")},
    )
    client = _client(fallbacks=[BACKUP])
    client.complete("你是评审", [{"role": "user", "content": "x"}])
    event = client.budget.fallbacks[0]
    assert SECRET not in json.dumps(event, ensure_ascii=False), "降级留痕里带着凭据原文"
    assert "已抹掉的凭据" in event["message"], event


def test_the_chain_never_mutates_the_shared_client(monkeypatch):
    """`llm` 实例是跨线程共享的（并发 3）：降级只能把型号当参数传，不许写回 self。"""
    install_by_model(monkeypatch, {PRIMARY: _http_error(503), BACKUP: _text_payload("print(5)")})
    client = _client(fallbacks=[BACKUP])
    client.complete("你是数据工程师", [{"role": "user", "content": "x"}])
    assert client.model == PRIMARY, "共享客户端的型号被降级串改过"


def test_primary_appearing_in_the_chain_is_not_retried_as_a_fallback(monkeypatch):
    sent = install_by_model(monkeypatch, {PRIMARY: _http_error(503)})
    client = _client(fallbacks=[PRIMARY, BACKUP])
    client.complete("你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=10)
    assert [call["model"] for call in sent] == [PRIMARY, BACKUP], sent


def test_role_clone_carries_the_chain():
    """按角色换模型时把兜底弄丢，等于"换了个更稳的型号，顺手把降级关掉了"。"""
    base = _client(fallbacks=[BACKUP])
    clone = _agent_llm(base, {"model": "other"})
    assert clone.fallback_models == [BACKUP], clone.fallback_models
    assert clone.model == "other"


def test_last_failure_is_raised_when_the_whole_chain_is_bad(monkeypatch):
    install_by_model(monkeypatch, {PRIMARY: _http_error(503), BACKUP: _http_error(503)})
    client = _client(fallbacks=[BACKUP])
    with pytest.raises(LLMHTTPError) as raised:
        client.complete("你是评审", [{"role": "user", "content": "x"}])
    assert raised.value.code == 503
    assert len(client.budget.fallbacks) == 1, "最后一次失败没有下一步可降，不该记成降级"


# ---------------------------------------------------------------- 接线：产物里真看得见


def test_fallback_lands_in_the_artifacts(monkeypatch, tmp_path):
    """只测 `complete()` 不够——留痕要落到 evaluation.json 与 transcript 才算接上线。"""
    install_by_model(monkeypatch, {PRIMARY: _blank_eaten_payload(), BACKUP: _text_payload("print(1)")})
    client = _client(fallbacks=[BACKUP])
    result = run_analysis(
        question="统计各账号的登录失败次数，列出风险最高的账号",
        sources=str(Path(__file__).resolve().parents[1] / "demo" / "data" / "login_auth.csv"),
        mode="real",
        llm=client,
        outputs_root=tmp_path / "outputs",
    )
    outputs = Path(result["outputs_dir"])
    evaluation = json.loads((outputs / "evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["llm_fallbacks"], "降级发生了却没落进 evaluation.json"
    assert evaluation["llm_fallbacks"][0]["from_model"] == PRIMARY
    assert evaluation["llm_fallbacks"][0]["to_model"] == BACKUP
    assert BACKUP in evaluation["models_used"], evaluation["models_used"]
    # 真实请求数必须多于逻辑调用数：少记一次上游请求，就等于让降级在成本口径上隐身
    assert evaluation["llm_http_attempts"] > evaluation["llm_calls"], (
        evaluation["llm_http_attempts"],
        evaluation["llm_calls"],
    )
    events = [
        json.loads(line)
        for line in (outputs / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e for e in events if e.get("event") == "llm_fallback"], "transcript 里查不到这次降级"
