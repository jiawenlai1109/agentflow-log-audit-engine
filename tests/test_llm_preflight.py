"""G2 型号预检的单测：全部用假客户端，零额度。

这组用例守的是预检这件事本身的可信度：
- 判据必须与运行时同一把尺（`extract_json`），比运行时更严就是造假的"这个型号不能用"；
- 五档结论各有唯一的成因路径，不许互相塌成一档；
- 缓存里不许出现正文，更不许出现凭据；
- `run_eval --mode real` 真的去读了这张表（接线用例，不是只测纯函数）。
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from agentflow.core.llm import EmptyContentError, LLMError
from agentflow.core.llm_preflight import (
    PROBE_VERSION,
    build_report,
    classify,
    gate_message,
    is_fresh,
    list_models,
    lookup,
    probe,
    read_cache,
    write_cache,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeClient:
    """只装 `thinking` 与 `complete`：探针能看到的接口就这么点。"""

    def __init__(self, result, thinking=None):
        self.result = result
        self.thinking = thinking

    def complete(self, system, messages, temperature=0.2, max_tokens=2000):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _level(**kwargs):
    base = {"ok": True, "strict_json": True, "extractable_json": True, "seconds": 1.0, "chars": 26, "error": None}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- 判据：与运行时同一把尺


def test_fenced_json_counts_as_usable_because_runtime_can_extract_it():
    """整段不是 JSON、但 `extract_json` 取得出来 ⇒ 可用。

    这条钉的是我自己犯过的错：第一版探针用 `json.loads(整段)`，把 glm-5.3 关思考后的
    散文裹 JSON 判成"破契约"。运行时能用的东西，量具说不能用，那是量具的假黑。
    """
    record = probe(_FakeClient('```json\n{"items": ["a", "b", "c"]}\n```'))
    assert record["ok"] is True and record["extractable_json"] is True
    assert record["strict_json"] is False, "围栏里的 JSON 不该被算成整段 JSON"


def test_prose_without_json_is_not_usable():
    record = probe(_FakeClient("我觉得这批日志整体风险不高，建议再看一眼。"))
    assert record["ok"] is False and record["error"] == "no_json_extracted"


def test_blank_text_is_not_usable():
    """空串在这里判不可用，与 #42 的运行时判据一致：交白卷不是答案。"""
    assert probe(_FakeClient("   "))["ok"] is False


def test_empty_content_error_is_recorded_with_its_shape():
    client = _FakeClient(
        EmptyContentError("白卷", detail={"finish_reason": "length", "content_kind": "blank", "reasoning_chars": 1106})
    )
    record = probe(client)
    assert record["ok"] is False and record["error"] == "empty_content"
    assert record["content_kind"] == "blank" and record["reasoning_chars"] == 1106


def test_transport_error_does_not_escape_the_probe(monkeypatch):
    """探针自己不许崩：裸异常也要记成一格，否则预检会变成新的事故来源。"""
    record = probe(_FakeClient(LLMError("LLM HTTP 402: quota exhausted")))
    assert record["ok"] is False and record["error"] == "llm_error"
    assert "402" in record["message"]

    record = probe(_FakeClient(RuntimeError("网关把连接断了")))
    assert record["ok"] is False and record["error"] == "uncaught_RuntimeError"


def test_probe_never_stores_the_answer_text():
    """形状可以留档，正文不行——正文里可能有用户数据。"""
    record = probe(_FakeClient('{"secret_column": "10.0.0.5"}'))
    assert "secret_column" not in json.dumps(record), record


# ---------------------------------------------------------------- 五档结论各有成因


def test_classify_both_levels_fine():
    assert classify({"default": _level(), "disabled": _level()})[0] == "usable"


def test_classify_only_works_with_thinking_off():
    verdict, note = classify(
        {"default": _level(ok=False, error="empty_content", content_kind="blank", finish_reason="length", reasoning_chars=1106, chars=0),
         "disabled": _level()}
    )
    assert verdict == "usable_if_thinking_disabled"
    assert "thinking: disabled" in note and "reasoning 1106 字" in note, note


def test_classify_disabling_thinking_breaks_json():
    """只有"关了就取不出 JSON"才叫 do_not_disable_thinking。"""
    verdict, note = classify({"default": _level(), "disabled": _level(ok=False, error="no_json_extracted", chars=646)})
    assert verdict == "do_not_disable_thinking" and "取不出 JSON" in note, note


def test_classify_disabling_thinking_only_wraps_json_is_still_usable():
    """散文裹 JSON 在运行时取得出来 ⇒ 不能判成"别关思考"（我 10-06 下早了的那条）。"""
    verdict, _ = classify({"default": _level(), "disabled": _level(ok=True, strict_json=False, chars=329)})
    assert verdict == "usable"


def test_classify_both_broken_is_unusable_and_quotes_the_reason():
    verdict, note = classify({"default": _level(ok=False, error="llm_error", message="LLM HTTP 402: quota"), "disabled": None})
    assert verdict == "unusable" and "402" in note, note


def test_classify_without_default_level_is_unprobed():
    assert classify({"disabled": _level()})[0] == "unprobed"


# ---------------------------------------------------------------- 缓存与主机绑定


def test_cache_roundtrip_and_freshness(tmp_path):
    report = build_report("https://gw.test/v1", [{"model": "m1", "verdict": "usable", "note": "ok", "levels": {}}])
    path = write_cache(report, tmp_path / "sub" / "llm_preflight.json")
    assert read_cache(path) == report
    assert is_fresh(report) and not is_fresh({"generated_at": time.time() - 100 * 3600})


