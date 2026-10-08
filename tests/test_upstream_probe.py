"""P4 的尺子先测尺子：上游并发探针自己必须被测过，才配拿它的读数定默认值。

这里**一次真实调用都不发**——用假客户端把形状喂进去。原因不是省事，是判据：
真实网关的形状要花真钱才能重来，而"这把尺怎么读数"必须每次都能免费重来。
`recommended_limit` 是**观测结果**，这套用例管的是**读数规则**：
被拒要单独一格、排队要单独一格、没有读数时不许编一个数出来。
"""

from __future__ import annotations

import json
import threading
import time

from agentflow.core.llm import LLMError, LLMHTTPError
from agentflow.core.llm_preflight import (
    CONCURRENCY_HISTORY_KEEP,
    PROBE_VERSION,
    measured_concurrency,
    merge_concurrency,
    probe_concurrency,
    read_cache,
)
from agentflow.core.stats import percentile as stats_percentile
from agentflow.core.stats import shape as stats_shape

GOOD = '{"items": ["a", "b", "c"]}'


class FakeClient:
    """一路线上：给它什么脚本就演什么（探针按路构造客户端，一路一个实例）。"""

    def __init__(self, outcome: object, delay: float = 0.0):
        self.thinking = None
        self.outcome = outcome
        self.delay = delay

    def complete(self, system: str, messages, **kwargs) -> str:
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return str(self.outcome)


def scripted(outcomes: list[object], delays: list[float] | None = None):
    """按**调用次序**分发结果：第 n 路拿第 n 项，用完就重复最后一项。

    探针每档会调 `width` 次工厂，所以这里能精确指定"这一档的第几路被拒"，
    而不必赌线程调度顺序——读数规则的用例如果自己靠运气，它就成不了判据。
    """
    state = {"index": 0}
    lock = threading.Lock()

    def factory():
        with lock:
            position = state["index"]
            state["index"] += 1
        outcome = outcomes[position] if position < len(outcomes) else outcomes[-1]
        delay = 0.0
        if delays:
            delay = delays[position] if position < len(delays) else delays[-1]
        return FakeClient(outcome, delay=delay)

    return factory


def test_all_clean_widths_report_the_top_one():
    """三档全干净 ⇒ 推荐值取最高那档，并说清它是「再往上没有数据」，不是网关公布的配额。"""
    result = probe_concurrency(scripted([GOOD]), widths=(1, 2, 4), settle_s=0)
    assert [wave["width"] for wave in result["waves"]] == [1, 2, 4]
    assert result["recommended_limit"] == 4, result
    assert all(wave["refused"] == 0 and not wave["queued"] for wave in result["waves"])
    assert result["calls_total"] == 7, "每档一波：1+2+4"
    assert "再往上没有数据" in result["recommended_meaning"]


def test_http_429_is_counted_as_refused_not_as_success():
    """被拒必须进 `refused` 这一格。

    第 1 档那一路成功（那是基准），第 2 档两路里第二路拿到 429 ⇒ 干净档只剩 1。
    推荐值不许因为"大部分成功"就往上涨——闸门按 2 放行就会天天撞 429。
    """
    denied = LLMHTTPError("LLM HTTP 429: too many requests", code=429)
    result = probe_concurrency(scripted([GOOD, GOOD, denied]), widths=(1, 2), settle_s=0)
    narrow, wide = result["waves"]
    assert narrow["refused"] == 0 and narrow["ok"] == 1, narrow
    assert wide["refused"] == 1, wide
    assert wide["errors"].get("http_429") == 1, wide["errors"]
    assert result["recommended_limit"] == 1, result


def test_latency_payout_is_recorded_as_queued():
    """延迟涨到基准数倍 = 排队。它和"被拒"是两种红，读数只认两者都干净的那档。

    `payout_floor_s` 压到 0.01 是为了让用例跑得动：真实读数里那个绝对下限是 0.5 秒
    （见下一条），这儿测的是判据形状不是网关速度。
    """
    result = probe_concurrency(
        scripted([GOOD, GOOD, GOOD], delays=[0.0, 0.06, 0.06]),
        widths=(1, 2),
        settle_s=0,
        payout_floor_s=0.01,
    )
    baseline, payout = result["waves"]
    assert not baseline["queued"], baseline
    assert payout["queued"], (baseline["latency"], payout["latency"])
    assert payout["refused"] == 0 and payout["failed"] == 0, "这一档只是慢，没有被打回"
    assert payout["payout_delta_seconds"] > 0.01, payout
    assert result["recommended_limit"] == 1, result


