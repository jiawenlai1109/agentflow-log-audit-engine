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
    assert "content 不是文本" in text and "finish_reason=length" in text, text
    # 原因要说中要害：有 reasoning 在场就是预算被思考吃满，并给出可行动的下一步
    assert "reasoning_content 16 字" in text and "thinking=disabled" in text, text


def test_empty_content_is_still_empty_text_not_an_error(monkeypatch):
    """空串是"模型什么都没说"，null 是"没有正文这个字段"——两者不许混成一个错误。"""
    install(monkeypatch, _text_payload(""))
    assert _llm().complete("你是评审", [{"role": "user", "content": "看这份报告"}]) == ""


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
            "finish_reason": "length",
            "reasoning_chars": 16,
            "max_tokens": 200,
            "thinking": None,
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