def test_cache_holds_only_the_host_not_the_endpoint_secret(tmp_path):
    """缓存要能被前端与跑批共用，所以它只该留下主机名与形状。

    完整的 base_url 里可能带 token/query，正文与凭据一概不进缓存；网关错误正文那一路
    由 `OpenAILLM._mask` 在出口处抹（`test_llm_response_shape.py` 两条各盖一半）。
    """
    report = build_report("https://gw.test/v1?token=leaky", [{"model": "m1", "verdict": "usable", "note": "ok", "levels": {}}])
    text = json.dumps(report, ensure_ascii=False)
    assert report["base_host"] == "gw.test"
    assert "leaky" not in text and "https://gw.test" not in text, text
    assert "levels" in text  # 形状留着才是证据


def test_lookup_is_bound_to_host_and_probe_version():
    report = build_report("https://a.test/v1", [{"model": "m1", "verdict": "usable", "note": "", "levels": {}}])
    assert lookup(report, "https://a.test/v1", "m1")[0] == "usable"
    assert lookup(report, "https://b.test/v1", "m1")[0] == "unprobed", "换端点就不能拿别的站的结论放行"
    assert lookup(report, "https://a.test/v1", "other")[0] == "unprobed"
    stale = {**report, "probe_version": PROBE_VERSION + 1}
    assert lookup(stale, "https://a.test/v1", "m1")[0] == "unprobed", "旧版/新版探针的结论不可互用"
    assert lookup(None, "https://a.test/v1", "m1")[0] == "unprobed"


# ---------------------------------------------------------------- 跑批那道闸门的语义


def test_gate_blocks_only_a_probed_dead_model():
    report = build_report(
        "https://a.test/v1",
        [
            {"model": "dead", "verdict": "unusable", "note": "两档都不行", "levels": {}},
            {"model": "needs-off", "verdict": "usable_if_thinking_disabled", "note": "要关", "levels": {}},
        ],
    )
    abort, _ = gate_message("https://a.test/v1", "dead", report)
    assert "判死" in abort and "未产生本批调用" in abort, abort
    abort, note = gate_message("https://a.test/v1", "needs-off", report)
    assert abort == "" and "需要 thinking=disabled" in note, note
    # 没缓存 ⇒ 只提示，不拦：把"记得跑预检"变成硬前置，预检自己就成了新的断点
    abort, note = gate_message("https://a.test/v1", "unprobed-model", None)
    assert abort == "" and "无新鲜缓存" in note, note


def test_list_models_reads_the_authoritative_endpoint(monkeypatch):
    """型号清单以 `GET {base}/models` 为准；形状不认识就报错，不猜。"""
    import urllib.request

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": "b-model"}]}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Resp())
    assert list_models("https://a.test/v1", "k") == ["a-model", "b-model"]

    class _Bad:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"result": "weird shape"}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Bad())
    with pytest.raises(LLMError):
        list_models("https://a.test/v1", "k")


# ---------------------------------------------------------------- 接线：run_eval 真的读了这张表


def _load_run_eval():
    spec = importlib.util.spec_from_file_location("run_eval_preflight_wiring", PROJECT_ROOT / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_eval_real_mode_consults_the_capability_cache(tmp_path, monkeypatch):
    """这条测的不是 `gate_message` 的纯逻辑，而是"跑批那条路上有人去读了缓存"。

    预检函数写得再对，没接上线就等于没有——本项目已经为这句话交过学费（#40、每跑自检）。
    """
    run_eval = _load_run_eval()
    cache = tmp_path / ".appdata" / "llm_preflight.json"
    report = build_report("https://a.test/v1", [{"model": "m1", "verdict": "unusable", "note": "两档都不行", "levels": {}}])
    write_cache(report, cache)

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://a.test/v1")
    monkeypatch.setenv("LLM_MODEL", "m1")
    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("agentflow.core.llm.OpenAILLM.complete", lambda self, *a, **kw: "ok")

    abort, notices = run_eval.preflight_credentials("real")
    assert "判死" in abort, (abort, notices)
    assert any("两档都不行" in line for line in notices), notices

    # mock 模式一个字节都不该碰这套东西
    assert run_eval.preflight_credentials("mock") == ("", [])


def test_blank_ping_is_not_reported_as_a_credential_failure(tmp_path, monkeypatch):
    """2026-10-06 实测翻过的车：ping 用 8 个 token，思考档把预算全花在 reasoning 上
    ⇒ 交白卷 ⇒ 被报成"凭据预检失败"，整批 real 根本没开始跑。

    凭据预检只管"连不连得上、有没有权限"；白卷是**能力形状**，必须放行并把原因原样
    打在开头，否则这条闸门会把好端端的端点判死。
    """
    run_eval = _load_run_eval()
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://a.test/v1")
    monkeypatch.setenv("LLM_MODEL", "thinker")
    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path)  # 没有预检缓存 ⇒ 只出提示

    def blank(self, *args, **kwargs):
        raise EmptyContentError(
            "白卷",
            detail={"content_kind": "null", "finish_reason": "length", "reasoning_chars": 38, "agent": "unknown"},
        )

    monkeypatch.setattr("agentflow.core.llm.OpenAILLM.complete", blank)
    abort, notices = run_eval.preflight_credentials("real")
    assert abort == "", f"白卷被当成凭据失败拦下了：{abort}"
    assert any("能力形状" in line and "reasoning 38 字" in line for line in notices), notices
