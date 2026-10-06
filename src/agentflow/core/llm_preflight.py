"""LLM 预检：把"这个型号能不能在本项目里用"变成一次可复跑的实测，而不是靠人猜。

为什么要有这一层（2026-10-06 实测背景）：同一台网关上 10 个型号的行为差得很远——
7 个开箱出纯 JSON，2 个默认档交白卷（配 `thinking: disabled` 才正常），1 个反过来
（关了思考改吐散文，破了输出契约）。这些只能实测，写在文档里会立刻过期。

三条设计决定：

1. **探针走引擎自己的客户端**（`OpenAILLM.complete`），不走裸 urllib。要判的是
   "引擎能不能从这里取出它要的正文"，不是"HTTP 是否 200"——裸探针会漏掉 #42 那类
   形状问题，因为它自己解析自己那套容错。
2. **判据写成表**：`usable / usable_if_thinking_disabled / do_not_disable_thinking /
   unusable / unprobed`。每一档都有唯一的成因路径，报错时要能点名是哪一格。
3. **缓存里绝不存凭据**，也不存正文——只存形状（长度、finish_reason、是否纯 JSON、
   耗时）。缓存是给别人看的证据，正文与 key 都不是证据应该带走的东西。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agentflow.core.llm import EmptyContentError, LLMError, extract_json  # 与运行时同一个取正文口径

PROBE_VERSION = 1
PROBE_PROMPT = 'Return JSON only, no prose, no code fence: {"items": ["a", "b", "c"]}'
PROBE_SYSTEM = "You are a JSON API."
DEFAULT_MAX_TOKENS = 300
DEFAULT_FRESH_HOURS = 24

VERDICTS = ("usable", "usable_if_thinking_disabled", "do_not_disable_thinking", "unusable", "unprobed")


def list_models(base_url: str, api_key: str, timeout: int = 30) -> list[str]:
    """`GET {base_url}/models` 是唯一权威的型号口径（页面列什么不等于 key 能用什么）。

    只返回列表，不猜可用性；失败抛 LLMError，让调用方决定是中止还是降级到手工清单。
    """
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        raise LLMError(f"GET {url} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}") from exc
    except Exception as exc:  # noqa: BLE001 - 预检要把错误原样说出去
        raise LLMError(f"GET {url} 失败：{type(exc).__name__}: {str(exc)[:200]}") from exc
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise LLMError(f"GET {url} 返回形状不认识（顶层键：{sorted(data) if isinstance(data, dict) else type(data).__name__}）")
    ids = [str(item.get("id") or item.get("name")) for item in items if isinstance(item, (dict, str))]
    return sorted({item for item in ids if item and item != "None"})


def probe(client: Any, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    """打一次真实调用，按**引擎能否取出可用正文**记分，不记正文本身。"""
    started = time.monotonic()
    record: dict[str, Any] = {"thinking": client.thinking, "max_tokens": max_tokens}
    try:
        text = client.complete(PROBE_SYSTEM, [{"role": "user", "content": PROBE_PROMPT}], max_tokens=max_tokens)
    except EmptyContentError as exc:
        detail = getattr(exc, "detail", {}) or {}
        record.update(
            {
                "ok": False,
                "seconds": round(time.monotonic() - started, 2),
                "error": "empty_content",
                "finish_reason": detail.get("finish_reason"),
                "content_kind": detail.get("content_kind"),
                "reasoning_chars": detail.get("reasoning_chars"),
            }
        )
        return record
    except LLMError as exc:
        record.update(
            {"ok": False, "seconds": round(time.monotonic() - started, 2), "error": "llm_error", "message": str(exc)[:200]}
        )
        return record
    except Exception as exc:  # noqa: BLE001 - 裸异常本身就是要抓的形状问题（见 #42 的教训）
        record.update(
            {
                "ok": False,
                "seconds": round(time.monotonic() - started, 2),
                "error": f"uncaught_{type(exc).__name__}",
                "message": str(exc)[:200],
            }
        )
        return record
    parsed = _shape_of(text)
    record.update(
        {
            # ok 用运行时那把尺（extract_json）：引擎真取得出来就算能用。strict 只作质量注记
            # ——把"整段就是 JSON"当可用判据会比运行时更严，那种红是假红。
            "ok": parsed["extractable"],
            "extractable_json": parsed["extractable"],
            "strict_json": parsed["strict"],
            "seconds": round(time.monotonic() - started, 2),
            "chars": len(text or ""),
            "error": None if parsed["extractable"] else "no_json_extracted",
        }
    )
    return record


def _shape_of(text: str) -> dict[str, bool]:
    """两种口径一次量出来：整段是不是 JSON（strict），与引擎能不能取出 JSON（extractable）。

    为什么两把都要留：2026-10-06 我先用 strict 判了 `glm-5.3` 关思考后"破 JSON 契约"，
    而运行时用的是 `core.llm.extract_json`（能容忍围栏与散文包裹）——只用 strict 就是
    量具比判据更严，会造出系统其实没坏的假红。结论必须按运行时那把尺下。
    """
    stripped = (text or "").strip()
    strict = False
    try:
        json.loads(stripped)
        strict = True
    except Exception:  # noqa: BLE001 - 解析不了就是"整段不是 JSON"，不必分哪一种
        pass
    extractable = False
    try:
        extracted = extract_json(stripped)
        extractable = isinstance(extracted, (dict, list))
    except Exception:  # noqa: BLE001 - 取不出 JSON 就是不可用，让探针把它记成一格
        extractable = False
    return {"strict": strict, "extractable": extractable}


def classify(levels: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """把两档探结果合成一句可行动的结论。判据顺序是写死的，改顺序就是改语义。"""
    default, disabled = levels.get("default"), levels.get("disabled")
    if default is None:
        return "unprobed", "默认档没探过，无法给出结论"
    if default["ok"]:
        if disabled is not None and not disabled["ok"]:
            return (
                "do_not_disable_thinking",
                f"默认档正常，关思考后取不出 JSON（{disabled.get('chars', 0)} 字，error={disabled.get('error')}）——这台网关上别关",
            )
        return "usable", "默认档就能出可用正文"
    if disabled is not None and disabled["ok"]:
        return (
            "usable_if_thinking_disabled",
            f"默认档 {default.get('error')}（finish_reason={default.get('finish_reason')}、"
            f"reasoning {default.get('reasoning_chars')} 字），配 llm.thinking: disabled 后 {disabled['seconds']}s 正常",
        )
    reason = default.get("message") or default.get("error") or "未知"
    return "unusable", f"两档都不行：默认档 {str(reason)[:120]}"


def host_of(base_url: str) -> str:
    """缓存按主机名归组：换 endpoint 就是一次新的预检，不能拿别的站的结果来放行。"""
    return (base_url or "").split("//")[-1].split("/")[0].strip().lower()


def build_report(base_url: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "probe_version": PROBE_VERSION,
        "generated_at": int(time.time()),
        "generated_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_host": host_of(base_url),
        "prompt_chars": len(PROBE_PROMPT),
        "max_tokens": DEFAULT_MAX_TOKENS,
        "models": entries,
    }


def write_cache(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def is_fresh(report: dict[str, Any], hours: int = DEFAULT_FRESH_HOURS) -> bool:
    generated = report.get("generated_at")
    if not isinstance(generated, (int, float)):
        return False
    return (time.time() - generated) <= hours * 3600


def lookup(report: dict[str, Any] | None, base_url: str, model: str) -> tuple[str, str]:
    """在缓存里查一个型号的结论。查不到一律 `unprobed`——**没探过不等于能用，也不等于不能用**。"""
    if not report or report.get("probe_version") != PROBE_VERSION:
        return "unprobed", "没有预检缓存（或缓存来自旧版探针）"
    if report.get("base_host") != host_of(base_url):
        return "unprobed", f"缓存属于 {report.get('base_host')}，与当前端点 {host_of(base_url)} 不是同一台"
    for entry in report.get("models") or []:
        if str(entry.get("model")) == model:
            return str(entry.get("verdict") or "unprobed"), str(entry.get("note") or "")
    return "unprobed", f"当前端点的模型清单里没有 {model}"


def gate_message(base_url: str, model: str, report: dict[str, Any] | None) -> tuple[str, str]:
    """跑批前的那道判断：(要不要中止, 给运维看的话)。

    中止只有一种情况——**探过且结论是 unusable**。`unprobed` 与 `usable_*` 都放行，
    但把结论打在开头：没探过就拒绝启动，等于让预检缓存变成新的隐性前置条件；
    而"缓存说这个型号要关思考、配置里没关"这种必炸的组合，交给一次真实调用去撞
    也要能撞出可读的错（#42 之后才谈得上）。
    """
    verdict, note = lookup(report, base_url, model)
    if verdict == "unprobed" and (report is None or not is_fresh(report)):
        return "", f"预检：{model} @ {host_of(base_url)} 无新鲜缓存——建议先 `python scripts/preflight_llm.py`"
    if verdict == "unusable":
        return f"预检判死：{model} @ {host_of(base_url)} 不可用（{note}）；已中止，未产生本批调用。", note
    if verdict == "usable_if_thinking_disabled":
        return "", f"预检：{model} 需要 thinking=disabled（{note}）"
    if verdict == "do_not_disable_thinking":
        return "", f"预检：{model} 别关思考（{note}）"
    if verdict == "usable":
        return "", f"预检：{model} 可用"
    return "", f"预检：{model} 无结论（{note}）"
