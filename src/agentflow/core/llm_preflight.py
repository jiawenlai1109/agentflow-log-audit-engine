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
import re
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from agentflow.core.llm import (  # 与运行时同一个取正文口径
    EmptyContentError,
    LLMError,
    LLMHTTPError,
    LLMTransportError,
    extract_json,
)
from agentflow.core.stats import shape  # 与负载压测同一把尺：p95 在两边必须是同一个算法

# `Authorization: Bearer xxx` 与 OpenAI 风格的长 opaque token：两种来路都要抹，
# 判据按形状不按"是不是我那把 key"（缓存备注可能是网关回显的别人的 token）。
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})")
_OPAQUE_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")

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
        # 错误**按类型分格**，不靠读文案：`LLMHTTPError` 自带 code，而"网关回了 429"与
        # "连不上"是两件事——混成一格就没法回答"闸门该定在几路"。文案改一个字就静默失效的
        # 判据，是本项目最不该再犯的那类错（型号降级那条已经吃过一次）。
        kind = "llm_error"
        status = getattr(exc, "code", None)
        if isinstance(exc, LLMHTTPError):
            kind = f"http_{status}"
        elif isinstance(exc, LLMTransportError):
            kind = "transport"
        record.update(
            {
                "ok": False,
                "seconds": round(time.monotonic() - started, 2),
                "error": kind,
                "http_status": status,
                "message": str(exc)[:200],
            }
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


def mask_secrets(text: Any) -> Any:
    """把"像凭据"的片段从任何要落盘或外印的字符串里抹掉。

    为什么在预检层也要做一遍：有些网关把请求头原样回显在错误页里，而那段文本会被当作
    `note` 存进缓存、再被 Web 接口转述出去。`OpenAILLM._mask` 只认得**自己那把 key**，
    缓存里的备注可能带的是别处抄来的 token——所以这里按形状抹，不按已知值抹。
    """
    if not isinstance(text, str):
        return text
    masked = _BEARER_RE.sub(r"\1<已抹掉的凭据>", text)
    return _OPAQUE_TOKEN_RE.sub("<已抹掉的凭据>", masked)


def _mask_report(report: dict[str, Any]) -> dict[str, Any]:
    """落盘前把每个 entry 的 note / 各档的 message 抹一遍。只碰文本，不碰形状字段。"""
    masked = json.loads(json.dumps(report, ensure_ascii=False))
    for entry in masked.get("models") or []:
        if "note" in entry:
            entry["note"] = mask_secrets(entry.get("note"))
        for level in (entry.get("levels") or {}).values():
            if isinstance(level, dict) and "message" in level:
                level["message"] = mask_secrets(level.get("message"))
    return masked


def write_cache(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_mask_report(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


CONCURRENCY_HISTORY_KEEP = 20

# 历史条目留哪几格：**读数 + 判定依据**，不留正文（正文本来就不进缓存），也不留 `history` 本身。
# 后者不是洁癖：第一版把自己塞进自己的取值里，缓存变成循环引用，写盘时 json 递归爆栈。
CONCURRENCY_HISTORY_FIELDS = (
    "measured_at",
    "widths",
    "calls_total",
    "recommended_limit",
    "waves",
    "latency_payout_ratio",
    "latency_payout_floor_seconds",
)


def _reading_only(concurrency: dict[str, Any]) -> dict[str, Any]:
    """取一份读数的**独立副本**，只带形状字段。历史里放的必须是副本，不是容器本身。"""
    return {key: concurrency.get(key) for key in CONCURRENCY_HISTORY_FIELDS if key in concurrency}


def merge_concurrency(cache: Path, base_url: str, concurrency: dict[str, Any]) -> Path:
    """把并发读数并进预检缓存，**不覆盖型号结论，也不覆盖上一次的读数**。

    缓存按主机归组：换 endpoint 就是另一台网关，那台上量出来的干净档数在这台不作数
    （与 `lookup` 那条"缓存属于别台站就判 unprobed"是同一个口径）。
    按型号分开存：不同型号的配额可以完全不同，混成一个"这台站的上限"是假的。

    为什么要留 `history`：2026-10-08 第一版把读数直接盖在旧读数上，于是"闸门默认值从哪来"
    这句在第二次跑之后就查不到出处了。带时间戳的历史才是证据，最后一格就是当前值
    （`history[-1]` 与顶层同源，两处不一致时以能读出来的这份为准）。
    """
    report = read_cache(cache) or {}
    if report.get("probe_version") != PROBE_VERSION or (
        report.get("base_host") and report.get("base_host") != host_of(base_url)
    ):
        report = {
            "probe_version": PROBE_VERSION,
            "base_host": host_of(base_url),
            "models": [],
        }
    by_model = report.get("concurrency_by_model") or {}
    key = str(concurrency.get("model"))
    previous = by_model.get(key) or {}
    history = list(previous.get("history") or [])
    # 兼容"没有 history 的旧缓存"：上一版是直接把读数盖掉的，那份证据只挂在顶层。
    # 按 measured_at 判重，顶层已经是历史最后一格时不重复推进去。
    if previous.get("measured_at") and (
        not history or history[-1].get("measured_at") != previous.get("measured_at")
    ):
        history.append(_reading_only(previous))
    entry = dict(concurrency)
    # 这里放的是**副本**：把 `concurrency` 自己塞进 `concurrency["history"]` 会让缓存变成
    # 循环引用，写盘时 json 递归爆栈（第一版的真 bug，被这条链的用例抓住的）。
    entry["history"] = (history + [_reading_only(concurrency)])[-CONCURRENCY_HISTORY_KEEP:]
    by_model[key] = entry
    report["concurrency_by_model"] = by_model
    report["generated_at"] = int(time.time())
    report["generated_at_text"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return write_cache(report, cache)


def measured_concurrency(
    report: dict[str, Any] | None, base_url: str, model: str
) -> tuple[int | None, str]:
    """闸门默认值的**出处**：(实测干净档数或 None, 给运维看的一句话)。

    查不到就回 `None` 而不是回一个猜测值——让调用方明确知道自己没有读数可用，
    这比"看起来有个默认值"诚实。主机与型号都要对上，否则是别台站/别个型号的数。
    """
    if not report or report.get("probe_version") != PROBE_VERSION:
        return None, "没有预检缓存（或缓存来自旧版探针）"
    if report.get("base_host") != host_of(base_url):
        return None, f"缓存属于 {report.get('base_host')}，与当前端点 {host_of(base_url)} 不是同一台"
    entry = (report.get("concurrency_by_model") or {}).get(str(model))
    if not entry:
        return None, f"这台站上没给 {model} 量过并发形状"
    limit = entry.get("recommended_limit")
    if not isinstance(limit, int) or limit <= 0:
        return None, f"量过了但最低档都不干净（{concurrency_note(entry)}）"
    return limit, concurrency_note(entry)


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


# ---------------------------------------------------------------- 上游并发形状（P4-0）

# 默认三档：1 路是基准，4 与 8 是"100 个用户同时点分析"在最省的形态下会打出来的路数。
# 为什么不是 300：300 = 100 job × 内部 3 路，那是**算术**不是上限；把尺子拉到 300 去撞
# 网关，撞出来的红分不清是"配额到了"还是"我们自己被打死了"。要谈 300 得先有 8 的读数。
DEFAULT_WIDTHS = (1, 4, 8)
# p95 相对 1 路基准的容忍倍率与绝对下限：超过两者才判"这一档已经开始排队"。
# 绝对下限是必要的：快网关的 1 路基准 p95 会四舍五入成 0.0，那时任何抖动都能"超过基准 3 倍"，
# 判据就会把噪声读成排队。倍率与下限都跟着读数一起落盘，改判据要重跑，不许只改结论。
LATENCY_PAYOUT_RATIO = 3.0
LATENCY_PAYOUT_FLOOR_S = 0.5


def probe_concurrency(
    make_client: Callable[[], Any],
    widths: Sequence[int] = DEFAULT_WIDTHS,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    settle_s: float = 1.0,
    payout_ratio: float = LATENCY_PAYOUT_RATIO,
    payout_floor_s: float = LATENCY_PAYOUT_FLOOR_S,
) -> dict[str, Any]:
    """按 1/4/8… 路**同时**打真实调用，量"这台网关此刻接得住几路"。

    三条判据的形状，都得写清楚，否则读数会被读成它没有说的东西：

    - **每档只发一波**（并发 = 该档路数，一次一发）。测的是"同时打开几个连接会不会被拒"，
      不是吞吐量极限；把同一批连接反复用会低估拒绝率。
    - **退避关掉**（调用方构造客户端时 `max_retries=0`）。带重试的探针会把 429 藏在
      "最终成功"里，于是配额看起来比实际大——这正是 P4 闸门最不能骗自己的一格。
    - **"排队"与"被拒"是两种红**：HTTP 429/5xx 是被拒，延迟涨到基准的数倍是被排队。
      两者都记，`recommended_limit` 只取**两者都干净**的最大档。

    `make_client` 每路调一次：客户端里有线程本地的 usage 归因，但预算与重试计数是实例状态，
    多线程共用一个客户端会互相踩，量出来的延迟就不是上游的形状而是我们自己的锁竞争。
    """
    import threading

    waves: list[dict[str, Any]] = []
    baseline: dict[str, Any] | None = None
    for width in sorted({int(item) for item in widths if int(item) > 0}):
        results: list[dict[str, Any]] = []
        lock = threading.Lock()

        def one() -> None:
            record = probe(make_client(), max_tokens=max_tokens)
            with lock:
                results.append(record)

        threads = [threading.Thread(target=one, name=f"preflight-{width}") for _ in range(width)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = round(time.monotonic() - started, 2)

        latencies = [float(item.get("seconds") or 0.0) for item in results]
        distribution = shape(f"width_{width}", latencies)
        errors: dict[str, int] = {}
        for item in results:
            if item.get("ok"):
                continue
            # 直接用 `probe` 分好的格子（http_429 / transport / empty_content / …）。
            # 这里不再从文案里正则找状态码：那样判据就建在"错误怎么写"上，文案一改就漏计。
            key = str(item.get("error") or "unknown")
            errors[key] = errors.get(key, 0) + 1
        refused = sum(count for code, count in errors.items() if code.startswith("http_"))
        failed = sum(errors.values())
        if width == min(sorted({int(item) for item in widths if int(item) > 0})):
            baseline = distribution
        # "排队"要**比值与绝对差都过线**才算：快网关的基准 p95 会四舍五入成 0.0，
        # 那时"大于基准的 3 倍"对任何噪声都成立，判据会自己失效（这把尺第一版就是这样）。
        delta = round(distribution["p95"] - (baseline or {}).get("p95", 0.0), 3)
        payout = bool(
            baseline
            and distribution["p95"] > (baseline["p95"] * payout_ratio)
            and delta > payout_floor_s
        )
        waves.append(
            {
                "width": width,
                "calls": len(results),
                "ok": sum(1 for item in results if item.get("ok")),
                "refused": refused,
                "failed": failed,
                "errors": errors,
                "wall_seconds": wall,
                "latency": distribution,
                "queued": payout,
                "payout_delta_seconds": delta,
                "payout_ratio": (
                    round(distribution["p95"] / baseline["p95"], 2)
                    if baseline and baseline["p95"]
                    else None
                ),
            }
        )
        if settle_s:
            time.sleep(settle_s)  # 让上一波的限流窗口过去，否则第二档测到的是第一档的余震

    # 干净 = 没人被拒、没人失败、也没人排队。少一条都会把默认值定高，
    # 而闸门定高一次的代价是线上跑到那档时才现形——那已经是用户的作业了。
    clean = [
        wave["width"]
        for wave in waves
        if wave["refused"] == 0 and wave["failed"] == 0 and not wave["queued"]
    ]
    recommended = max(clean) if clean else 0
    return {
        "widths": [wave["width"] for wave in waves],
        "calls_total": sum(wave["calls"] for wave in waves),
        "latency_payout_ratio": payout_ratio,
        "latency_payout_floor_seconds": payout_floor_s,
        "waves": waves,
        "recommended_limit": recommended,
        # 这个数是"我这把尺判出来的干净最大档"，不是网关公布的配额。闸门可以拿它当默认值，
        # 但谁都不许把它读成"上游支持 N 路"。
        "recommended_meaning": "最后一档没有 429/5xx、没有失败、p95 也没超基准的 3 倍；再往上没有数据",
        "measured_at": int(time.time()),
    }


def concurrency_note(concurrency: dict[str, Any] | None) -> str:
    """把并发读数说成一句人话（包括"根本没测过"这一种）。"""
    if not concurrency:
        return "上游并发形状未测：闸门用的是保守占位值，不是实测配额"
    waves = concurrency.get("waves") or []
    parts = [
        f"{wave['width']}路:{wave['ok']}/{wave['calls']}ok,p95={wave['latency']['p95']}s"
        + (f",拒{wave['refused']}" if wave["refused"] else "")
        + (",排队" if wave["queued"] else "")
        for wave in waves
    ]
    limit = concurrency.get("recommended_limit")
    tail = f"干净上限≥{limit}" if limit else "连最低档都不干净"
    return "上游并发实测：" + " ｜ ".join(parts) + f" ⇒ {tail}"


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
