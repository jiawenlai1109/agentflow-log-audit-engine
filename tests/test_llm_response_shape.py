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
    sent: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - 与被替换函数的形状一致
        sent.append(json.loads(request.data.decode("utf-8")))
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", fake_urlopen)
    return sent


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
