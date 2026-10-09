"""P5-4：压测尺子里那个"会重连的客户端"，与浏览器那一份策略不许分叉。

这片要回答的是 P2 留下的那半笔账：杀受理层那一轮**作业没丢**，但同轮 40 次轮询连接失败——
"用户当时拿不到结果"。要证明这一格被消掉了，尺子得能用两种客户端形状各跑一遍同一把尺。

于是有了一条新的风险：**尺子里那份策略不是真客户端用的那份**。真客户端住在
`frontend/src/api/backoff.js`（P5-2 定的权威，可重试集合、退避参数、`Retry-After` 优先都在那儿）。
压测里这份是镜像——镜像不加相等守卫，迟早跟权威分叉，而分叉之后的"扛住了"是 Python 那半份
自己扛的，与浏览器无关：那正是这一族最难查的形状（读数没问题，读的是错的东西）。

所以这里两组判据：**两条跨语言相等守卫**（按文本读回 backoff.js 的常量与集合）+
**镜像自己的行为**（不退避的形状一次就放手、会退避的形状撑过两次被杀、次数有顶、
`Retry-After` 优先、401/422 一次都不重试）。
"""

from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path

import pytest

from scripts import load_test as harness

BACKOFF = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "backoff.js"


def _function_body(name: str) -> str:
    """取 `backoff.js` 里那个导出函数的正文（到第一个顶格 `}` 为止）。"""
    text = BACKOFF.read_text(encoding="utf-8")
    match = re.search(rf"export function {name}\([^)]*\) \{{\n(.*?)\n\}}", text, re.S)
    assert match, f"backoff.js 里找不到 {name}——它被改名或删掉了，那这条守卫就该红，不是静默跳过"
    return match.group(1)


# ---------------------------------------------------------------- 两条相等守卫


def test_the_retriable_status_set_matches_the_browser_authority():
    """可重试的状态集合：尺子与浏览器必须同一份。

    JS 用 `!status` 表示"没有响应＝连不上"，另外把 `status === 0` 也当同一类；Python 那侧
    对应的是 `retriable(exc=...)`（异常没有状态）。所以这里比的是**去掉 0 之后**的集合。
    """
    body = _function_body("shouldRetry")
    js_statuses = {int(value) for value in re.findall(r"status === (\d+)", body)}
    assert 0 in js_statuses, "JS 那份不再把『没有响应』算可重试 ⇒ Python 镜像的异常分支要一起重看"
    assert js_statuses - {0} == set(harness.RETRIABLE_STATUS), (
        f"两份可重试集合分叉了：JS={sorted(js_statuses - {0})} 尺子={sorted(harness.RETRIABLE_STATUS)}"
    )


def test_the_backoff_numbers_match_the_browser_authority():
    """退避参数：封顶、倍数、次数上限、抖动比例，四个数一个都不许各写一遍。"""
    defaults = re.search(r"export const DEFAULTS = \{(.*?)\n\}", BACKOFF.read_text(encoding="utf-8"), re.S)
    assert defaults, "backoff.js 里的 DEFAULTS 找不到了"
    table = {key: float(value) for key, value in re.findall(r"(\w+):\s*([\d.]+)", defaults.group(1))}
    assert table["baseMs"] / 1000 == pytest.approx(harness.RETRY_BASE_S)
    assert table["capMs"] / 1000 == pytest.approx(harness.RETRY_CAP_S)
    assert table["maxAttempts"] == harness.RETRY_MAX_ATTEMPTS
    assert table["jitterRatio"] == pytest.approx(harness.RETRY_JITTER_RATIO)


# ---------------------------------------------------------------- 镜像自己的行为


class _Response:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


def test_no_response_is_worth_retrying_while_401_and_422_are_not():
    """受理层被杀的那几秒表现成"连不上"⇒ 要重试；权限与形状错了重试不会变好⇒ 不许重试。"""
    assert harness.retriable(exc=ConnectionResetError("reset")) is True
    assert harness.retriable(status=429) is True
    assert harness.retriable(status=502) is True
    for status in (401, 403, 404, 422):
        assert harness.retriable(status=status) is False, f"{status} 被当成可重试就是在打上游"


def test_the_delay_grows_caps_and_a_retry_after_beats_it():
    """指数增长 + 封顶；服务端说等多久就等多久（它知道，我们不知道）。

    抖动那一项用 `rng` 注入才能钉死：`random.Random(0).random()` 不是 0（第一版就这么写错了一次，
    把"固定种子"当成"没有抖动"），所以形状断言走一个恒返回 0 的 rng，抖动的**上界**再单独测。
    """

    class _NoJitter:
        @staticmethod
        def random() -> float:
            return 0.0

    assert harness.next_delay_s(1, _NoJitter) == pytest.approx(harness.RETRY_BASE_S)
    assert harness.next_delay_s(4, _NoJitter) == pytest.approx(min(harness.RETRY_CAP_S, harness.RETRY_BASE_S * 8))
    assert harness.next_delay_s(30, _NoJitter) == pytest.approx(harness.RETRY_CAP_S), "退避必须有顶"
    live = random.Random(0)
    assert harness.RETRY_BASE_S <= harness.next_delay_s(1, live) <= harness.RETRY_BASE_S * (
        1 + harness.RETRY_JITTER_RATIO
    ), "抖动只能在有界范围内推高，不能把退避推成第二个数量级"
    assert harness.next_delay_s(30, live) <= harness.RETRY_CAP_S * (1 + harness.RETRY_JITTER_RATIO)
    assert harness.retry_after_s(_Response(429, {"retry-after": "2"})) == 2.0
    assert harness.retry_after_s(_Response(429, {"retry-after": "900"})) == 60.0, "不许照抄一个永远等"
    assert harness.retry_after_s(_Response(429, {"retry-after": "soon"})) == 0.0, "非法值不当成永远等"


