"""LLM 响应形状的容错（#41）。

现场（2026-10-05 real 模式实测）：思考档模型把 `max_tokens` 花在 reasoning 上时，
`message.content` 返回的是 **null**（不是空串）。客户端原样把 None 交出去，下游
`strip_code_fence` / `extract_json` 在 `.strip()` 上炸成 `AttributeError`——
错误类型与真原因毫无关系，还穿出 DAG 把整条 run 打死，用户看到的分类是 `run_error`。

这一批用例守三件事：空返回必须在客户端就被判成**可诊断的 LLM 错误**；
`thinking` 档位只有显式配置才发得出去（留空 = 请求体一个字节都不变）；
档位值写错要在构造期就拒，不能退成"静默不发"。
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from agentflow.core.llm import LLMError, OpenAILLM
from agentflow.pipeline import _agent_llm, run_analysis


class _Response(io.BytesIO):
    """假装是 urlopen 的返回：可读、可作为上下文管理器使用。"""

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def install(monkeypatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把 `llm.urllib.request.urlopen` 换成回放器，返回被发出的请求体列表。"""
    return install_sequence(monkeypatch, [payload])


def install_sequence(monkeypatch, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按次序回放多个响应——测"提额重试"必须能让第二次调用看到不同形状。"""
    sent: list[dict[str, Any]] = []
    queue = list(payloads)

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - 与被替换函数的形状一致
        sent.append(json.loads(request.data.decode("utf-8")))
        payload = queue.pop(0) if queue else payloads[-1]
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", fake_urlopen)
    return sent


def _null_payload(reasoning: str = "先想一步" * 4, finish: str = "length") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": None, "reasoning_content": reasoning},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 8},
    }


def _llm(**kwargs: Any) -> OpenAILLM:
    return OpenAILLM(api_key="test-key", base_url="https://llm.test/v1", **kwargs)


def _text_payload(text: str = "ok") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


def test_null_content_becomes_a_diagnosable_error(monkeypatch):
    """content=null 必须落成带原因与出路的 LLMError，而不是往下漏一个 None。"""
    install(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": None, "reasoning_content": "先想一步" * 4},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 8},
        },
    )
    llm = _llm()
    with pytest.raises(LLMError) as raised:
        llm.complete("你是数据工程师", [{"role": "user", "content": "算一下"}], max_tokens=8)
    text = str(raised.value)
    assert "没有可用正文" in text and "content=null" in text, text
    assert "finish_reason=length" in text, text
    # 原因要说中要害：有 reasoning 在场就是预算被思考吃满，并给出可行动的下一步
    assert "reasoning_content 16 字" in text and "thinking=disabled" in text, text


def test_empty_content_is_still_empty_text_not_an_error(monkeypatch):
    """空串 + stop + 无 reasoning ⇒ 模型真的什么都没说，交回下游按内容判定。

    这条与下面那条空白正文的用例是一对：区别不在字面值（都是空），而在
    `finish_length + reasoning` 这个签名在不在场。
    """
    install(monkeypatch, _text_payload(""))
    assert _llm().complete("你是评审", [{"role": "user", "content": "看这份报告"}]) == ""


def _blank_eaten_payload(reasoning: str = "先想一步" * 4) -> dict[str, Any]:
    """2026-10-06 在 InkStone 上实测到的 Agents-A1 默认档形状：白卷 + 思考吃满预算。"""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": "", "reasoning_content": reasoning},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 44, "completion_tokens": 300},
    }


def test_blank_content_with_eaten_budget_is_not_a_success(monkeypatch):
    """缺陷 #42：content='' 配 finish_reason=length + reasoning 在场，就是 #41 换了个字面值。

    放行它的后果是 fail-open——Executor 拿到空代码、Inspector 拿到空 JSON，
    报出来的错与真因毫无关系，而留痕与提额重试一次都不会触发。
    """
    install(monkeypatch, _blank_eaten_payload())
    with pytest.raises(LLMError) as raised:
        _llm().complete("你是数据工程师", [{"role": "user", "content": "算一下"}], max_tokens=300)
    text = str(raised.value)
    assert "content=blank" in text and "finish_reason=length" in text, text
    assert "thinking=disabled" in text, "提示要说给出路，不能只说坏了"


def test_blank_eaten_budget_is_retry_eligible(monkeypatch):
    """空白白卷与 null 白卷走同一条救法：签名对就提额重试一次。"""
    sent = install_sequence(monkeypatch, [_blank_eaten_payload(), _text_payload("print(1)")])
    llm = _llm(thinking_budget_retry=True)
    from agentflow.core.budget import BudgetCounter

    llm.budget = BudgetCounter(limit=30)
    assert llm.complete("你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=300) == "print(1)"
    assert [call["max_tokens"] for call in sent] == [300, 600], sent
    assert llm.budget.empty_content[0]["content_kind"] == "blank", llm.budget.empty_content


def test_blank_content_shape_lands_in_the_artifacts(monkeypatch, tmp_path):
    """真实调用点上也要成立：白卷必须落成 llm_error 降级并把 content_kind 记进产物。

    只测 `complete()` 抛不抛不够——#42 的漏法正是"空串往下漏"，一路到 JSON 解析才炸。
    """
    from pathlib import Path

    install(monkeypatch, _blank_eaten_payload())
    result = run_analysis(
        question="统计各账号的登录失败次数，列出风险最高的账号",
        sources=str(Path(__file__).resolve().parents[1] / "demo" / "data" / "login_auth.csv"),
        mode="real",
        llm=_llm(),
        outputs_root=tmp_path / "outputs",
    )
    assert result["status"] == "degraded", result["status"]
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["degraded_reason"] == "llm_error", evaluation["degraded_reason"]
    assert evaluation["llm_empty_content"], "白卷没落进 evaluation.json"
    assert evaluation["llm_empty_content"][0]["content_kind"] == "blank"


def test_missing_choices_keeps_the_structural_error(monkeypatch):
    """响应体整个不对时仍是老那条"结构异常"，不要伪装成空返回那一类。"""
    install(monkeypatch, {"detail": "upstream exploded"})
    with pytest.raises(LLMError) as raised:
        _llm().complete("你是评审", [{"role": "user", "content": "x"}])
    assert "响应结构异常" in str(raised.value)


def test_thinking_is_sent_only_when_configured(monkeypatch):
    """留空 = 请求体一字节不改；配了才发 `thinking: {"type": ...}`。"""
    sent = install(monkeypatch, _text_payload())
    _llm().complete("你是评审", [{"role": "user", "content": "x"}])
    assert "thinking" not in sent[-1], sent[-1]

    _llm(thinking="DISABLED ").complete("你是评审", [{"role": "user", "content": "x"}])
    assert sent[-1]["thinking"] == {"type": "disabled"}, sent[-1]


def test_unknown_thinking_level_is_refused_at_construction():
    """档位写错不能退成"静默不发"——那会让"我明明关了思考"变成查不出来的事。"""
    with pytest.raises(LLMError):
        _llm(thinking="auto")


def test_role_level_overrides_reach_the_clone():
    """按角色覆盖 thinking 是 B 案的落点：克隆时必须带上，否则全部角色共用一个档位。"""
    base = _llm(thinking="disabled")
    assert _agent_llm(base, {"model": "other"}).model == "other"
    assert _agent_llm(base, {"model": "other"}).thinking == "disabled"  # 继承全局
    assert _agent_llm(base, {"thinking": "enabled"}).thinking == "enabled"
    assert _agent_llm(base, {"thinking": "enabled"}).model == base.model  # 没点模型就别换
    assert _agent_llm(base, {}).thinking == "disabled"  # 无覆盖不克隆，仍是同一个客户端


def test_null_content_does_not_crash_through_the_dag(monkeypatch, tmp_path):
    """走真实调用点：空返回要落成 `llm_error` 降级，而不是 `run_error` + AttributeError。

    这条不是只测 `complete()` 抛不抛——② 那片修的是守卫异常，`complete()` 返回 None 这条
    路没人守过，所以它能在 `strip_code_fence` 上崩穿 DAG。数据用主场景的登录审计。
    """
    from pathlib import Path

    install(
        monkeypatch,
        {"choices": [{"message": {"content": None, "reasoning_content": "想想想"}, "finish_reason": "length"}]},
    )
    result = run_analysis(
        question="统计各账号的登录失败次数，列出风险最高的账号",
        sources=str(Path(__file__).resolve().parents[1] / "demo" / "data" / "login_auth.csv"),
        mode="real",
        llm=_llm(),
        outputs_root=tmp_path / "outputs",
    )
    assert result["status"] == "degraded", result["status"]
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["degraded_reason"] == "llm_error", evaluation["degraded_reason"]
    report = (Path(result["outputs_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "NoneType" not in report, "错误正文又变成一句没人看得懂的 AttributeError 了"
    # 空正文的形状必须留在产物里：只有一行报错文本闪过，事后没人能归因是哪一步在烧思考
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["llm_empty_content"], "空正文没落进 evaluation.json"
    assert evaluation["llm_empty_content"][0]["finish_reason"] == "length"
    events = [
        json.loads(line)
        for line in (Path(result["outputs_dir"]) / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e for e in events if e.get("event") == "llm_empty_content"], "transcript 里查不到这次空正文"


# ------------------------------------------------------------------ 预算被思考吃满时的提额重试


def test_retry_off_by_default_costs_one_call(monkeypatch):
    """默认不开重试：多打一次真实调用就是要多花一次钱，这个开关不该由系统替用户做主。"""
    sent = install(monkeypatch, _null_payload())
    with pytest.raises(LLMError):
        _llm().complete("你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=200)
    assert len(sent) == 1, sent


def test_budget_retry_recovers_with_a_larger_envelope(monkeypatch):
    """签名对（length + reasoning 在场）时提额重试一次，并把"靠多大预算救回来"记下来。"""
    install_sequence(monkeypatch, [_null_payload(), _text_payload("print(1)")])
    llm = _llm(thinking_budget_retry=True)
    from agentflow.core.budget import BudgetCounter

    llm.budget = BudgetCounter(limit=30)
    assert llm.complete("你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=200) == "print(1)"
    assert llm.budget.empty_content == [
        {
            "agent": "unknown",
            # 型号进形状细节：降级链启用后，"哪个型号在吃满预算"就是唯一要说清的事
            "model": llm.model,
            "finish_reason": "length",
            "reasoning_chars": 16,
            "max_tokens": 200,
            "thinking": None,
            "content_kind": "null",
            "recovered_with": 400,
        }
    ], llm.budget.empty_content


def test_retry_never_exceeds_the_cap(monkeypatch):
    """封顶是不许突破的：救不回来就报错，不能把用户的额度顶到未知深度。"""
    sent = install_sequence(monkeypatch, [_null_payload(), _null_payload()])
    llm = _llm(thinking_budget_retry=True, max_tokens_cap=256)
    with pytest.raises(LLMError):
        llm.complete("你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=200)
    assert [call["max_tokens"] for call in sent] == [200, 256], sent


def test_structural_emptiness_is_not_worth_retrying(monkeypatch):
    """没有 reasoning、也不是 length ⇒ 不是"预算被吃满"，重试只是白烧一次调用。"""
    sent = install(monkeypatch, _null_payload(reasoning="", finish="stop"))
    llm = _llm(thinking_budget_retry=True)
    with pytest.raises(LLMError):
        llm.complete("你是评审", [{"role": "user", "content": "x"}], max_tokens=200)
    assert len(sent) == 1, sent


def test_clone_carries_every_budget_knob():
    """按角色换模型时，档位与预算旋钮必须跟着走——漏一个就是"换了模型顺手把开关弄丢了"。"""
    base = _llm(thinking="disabled", thinking_budget_retry=True, thinking_budget_factor=3.0, max_tokens_cap=4000)
    clone = _agent_llm(base, {"model": "other"})
    assert (clone.thinking, clone.thinking_budget_retry, clone.thinking_budget_factor, clone.max_tokens_cap) == (
        "disabled",
        True,
        3.0,
        4000,
    )


# ------------------------------------------------------------------ 传输层形状：网关不给 JSON 的时候


def install_raw(monkeypatch, body: str, status: int = 200) -> list[str]:
    """回放一个**不是 JSON** 的 2xx 响应（配额页/登录页/网关错误页），记录被请求的次数。"""
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - 与被替换函数的形状一致
        calls.append(str(request.full_url))
        response = _Response(body.encode("utf-8"))
        response.status = status  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", fake_urlopen)
    return calls


def install_http_error(monkeypatch, code: int, body: str) -> list[str]:
    """回放一个非 2xx（urlopen 抛 HTTPError）。"""
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls.append(str(request.full_url))
        raise urllib.error.HTTPError(request.full_url, code, "gateway says no", None, io.BytesIO(body.encode()))

    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", fake_urlopen)
    return calls


def test_non_json_200_body_becomes_a_diagnosable_error(monkeypatch):
    """200 + HTML：判成写得出原因的 LLMError，而不是让 JSONDecodeError 打穿整条 run。

    执行器只 `except LLMError`（agents/executor.py），裸异常会把一次任务失败升级成
    run_error，用户看到的归因从"网关给的是配额页"变成"未知错误"。
    """
    calls = install_raw(monkeypatch, "<html><body>quota exceeded, please recharge</body></html>")
    with pytest.raises(LLMError) as raised:
        _llm(max_retries=2).complete("你是评审", [{"role": "user", "content": "x"}])
    text = str(raised.value)
    assert "响应不是 JSON" in text and "配额页" in text, text
    assert len(calls) == 1, f"拿到的还是那张页，退避重试只是白烧：{calls}"


def test_non_json_body_never_echoes_the_key(monkeypatch):
    """有的网关把请求头回显在错误页里：正文截断之前必须先抹掉凭据。"""
    install_raw(monkeypatch, "<html>Bearer sk-super-secret-token-value seen in body</html>")
    client = OpenAILLM(api_key="sk-super-secret-token-value", base_url="https://llm.test/v1")
    with pytest.raises(LLMError) as raised:
        client.complete("你是评审", [{"role": "user", "content": "x"}])
    assert "sk-super-secret-token-value" not in str(raised.value), "错误文本里带着凭据原文"
    assert "已抹掉的凭据" in str(raised.value), str(raised.value)


def test_json_array_body_is_structural_not_attribute_error(monkeypatch):
    """顶层是数组也是"结构异常"，不许在 `.get` 上炸成 AttributeError。"""
    install_raw(monkeypatch, "[1, 2, 3]")
    with pytest.raises(LLMError) as raised:
        _llm().complete("你是评审", [{"role": "user", "content": "x"}])
    assert "响应结构异常" in str(raised.value)


def test_404_names_both_causes(monkeypatch):
    """404 在这条线上只有两种来路，都得当场说出来：/v1 段，或该站只提供别的协议。"""
    calls = install_http_error(monkeypatch, 404, "404 page not found")
    with pytest.raises(LLMError) as raised:
        _llm(max_retries=2).complete("你是评审", [{"role": "user", "content": "x"}])
    text = str(raised.value)
    assert "/chat/completions 不存在" in text and "/v1" in text and "Anthropic" in text, text
    assert len(calls) == 1, f"404 换型号不会有用，重试只是白烧：{calls}"


def test_http_error_body_never_echoes_the_key(monkeypatch):
    """非 2xx 的错误正文同样可能回显请求头：抹凭据必须在一处修，两个出口都盖住。

    这条与 `test_non_json_body_never_echoes_the_key` 是同一件事的两半——只盖 200 那条
    等于让 401/404 的正文带着 key 进 transcript 与预检缓存。
    """
    install_http_error(monkeypatch, 401, "unauthorized, header was Bearer sk-echo-me-please")
    client = OpenAILLM(api_key="sk-echo-me-please", base_url="https://llm.test/v1")
    with pytest.raises(LLMError) as raised:
        client.complete("你是评审", [{"role": "user", "content": "x"}])
    assert "sk-echo-me-please" not in str(raised.value), "HTTP 错误正文里带着凭据原文"
    assert "已抹掉的凭据" in str(raised.value), str(raised.value)


# ---------------------------------------------------------------- 片1：思考档与信封必须成对


def test_envelope_is_raised_for_thinking_models(monkeypatch):
    """critic 的 800 装不下它的草稿（实测 reasoning 1452 字）⇒ 抬到下限 2000。"""
    sent = install(monkeypatch, _text_payload())
    _llm(envelope_multiplier=2.0, envelope_floor=2000).complete(
        "你是评审", [{"role": "user", "content": "x"}], max_tokens=800
    )
    assert sent[-1]["max_tokens"] == 2000, sent[-1]


def test_envelope_doubling_applies_to_larger_roles(monkeypatch):
    """executor 的 2000 翻倍成 4000：实测它的草稿要写 3793~4837 字。"""
    sent = install(monkeypatch, _text_payload())
    _llm(envelope_multiplier=2.0, envelope_floor=2000).complete(
        "你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=2000
    )
    assert sent[-1]["max_tokens"] == 4000, sent[-1]


def test_envelope_never_shrinks_and_never_crosses_the_cap(monkeypatch):
    """只在变大的方向走，且不越过封顶：宁可回到角色原值，也不能把某个角色调到比自己还小。"""
    sent = install(monkeypatch, _text_payload())
    _llm(envelope_multiplier=2.0, envelope_floor=2000, max_tokens_cap=3000).complete(
        "你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=2000
    )
    assert sent[-1]["max_tokens"] == 3000, sent[-1]

    sent2 = install(monkeypatch, _text_payload())
    _llm(envelope_multiplier=0.5, envelope_floor=100).complete(
        "你是数据工程师", [{"role": "user", "content": "x"}], max_tokens=2000
    )
    assert sent2[-1]["max_tokens"] == 2000, "倍率配小了也不许把角色的信封削薄"


def test_disabled_thinking_keeps_the_envelope_untouched(monkeypatch):
    """关思考就没有草稿要挤 ⇒ 抬信封纯属多花钱。策略必须成对，不许两个旋钮各转各的。"""
    sent = install(monkeypatch, _text_payload())
    _llm(thinking="disabled", envelope_multiplier=2.0, envelope_floor=2000).complete(
        "你是评审", [{"role": "user", "content": "x"}], max_tokens=800
    )
    assert sent[-1]["max_tokens"] == 800, sent[-1]
    assert sent[-1]["thinking"] == {"type": "disabled"}, sent[-1]


def test_pipeline_defaults_pair_the_thinking_tier_with_a_bigger_envelope(tmp_path, monkeypatch):
    """片1 的接线用例：真实客户端由 pipeline 构造时，信封与重试两个默认必须成对生效。

    只测 `OpenAILLM` 的纯函数逻辑不够——默认值长在 `pipeline._real_llm` 里，
    那里没接上就等于线上还是 mock 时代的信封（正是这批 real 全量 14 次白卷的来路）。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from agentflow.pipeline import _real_llm, llm_policy

    client = _real_llm({"model": "m"})
    assert client.envelope_multiplier == 2.0 and client.envelope_floor == 2000, llm_policy(client)
    assert client.thinking_budget_retry is True, "白卷兜底默认开：显式配 false 才关"

    # 显式写 false 必须是 false——用 `or` 会把"关掉"读成"没配"
    assert _real_llm({"model": "m", "thinking_budget_retry": False}).thinking_budget_retry is False
    # 关思考 ⇒ 不抬信封（成对），也不留一个用不上的倍率
    off = _real_llm({"model": "m", "thinking": "disabled"})
    assert off.envelope_multiplier == 1.0 and off.envelope_floor == 0, llm_policy(off)

    policy = llm_policy(client)
    assert policy["thinking_budget_retry"] is True and policy["model"] == "m", policy


def test_policy_and_clone_travel_together():
    """按角色换型号/换档位时，信封策略必须跟着克隆走，否则那个角色会重新开始交白卷。"""
    base = _llm(envelope_multiplier=2.0, envelope_floor=2000)
    clone = _agent_llm(base, {"model": "other"})
    assert (clone.envelope_multiplier, clone.envelope_floor) == (2.0, 2000)


def test_llm_policy_lands_in_the_artifacts(monkeypatch, tmp_path):
    """策略要落进 evaluation.json：跨批次比较先排掉"换了档位/信封"这个变量。"""
    from pathlib import Path

    install(monkeypatch, _text_payload())
    client = _llm(envelope_multiplier=2.0, envelope_floor=2000)
    result = run_analysis(
        question="统计各账号的登录失败次数，列出风险最高的账号",
        sources=str(Path(__file__).resolve().parents[1] / "demo" / "data" / "login_auth.csv"),
        mode="real",
        llm=client,
        outputs_root=tmp_path / "outputs",
    )
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    policy = evaluation.get("llm_policy") or {}
    assert policy.get("envelope_multiplier") == 2.0, policy
    assert policy.get("model") == client.model, policy