def test_zero_baseline_does_not_disable_the_payout_check():
    """基准快到 p95 四舍五入成 0.0 时，"超过基准 3 倍"对任何噪声都成立——判据会自己失效。

    这是这把尺第一版的真bug：`if baseline and baseline["p95"]` 让 p95=0.0 的基准直接跳过判据，
    于是"快网关"上永远测不出排队，读数看起来一切正常。修法是**比值与绝对差都要过线**。
    """
    result = probe_concurrency(
        scripted([GOOD] * 3, delays=[0.0, 0.55, 0.55]), widths=(1, 2), settle_s=0
    )
    baseline, payout = result["waves"]
    assert baseline["latency"]["p95"] == 0.0, baseline["latency"]  # 就是这个 0 让旧判据失效
    assert payout["queued"], (baseline["latency"], payout["latency"])
    assert result["recommended_limit"] == 1, result
    # 判据的两个阈值跟着读数落盘：改了判据就必须重跑，不许只改结论
    assert result["latency_payout_floor_seconds"] == 0.5
    assert result["latency_payout_ratio"] == 3.0


def test_transport_error_is_not_mixed_with_http_refusal():
    """网络断与配额到点是两件事：分不开就没法回答「闸门该定在几路」。"""
    result = probe_concurrency(
        scripted([GOOD, LLMError("LLM 网络错误或超时: timed out")]), widths=(1, 2), settle_s=0
    )
    wide = result["waves"][1]
    assert wide["refused"] == 0, "没有 HTTP 状态码就不该算成被上游拒"
    assert wide["errors"], wide
    assert not any(key.startswith("http_") for key in wide["errors"]), wide["errors"]
    # 传输错误也让这一档不干净：默认值不许建立在"错误没归类"之上
    assert result["recommended_limit"] == 1, result


def test_measured_concurrency_never_guesses(tmp_path):
    """没有读数就回 None 加一句原因：闸门宁可知道自己没数，也不许拿猜测值冒充实测。"""
    assert measured_concurrency(None, "https://x.example/v1", "m1")[0] is None
    cache = tmp_path / "p.json"
    merge_concurrency(cache, "https://x.example/v1", {"model": "m1", "recommended_limit": 4, "waves": []})

    report = read_cache(cache)
    assert measured_concurrency(report, "https://x.example/v1", "m1")[0] == 4
    # 换 endpoint = 另一台网关：那台上量出的 4 在这台不作数
    assert "不是同一台" in measured_concurrency(report, "https://y.example/v1", "m1")[1]
    # 换个型号也一样：不同型号的配额可以完全不同
    other = measured_concurrency(report, "https://x.example/v1", "m2")
    assert other[0] is None and "没给 m2 量过" in other[1], other
    # 量过了但最低档都不干净 ⇒ 仍然没有可用默认值
    dirty = tmp_path / "d.json"
    merge_concurrency(dirty, "https://x.example/v1", {"model": "m1", "recommended_limit": 0, "waves": []})
    assert measured_concurrency(read_cache(dirty), "https://x.example/v1", "m1")[0] is None