@pytest.mark.parametrize(
    "enabled,expected_attempts",
    [(False, 1), (True, 3)],
    ids=["naked", "resilient"],
)
def test_the_naked_client_gives_up_once_while_the_resilient_one_survives_two_kills(
    enabled, expected_attempts
):
    """P2 那笔账的形状：杀受理层之后前两次轮询连不上，第三次服务已重启。

    off 时这一轮观测直接中断（那 40 次 `transport_failed` 就是这么来的）；
    on 时它自己接回来，读数里该留下"重试过几次、分别是什么形状"。
    """
    attempts = {"n": 0}
    slept: list[float] = []

    async def task(_attempt: int):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionResetError("连接被对方关闭")
        return _Response(200)

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def go():
        ledger: dict = {}
        if not enabled:
            with pytest.raises(ConnectionResetError):
                await harness.resilient_call(task, enabled=False, sleep=fake_sleep, ledger=ledger)
        else:
            assert (await harness.resilient_call(task, enabled=True, sleep=fake_sleep, ledger=ledger)).status_code == 200
        return ledger

    ledger = asyncio.run(go())
    assert attempts["n"] == expected_attempts
    if enabled:
        assert ledger["retries"] == 2 and len(slept) == 2, ledger
        assert slept[1] > slept[0], f"第二次退避该比第一次长：{slept}"


def test_the_retry_count_has_a_ceiling_so_a_dead_server_cannot_be_hit_forever():
    """次数有顶：没有顶的退避就是无限重试，那台死掉的受理层会被自己的客户端打死。"""
    calls = {"n": 0}

    async def always_down(_attempt: int):
        calls["n"] += 1
        raise ConnectionResetError("没人监听")

    async def fake_sleep(_seconds):
        return None

    with pytest.raises(ConnectionResetError):
        asyncio.run(harness.resilient_call(always_down, enabled=True, sleep=fake_sleep))
    assert calls["n"] == harness.RETRY_MAX_ATTEMPTS, calls


def test_a_throttled_response_is_retried_and_honours_retry_after():
    """429 走 `Retry-After` 而不是我们自己的指数：配额那两格知道还要等多久。"""
    seen: list[float] = []
    responses = iter([_Response(429, {"retry-after": "3"}), _Response(200)])

    async def task(_attempt: int):
        return next(responses)

    async def fake_sleep(seconds):
        seen.append(seconds)

    result = asyncio.run(harness.resilient_call(task, enabled=True, sleep=fake_sleep))
    assert result.status_code == 200
    assert seen == [3.0], f"该照服务端说的等：{seen}"


# ---------------------------------------------------------------- 开关真的接到了线上


def _resilient_call_sites(body: str) -> list[str]:
    """把 `one_user` 里每个 `resilient_call(...)` 调用的参数文本抠出来（按括号配对，不靠数行）。"""
    sites: list[str] = []
    needle = "resilient_call("
    start = 0
    while True:
        index = body.find(needle, start)
        if index < 0:
            return sites
        # 已经吃掉左括号了，所以深度从 1 起算：从 0 起会在**内层**那个 `client.get(...)` 的
        # 右括号上就收工，抠出来的是半截参数——那条守卫自己先是这么假通过的。
        depth, end = 1, index + len(needle)
        while end < len(body):
            if body[end] == "(":
                depth += 1
            elif body[end] == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        sites.append(body[index + len(needle) : end])
        start = end


def test_the_switch_reaches_both_the_submit_and_the_poll_paths():
    """开关存在≠生效（PB3 那次的老账）：`--resilient-client` 必须**两个**调用点都接上。

    提交那处只接一半的后果是"轮询会重连、提交不会"——受理层被杀时客户端连 `job_id` 都拿不到，
    那一轮连"有一个作业"都不成立，退避再健壮也没东西可续。所以这里逐个调用点查参数，
    不是数一下出现次数（"count >= 2" 那种写法拦不住"两处都接在同一个调用上"）。
    """
    source = (Path(__file__).resolve().parents[1] / "scripts" / "load_test.py").read_text(encoding="utf-8")
    body = source[source.index("async def one_user(") : source.index("async def read_stream(")]
    sites = _resilient_call_sites(body)
    assert len(sites) == 2, f"提交与轮询该各一个 resilient_call，实到 {len(sites)} 个"
    for site in sites:
        assert "enabled=resilient" in site, f"有个调用点的开关没接上：{site[:120]}"
    assert "if resilient else None" in body, "幂等键要按'一次用户意图一个'生成，不许每次重试新造一个"
    assert "Idempotency-Key" in body, "韧性客户端不带幂等键就是放大器"


def test_the_report_says_which_client_shape_produced_it():
    """两份形状跑出来的报告必须分得开：形状落进 json，且 CLI 默认是改造前那个形状。

    默认设成 on 的话，"before"这半条对照以后再也跑不出来（而它正是那 40 次连接失败的账）。
    """
    source = (Path(__file__).resolve().parents[1] / "scripts" / "load_test.py").read_text(encoding="utf-8")
    assert '--resilient-client", choices=("on", "off"), default="off"' in source, (
        "开关的默认值就是 before 的形状，不许被顺手改成 on"
    )
    assert 'result["client_shape"] = args.resilient_client' in source, "形状不落盘，两份读数就分不出谁是谁"
    assert "args.resilient_client == \"on\"" in source, "drive 收到的必须是这个开关本身"


def test_the_accept_layer_comes_back_inside_the_retry_window():
    """对照必须让受理层在客户端还在退避的窗口里回来，否则两种形状的读数会一模一样。

    这条是**今天读出来的**：第一版 pair 两边 `transport_failed` 都是 39，`retries=312 / abandoned=39`
    看起来像"韧性没用"——真相是脚本要等所有用户跑完才重启那座塔，服务在整段退避期里根本没回来。
    所以这一格必须是显式参数、默认不帮忙（P2 那笔"没人帮忙的排空"读数不能被顺手改掉），
    而且它要落进 json：`server_back_at_s` 决定"客户端没扛住"该归给谁。
    """
    source = (Path(__file__).resolve().parents[1] / "scripts" / "load_test.py").read_text(encoding="utf-8")
    assert '"--restart-server-after", type=float, default=None' in source, (
        "默认值就是『没人帮忙』的形状，不许被对照需求带跑"
    )
    assert "args.restart_server_after" in source, "参数没接进 drive 就等于没有"
    assert '"server_back_at_s": holder.get("server_back_at_s")' in source, "服务回来的时刻必须落盘"
    assert "wait_ready(base, restarted" in source, "拉起来 ≠ 可用：不等它就绪就会把服务没起来算进客户端账上"


def test_the_harness_records_the_replay_probe_and_the_kill_window():
    """跑完补的那一枪与"杀的那一刻库里有几行"都必须落盘，否则 0 有两种读法。

    `submit_replays=0` 可能是**第一次提交根本没落库**（没打到那一瞬），也可能是
    **落库了而重发又建了一行**（承诺破了）。`rows_at_kill` 就是把这两者分开的那一格；
    它取不到时必须是 -1 而不是 0——把"没读到"写成 0，就是让一句未知冒充一句结论。
    """
    source = (Path(__file__).resolve().parents[1] / "scripts" / "load_test.py").read_text(encoding="utf-8")
    killer = source[source.index("async def killer_task") : source.index("def harvest_locks")]
    assert '"replay_probe": replay' in source, "重放探针的结果不落盘，跑完就查不到出处"
    assert '"rows_at_kill": holder.get("rows_at_kill")' in source, "杀那一刻的行数不落盘"
    assert "holder[\"rows_at_kill\"] = -1" in killer, (
        "取不到要记 -1：0 是一个有意义的读数（库里真的还没有行），不能拿来当失败值"
    )
    reading = source[source.index("def idempotency_reading") : source.index("def claim_attribution")]
    assert "sqlite3.Error" in reading, "读数函数不许把库错误吞成空字典（吞了就等于说「没有重复」）"


def test_the_resilience_counters_exist_even_when_nothing_happened():
    """缺键与零是两件事：这四格必须**先建好**再记账。

    否则"这一轮一次都没重试"与"这一轮没测重试"在 json 里长得一模一样——
    而对照报告最容易就在这种格子上被读成好消息。
    """
    source = (Path(__file__).resolve().parents[1] / "scripts" / "load_test.py").read_text(encoding="utf-8")
    init = source[source.index("counters = {") : source.index("counters = {") + 500]
    for key in ("retries", "abandoned", "submit_replays", "jobs_unresolved"):
        assert f'"{key}": 0' in init, f"计数器初始化里少了 {key}：那格的'零'会变成'没测'"
    poll = source[source.index('phase = "poll"') :]
    assert 'if status not in TERMINAL_STATUSES:' in poll and "jobs_unresolved" in poll, (
        "轮询到 600s 还没落到终态必须记一笔——那是**结果**，不是"
        "没测到"
    )
    # 重放那一格只能**守形状**：要真撞出它，得让受理层恰好在提交那一瞬被杀（时序不可复现），
    # 而今天这两轮对照里它确实是 0——`0` 是"没发生"，前提是这行代码还在。
    one_user = source[source.index("async def one_user(") : source.index("async def read_stream(")]
    assert 'counters["submit_replays"]' in one_user and "idempotency_replayed" in one_user, (
        "重放必须就地记账，否则'重试没产生第二个作业'又只能靠数日志证明"
    )