def test_merge_keeps_model_verdicts_and_separates_by_model(tmp_path):
    """并发读数不许把型号结论冲掉：同一份缓存是两件事的证据。"""
    cache = tmp_path / "p.json"
    cache.write_text(
        json.dumps(
            {
                "probe_version": PROBE_VERSION,
                "base_host": "x.example",
                "models": [{"model": "m1", "verdict": "usable", "note": "默认档就能出可用正文"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    merge_concurrency(cache, "https://x.example/v1", {"model": "m1", "recommended_limit": 4, "waves": []})
    merge_concurrency(cache, "https://x.example/v1", {"model": "m2", "recommended_limit": 8, "waves": []})
    report = json.loads(cache.read_text(encoding="utf-8"))
    assert report["models"][0]["verdict"] == "usable"
    by_model = report["concurrency_by_model"]
    assert by_model["m1"]["recommended_limit"] == 4
    assert by_model["m2"]["recommended_limit"] == 8


def test_history_is_a_chain_not_a_mirror(tmp_path):
    """同一个型号的第二次读数要**接在后面**，不许把上一次吞进自己肚子里。

    这条用例守的是合并逻辑的真 bug：第一版写的是
    `concurrency["history"] = history + [concurrency]`——把容器塞进自己的取值里，
    缓存当场成循环引用，写盘时 json 递归爆栈。断言分三层，缺一层就抓不住这个形状：
    能写盘（不递归）、两次读数按时间有序地都在、任何一格都不再套 `history`。
    """
    cache = tmp_path / "p.json"
    merge_concurrency(cache, "https://x.example/v1", {
        "model": "m1", "measured_at": 100, "widths": [1, 4, 8],
        "recommended_limit": 4, "waves": [{"width": 1}],
    })
    merge_concurrency(cache, "https://x.example/v1", {
        "model": "m1", "measured_at": 200, "widths": [1, 4, 8, 16],
        "recommended_limit": 16, "waves": [{"width": 16}],
    })
    entry = json.loads(cache.read_text(encoding="utf-8"))["concurrency_by_model"]["m1"]
    history = entry["history"]
    assert [item["measured_at"] for item in history] == [100, 200], history
    assert history[0]["recommended_limit"] == 4, "上一次的读数是「默认值从哪来」的出处，不能被盖掉"
    assert history[-1]["recommended_limit"] == entry["recommended_limit"] == 16
    assert not any("history" in item for item in history), "历史格不能再套历史——那是循环引用的形状"

    # 第三次：判重按 measured_at，旧缓存兼容那条不许把已有读数推成两格
    merge_concurrency(cache, "https://x.example/v1", {
        "model": "m1", "measured_at": 300, "recommended_limit": 8, "waves": [],
    })
    entry = json.loads(cache.read_text(encoding="utf-8"))["concurrency_by_model"]["m1"]
    assert [item["measured_at"] for item in entry["history"]] == [100, 200, 300], entry["history"]

    # 历史有顶：当前值只看顶层，留 20 格是查出处用的，不是拿来攒文件的
    for index in range(CONCURRENCY_HISTORY_KEEP + 5):
        merge_concurrency(cache, "https://x.example/v1", {
            "model": "m1", "measured_at": 1000 + index, "recommended_limit": 1, "waves": [],
        })
    entry = json.loads(cache.read_text(encoding="utf-8"))["concurrency_by_model"]["m1"]
    assert len(entry["history"]) == CONCURRENCY_HISTORY_KEEP, len(entry["history"])
    assert entry["history"][-1]["measured_at"] == 1000 + CONCURRENCY_HISTORY_KEEP + 4


def test_a_legacy_cache_loses_its_last_reading_without_the_compat_push(tmp_path):
    """旧格式缓存（顶层直接盖着读数、没有 `history`）并进新读数时，那份旧证据要接进历史。

    这是**回退分支**，不测它就没有人知道它接没接上：真实那台站的缓存正是旧格式生成的
    （第一次实测定档发生在加历史之前）。这一族的老账是"回退分支把'没接上'伪装成'接上了'"。
    """
    cache = tmp_path / "legacy.json"
    cache.write_text(
        json.dumps(
            {
                "probe_version": PROBE_VERSION,
                "base_host": "x.example",
                "models": [],
                "concurrency_by_model": {
                    "m1": {
                        "model": "m1",
                        "measured_at": 100,
                        "widths": [1, 4, 8],
                        "recommended_limit": 4,
                        "waves": [{"width": 8, "refused": 1}],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    merge_concurrency(cache, "https://x.example/v1", {
        "model": "m1", "measured_at": 200, "recommended_limit": 8, "waves": [],
    })
    entry = json.loads(cache.read_text(encoding="utf-8"))["concurrency_by_model"]["m1"]
    assert [item["measured_at"] for item in entry["history"]] == [100, 200], entry["history"]
    assert entry["history"][0]["recommended_limit"] == 4, "旧那次的干净档是默认值的出处，不能盖掉"
    # 兼容那条只推**读数**，不把它自己的历史一起推：整份塞进去会套出一层嵌套
    assert not any("history" in item for item in entry["history"]), entry["history"]
    # 顶层是当前值：闸门开闸读它，不读历史
    assert entry["recommended_limit"] == 8


def test_the_probe_and_the_load_harness_share_one_ruler():
    """分位数只许有一处实现：压测读数与上游并发读数要能放进同一张表里比。"""
    import scripts.load_test as harness

    assert harness.shape is stats_shape
    assert harness.percentile is stats_percentile
