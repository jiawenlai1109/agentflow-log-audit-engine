"""LLM 接入层：OpenAI 兼容客户端（退避重试 + usage 核算）+ MockLLM 离线模式 + 结构化输出校验。"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class LLMError(RuntimeError):
    """LLM 调用失败（网络 / 鉴权 / 响应结构异常 / 预算耗尽）。"""


class LLMHTTPError(LLMError):
    """网关回了非 2xx。带 `code` 是因为"该不该换型号"必须按状态码判，不能靠读错误文案
    ——文案改一个字，降级判据就静默失效，那正是本项目最不该再犯的那类错。"""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class LLMTransportError(LLMError):
    """连不上 / 超时 / 连接被断。换个型号可能真的有用（上游不同），这与鉴权失败性质相反。"""


class EmptyContentError(LLMError):
    """正文为空（`content` 不是字符串）的专用错误，带一份可比的结构化细节。

    思考档模型会把 `max_tokens` 花在 `reasoning_content` 上，此时正文是 `null`。
    带出 `detail` 是为了让"这一次为什么没正文"能落到产物里被归因——
    只留一句 human text，事后谁也分不出是网关坏了还是预算被思考吃满。
    """

    def __init__(self, message: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.detail = detail


class OutputValidationError(RuntimeError):
    """LLM 输出无法解析为期望的结构。"""


def extract_json(text: str) -> Any:
    """从 LLM 输出中提取首个完整 JSON 对象或数组。

    容错链：剥离 Markdown 围栏 → 定位首个 { 或 [ → 括号平衡扫描 → json.loads。
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    first_open = -1
    for i, ch in enumerate(text):
        if ch in "{[":
            first_open = i
            break
    if first_open < 0:
        raise ValueError("输出中未找到 JSON 起始符")

    stack: list[str] = []
    for i in range(first_open, len(text)):
        ch = text[i]
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break
            open_ch = stack.pop()
            if (open_ch == "{" and ch != "}") or (open_ch == "[" and ch != "]"):
                break
            if not stack:
                return json.loads(text[first_open : i + 1])
    raise ValueError("无法定位完整的 JSON 结构")


class BaseLLM(ABC):
    """LLM 统一接口。

    v1.2：预算计数点在每次真实 API 调用上——complete_structured 的内部校验重试、
    Summarizer、重规划等所有路径都会经过 _spend()，"30 次硬预算"不再被低估。
    """

    budget: Any = None  # BudgetCounter（由 pipeline 注入）
    agent_local = threading.local()  # 线程本地 agent 名（用于 usage 归因）

    def _spend(self) -> None:
        if self.budget is not None and not self.budget.spend():
            raise LLMError(f"LLM 调用预算耗尽（上限 {self.budget.limit} 次）")

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        """返回纯文本补全结果。"""

    def complete_structured(
        self,
        system: str,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        temperature: float = 0.2,
        max_tokens: int = 2000,
        max_retries: int = 2,
    ) -> BaseModel:
        """带 pydantic 校验的结构化输出，失败携带错误重试（≤ max_retries）。"""
        last_error: Exception | None = None
        current_messages = list(messages)
        for _ in range(max_retries + 1):
            self._spend()
            raw = self.complete(
                system=system,
                messages=current_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            try:
                data = extract_json(raw)
                return schema.model_validate(data)
            except Exception as exc:  # noqa: BLE001 - 统一转成校验失败
                last_error = exc
                current_messages = current_messages + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"输出格式错误：{exc}\n请重新输出符合要求的 JSON 结构。",
                    },
                ]
        raise OutputValidationError(
            f"连续 {max_retries + 1} 次输出无法通过 {schema.__name__} 校验：{last_error}"
        ) from last_error


class OpenAILLM(BaseLLM):
    """OpenAI 兼容 Chat Completions 客户端（标准库实现，零重依赖）。

    v1.2：429/5xx/网络超时传输层退避重试（尊重 Retry-After）；采集 usage token 计入预算。
    """

    RETRYABLE_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_retries: int = 2,
        thinking: str | None = None,
        thinking_budget_retry: bool = False,
        thinking_budget_factor: float = 2.0,
        max_tokens_cap: int = 8000,
        fallback_models: list[str] | None = None,
        envelope_multiplier: float = 1.0,
        envelope_floor: int = 0,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise LLMError("未配置 OPENAI_API_KEY（可写入 .env 或环境变量）")
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL") or "gpt-4o-mini"
        self.timeout = timeout
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", str(max_retries)))
        # 思考档位：enabled / disabled，留空 = 连这个字段都不发（保持接线前的线上行为）。
        # 形状按 OpenAI 兼容网关常见的 `thinking: {"type": ...}` 发送；
        # **上游认不认只能实测**——不认的网关会静默忽略或直接 400，
        # 所以"配置里写了 disabled"不等于"思考真的关了"。
        thinking = (thinking or "").strip().lower() or None
        if thinking not in (None, "enabled", "disabled"):
            raise LLMError(f"llm.thinking 只接受 enabled / disabled / 留空，收到 {thinking!r}")
        self.thinking = thinking
        # 预算被思考吃满时是否提额重试一次（默认关：多打一次真实调用就是要多花一次钱，
        # 该由运维显式开）。配置一律用 `.get` 读，不塞进 DEFAULT_CONFIG——
        # 往默认配置里加键会改变 `config` 归因指纹，制造一次"配置变了而行为没变"的噪声。
        self.thinking_budget_retry = bool(thinking_budget_retry)
        self.thinking_budget_factor = float(thinking_budget_factor)
        self.max_tokens_cap = int(max_tokens_cap)
        # 信封策略（片1）：思考档模型的 `max_tokens` 是"草稿 + 正文"共用的一个信封，
        # 草稿写到封顶时正文就是 null（2026-10-06 real 全量实测：14 次白卷，每一次
        # reasoning 字数都大于当时的 max_tokens）。抬上限本身不加钱——计费按实际写出的
        # token 算，多出来的只是终于被写出来的正文；白卷那一次照样付了草稿钱却什么都没拿到。
        # 倍率与下限只对"没关思考"的调用生效，关了思考没有草稿可挤。
        self.envelope_multiplier = float(envelope_multiplier)
        self.envelope_floor = int(envelope_floor)
        # 降级候选：**默认空 = 完全不启用**（多打一次真实调用就是要多花一次钱，
        # 而且换个型号产出的代码质量不同，会把"这次变绿"归因到模型身上）。
        # 一律 `.get` 读、不进 DEFAULT_CONFIG——加键会改 `config` 归因指纹。
        self.fallback_models = [str(model) for model in (fallback_models or []) if str(model).strip()]

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        """按型号链尝试：主型号失败且**失败类型可降级**时，才换下一个候选型号。

        三条纪律：
        1. **换型号不吞失败**——每一次降级都记一条事件（从哪个型号、到哪个型号、为什么），
           落在本次 run 的预算对象上，`evaluation.json` 与 transcript 都取得到；
        2. **能换的只有三种**：思考吃满预算的白卷签名、传输层错误/超时、5xx/429 重试用尽。
           401/403（没权限）、404（没这个端点）、非 JSON 响应（网关自己那张页）、结构异常、
           预算耗尽——换一个型号一个字都不会变，重试只是多烧一次钱；
        3. **不改共享客户端的状态**——本进程的 `llm` 是跨线程共享的（并发 3），
           把型号写回 `self.model` 会让 A 线程的降级串进 B 线程的请求体。型号只作为参数往下传。
        """
        chain = self._model_chain()
        for index, model in enumerate(chain):
            try:
                return self._complete_once(model, system, messages, temperature, max_tokens)
            except (EmptyContentError, LLMHTTPError, LLMTransportError) as exc:
                nxt = chain[index + 1] if index + 1 < len(chain) else None
                if nxt is None or not self._fallback_eligible(exc):
                    raise
                self._note_fallback(model, nxt, exc, tried=chain[: index + 1])
        raise LLMError(f"型号链全部失败（{chain}）")  # 理论上到不了：最后一枚在循环内 raise

    def _model_chain(self) -> list[str]:
        """主型号在前，候选按配置顺序，去重且不去掉主型号。"""
        chain = [self.model]
        for model in self.fallback_models:
            if model and model not in chain:
                chain.append(model)
        return chain

    def _fallback_eligible(self, exc: Exception) -> bool:
        """该不该换型号——按错误类型判，不按文案判（文案会变，判据不能跟着变）。"""
        if isinstance(exc, EmptyContentError):
            detail = exc.detail or {}
            # 与提额重试同一个签名：reasoning 在场 + finish_reason=length
            return bool(detail.get("reasoning_chars")) and detail.get("finish_reason") == "length"
        if isinstance(exc, LLMHTTPError):
            return exc.code in self.RETRYABLE_CODES
        return isinstance(exc, LLMTransportError)

    def _envelope(self, max_tokens: int) -> int:
        """按成对策略放大自然语言的请求体上限；关思考或没配策略时原样返回（一字节不改）。"""
        if self.thinking == "disabled" or self.envelope_multiplier <= 1.0:
            return max_tokens
        raised = max(int(max_tokens * self.envelope_multiplier), self.envelope_floor)
        # 只在"变大"的方向走，且不许越过封顶：宁可保住角色原值，也不把某个角色调到比自己还小
        return max(max_tokens, min(raised, self.max_tokens_cap))

    def _complete_once(
        self,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str:
        max_tokens = self._envelope(max_tokens)
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.thinking:
            payload["thinking"] = {"type": self.thinking}
        data = self._request(payload)
        text, empty = self._text_of(data, max_tokens=max_tokens, model=model)
        if text is not None:
            self._note_model(model)
            return text
        # 只有"reasoning 在场 + finish_reason=length"才是预算被思考吃满的签名，
        # 值得再花一次真实调用去提额重试；结构异常重试十次也一样坏。
        retry_at = self._retry_target(max_tokens, empty)
        if retry_at:
            data = self._request({**payload, "max_tokens": retry_at})
            text, second = self._text_of(data, max_tokens=retry_at, model=model)
            if text is not None:
                self._note({**empty, "recovered_with": retry_at})
                self._note_model(model)
                return text
            empty = second
        self._note(empty)
        hint = (
            "预算被思考链吃满：配 llm.thinking=disabled、llm.thinking_budget_retry=true 或提 max_tokens"
            if empty["reasoning_chars"]
            else "上游没给正文，也没记 reasoning（多半是网关/模型返回结构异常）"
        )
        raise EmptyContentError(
            f"LLM 没有可用正文（content={empty['content_kind']}、型号={model}、"
            f"调用方={empty['agent']}、finish_reason={empty['finish_reason']}、"
            f"reasoning_content {empty['reasoning_chars']} 字）：{hint}",
            detail=empty,
        )

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发一次 chat/completions，含传输层退避重试与 usage 记账。"""
        if self.budget is not None:
            # 每次进入都记一次真实请求（退避重试的每一次也算）：成本口径不许只看到"逻辑调用"
            self.budget.note_http_attempt()
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    code = getattr(resp, "status", 200)
                    raw = resp.read().decode("utf-8", "ignore")
                break
            except urllib.error.HTTPError as exc:
                body = self._mask(exc.read().decode("utf-8", "ignore"))[:500]
                if exc.code in self.RETRYABLE_CODES and attempt < self.max_retries:
                    time.sleep(self._backoff(attempt, exc.headers.get("Retry-After")))
                    attempt += 1
                    continue
                if exc.code == 404:
                    # 404 在这条线上只有两种来路，都值得当场说出来：要么 base_url 的
                    # /v1 段多/少了，要么这个站只提供 Anthropic 的 /v1/messages——
                    # 引擎说的是 OpenAI Chat，换型号不会有用，换协议才会。
                    raise LLMHTTPError(
                        f"LLM HTTP 404（{self.base_url}/chat/completions 不存在）："
                        f"检查 OPENAI_BASE_URL 是否该带 /v1；若该站只提供 Anthropic "
                        f"(/v1/messages) 或 OpenAI Responses，本引擎暂不接。原始正文：{body}",
                        code=404,
                    ) from exc
                raise LLMHTTPError(f"LLM HTTP {exc.code}: {body}", code=exc.code) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt, None))
                    attempt += 1
                    continue
                raise LLMTransportError(f"LLM 网络错误或超时: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # 200 + 非 JSON：网关自己的配额页/登录页/错误页。重试十次拿到的还是那张页，
            # 所以不进入退避；正文要截断并把凭据抹掉——有的网关会把请求头回显在错误页里。
            raise LLMError(
                f"LLM 响应不是 JSON（HTTP {code}，正文前 120 字：{self._mask(raw)[:120]!r}）："
                "多半是网关返回了配额页/登录页/网关错误页，而不是模型输出"
            ) from exc
        if not isinstance(data, dict):
            raise LLMError(f"LLM 响应结构异常（顶层不是对象）: {str(data)[:200]}")
        usage = data.get("usage") or {}
        if usage and self.budget is not None:
            agent = getattr(self.agent_local, "agent", "unknown")
            self.budget.add_tokens(
                agent,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
            )
        return data

    def _text_of(
        self, data: dict[str, Any], *, max_tokens: int, model: str | None = None
    ) -> tuple[str | None, dict[str, Any]]:
        """取正文；取不到时返回 `(None, 形状细节)`——细节里没有正文，也没有凭据。"""
        choices = data.get("choices")
        if not choices:
            raise LLMError(f"LLM 响应结构异常: {str(data)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        reasoning = message.get("reasoning_content") or ""
        finish = choices[0].get("finish_reason")
        if isinstance(content, str) and content.strip():
            return content, {}
        # 空白正文有两种来源，只按字段值一刀切会把它们混成一类（2026-10-06 实测）：
        # Agents-A1 默认档交回 content='' + finish_reason=length + reasoning 1019 字，
        # 那就是 #41 那个形状换了个字面值——思考把预算吃光，正文没写。这种必须判成
        # 没正文，否则空串一路漏到下游：Executor 拿到空代码、Inspector 拿到空 JSON，
        # 报出来的错跟真因毫无关系（缺陷 #42 的 fail-open）。
        # 而 content='' + stop + 没有 reasoning 仍是"模型什么都没说"，交回下游按内容判定。
        if isinstance(content, str) and not (finish == "length" and reasoning):
            return content, {}
        return None, {
            "agent": getattr(self.agent_local, "agent", None) or "unknown",
            "model": model or self.model,
            "finish_reason": finish,
            "reasoning_chars": len(reasoning),
            "max_tokens": max_tokens,
            "thinking": self.thinking,
            "content_kind": "null" if content is None else "blank",
        }

    def _retry_target(self, max_tokens: int, empty: dict[str, Any]) -> int:
        """要不要提额重试，判据写成可审计的一条：签名对、开关开、还没顶到封顶。"""
        if not self.thinking_budget_retry:
            return 0
        if empty.get("finish_reason") != "length" or not empty.get("reasoning_chars"):
            return 0
        raised = min(int(max_tokens * self.thinking_budget_factor), self.max_tokens_cap)
        return raised if raised > max_tokens else 0

    def _note(self, empty: dict[str, Any]) -> None:
        """空正文的形状进本次 run 的预算对象——`evaluation.json` 与 transcript 都从那里取。"""
        if self.budget is not None:
            self.budget.note_empty_content(empty)

    def _note_model(self, model: str) -> None:
        """记下"这次真的服务过"的型号：跨批次比较时，先排除换了型号这个变量。"""
        if self.budget is not None:
            self.budget.note_model_used(model)

    def _note_fallback(self, from_model: str, to_model: str, exc: Exception, tried: list[str]) -> None:
        """降级必留因：从哪个型号、到哪个型号、为什么、第几次。不记正文也不记凭据。"""
        if self.budget is None:
            return
        detail: dict[str, Any] = {
            "from_model": from_model,
            "to_model": to_model,
            "reason_class": type(exc).__name__,
            "tried": list(tried),
            "agent": getattr(self.agent_local, "agent", None) or "unknown",
        }
        if isinstance(exc, LLMHTTPError):
            detail["http_status"] = exc.code
        if isinstance(exc, EmptyContentError):
            shape = exc.detail or {}
            detail["finish_reason"] = shape.get("finish_reason")
            detail["content_kind"] = shape.get("content_kind")
            detail["reasoning_chars"] = shape.get("reasoning_chars")
        # 错误文本可能带网关正文：截短并抹掉凭据后再留痕
        detail["message"] = self._mask(str(exc))[:160]
        self.budget.note_fallback(detail)

    def _mask(self, text: str) -> str:
        """把凭据从任何要外印的网关正文里抹掉：错误文本会进 transcript、预检缓存与报告。

        有些网关把请求头原样回显在错误页里，而"错误正文截断 120 字"这件事本身
        并不能挡住回显。抹掉是一处修，所有出口都受益；什么都不印则会把真因一起丢掉。
        """
        leak = self.api_key or ""
        if not leak:
            return text
        return text.replace(leak, "<已抹掉的凭据>")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        """指数退避 + 随机抖动；服务端给了 Retry-After 则尊重之。"""
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(2**attempt, 8) + random.uniform(0, 0.5)


class MockLLM(BaseLLM):
    """离线确定性 LLM：按 Agent 名返回预设输出，用于验收与测试。"""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.overrides = overrides or {}

    ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
        "explorer": ("探查", "画像"),
        "planner": ("规划",),
        "executor": ("数据工程师",),
        "inspector": ("审核",),
        "visualizer": ("可视化",),
        "reporter": ("汇报",),
        "critic": ("评审",),
    }

    def _agent_name(self, system: str) -> str:
        """角色判定：优先用调用方打上的权威标签，其次才回落到 prompt 关键词嗅探。

        关键词嗅探在 M4 装载 skill 之后变成隐患：注入的方法文本里只要出现别的角色的
        关键词（"画像""审核"……），`ROLE_KEYWORDS` 的字典顺序就会把这次调用判给错误的
        角色，于是 mock 跑出一条没人看得懂的结果。BaseAgent 每次调用前已经写过
        `agent_local.agent`（原本只给 token 归因用），这里复用它当权威口径。
        """
        tagged = getattr(self.agent_local, "agent", None)
        if tagged:
            return tagged
        lowered = system.lower()
        for name, keywords in self.ROLE_KEYWORDS.items():
            if any(k in system or k in lowered for k in keywords):
                return name
        return "default"

    def _executor_code(self, messages: list[dict[str, str]]) -> str:
        """按任务描述生成确定性代码（离线模式）：求和 / 按日期趋势 / 类别对比 / 缺失列报错。"""
        first = messages[0]["content"] if messages else ""
        import ast
        import re

        def extract(label: str) -> list[str]:
            match = re.search(rf"{label}：(\[.*?\])", first)
            if not match:
                return []
            try:
                value = ast.literal_eval(match.group(1))
                return list(value) if isinstance(value, list) else []
            except (ValueError, SyntaxError):
                return []

        required = extract("必需列")
        available = extract("可用列")
        desc_match = re.search(r"任务：(.+)", first)
        desc = desc_match.group(1) if desc_match else ""
        header = (
            "import json, os\n"
            "import pandas as pd\n"
            "df = pd.read_csv(os.environ['DATA_PATH'])\n"
            "num_cols = df.select_dtypes(include='number').columns.tolist()\n"
            f"_TASK_DESC = {desc!r}\n"
        )
        if required and any(col not in available for col in required):
            missing = next(col for col in required if col not in available)
            return header + f"print(df[{missing!r}].sum())\n"
        # 跨表任务（M2-3）：prompt 里列出了几张表的 env 路径与 join 键，确定性做真 join。
        # 必须排在"总/合计"分支之前——"汇总"里含"总"，否则会被单表求和抢走。
        refs = re.findall(r"os\.environ\['DATA_PATH_(T\d+)'\]", first)
        key_match = re.search(r"join 键：([^（\n,]+)", first)
        side_columns = re.findall(r"侧列名 '([^']+)'", first)
        if len(refs) == 2 and key_match:
            key = key_match.group(1).strip()
            # 两侧实际列名（包内别名场景：t1 的 src_ip ↔ t2 的 主机）。不 rename 直接 merge
            # 会得到空表或 KeyError——别名机制到这一步才算真的被执行器用上。
            left_column = side_columns[0] if len(side_columns) == 2 else key
            right_column = side_columns[1] if len(side_columns) == 2 else key
            renames = ""
            if left_column != key:
                renames += f"_LEFT = _LEFT.rename(columns={{{left_column!r}: _KEY}})\n"
            if right_column != key:
                renames += f"_RIGHT = _RIGHT.rename(columns={{{right_column!r}: _KEY}})\n"
            return (
                "import json, os\n"
                "import pandas as pd\n"
                f"_LEFT = pd.read_csv(os.environ['DATA_PATH_{refs[0]}'])\n"
                f"_RIGHT = pd.read_csv(os.environ['DATA_PATH_{refs[1]}'])\n"
                f"_KEY = {key!r}\n"
                f"_LCOL = {left_column!r}\n"
                f"_RCOL = {right_column!r}\n"
                + renames
                + "merged = _LEFT.merge(_RIGHT, on=_KEY, how='inner')\n"
                "num = next((c for c in merged.select_dtypes(include='number').columns if c != _KEY), None)\n"
                "cats = [c for c in merged.select_dtypes(include=['object']).columns\n"
                "          if c != _KEY and not any(k in str(c) for k in ('日期', 'date', '时间'))]\n"
                "out = {'rows': int(len(merged)), 'columns': list(merged.columns),\n"
                "       'head': merged.head(5).astype(str).to_dict(orient='records'),\n"
                "       'aggregate': {'join_行数': int(len(merged))}}\n"
                "if num is not None and cats:\n"
                "    agg = merged.groupby(cats[0])[num].sum().sort_values(ascending=False)\n"
                "    out = {'rows': int(len(agg)), 'columns': [cats[0], num],\n"
                "           'head': [{cats[0]: str(k), num: float(v)} for k, v in agg.items()],\n"
                "           'aggregate': {'join_行数': int(len(merged)), '合计_' + num: float(merged[num].sum()),\n"
                "                           '最高_' + cats[0]: str(agg.index[0])}}\n"
                "print(json.dumps(out, ensure_ascii=False))\n"
            )
        if any(k in desc for k in ("总", "合计", "sum")):
            return (
                header
                + "num = num_cols[0]\n"
                + "total = float(df[num].sum())\n"
                + "print(json.dumps({'rows': int(len(df)), 'columns': list(df.columns), "
                + "'head': df.head(5).astype(str).to_dict(orient='records'), "
                + "'aggregate': {'合计_' + num: total}}, ensure_ascii=False))\n"
            )
        if any(k in desc for k in ("按日期", "每日", "趋势", "走势")):
            return header + """
date_col = None
for c in df.columns:
    if any(k in str(c) for k in ('日期', 'date', '时间')):
        date_col = c
        break
if date_col is not None:
    df[date_col] = pd.to_datetime(df[date_col])
    num = num_cols[0] if num_cols else None
    if num is not None:
        agg = df.groupby(df[date_col].dt.date)[num].sum().sort_index()
        agg = agg.tail(7) if len(agg) > 30 else agg
        head = [{'日期': str(k), num: float(v)} for k, v in agg.items()]
        out = {'rows': len(head), 'columns': [date_col, num], 'head': head}
    else:
        out = {'rows': int(len(df)), 'columns': list(df.columns), 'head': df.head(5).astype(str).to_dict(orient='records')}
else:
    out = {'rows': int(len(df)), 'columns': list(df.columns), 'head': df.head(5).astype(str).to_dict(orient='records')}
print(json.dumps(out, ensure_ascii=False))
"""
        if any(k in desc for k in ("对比", "哪个", "最高", "排名")):
            return header + """
cat_cols = df.select_dtypes(include=['object']).columns.tolist()
cat_cols = [c for c in cat_cols if not any(k in str(c) for k in ('日期', 'date'))] or cat_cols
num = None
for kw in ('利润', '利润率', '销售额', '销量'):
    if kw in _TASK_DESC:
        num = next((c for c in num_cols if kw in str(c)), None)
        if num:
            break
num = num or (num_cols[0] if num_cols else None)
if cat_cols and num:
    import re as _re
    _m = _re.search(r'前(\\d+)', _TASK_DESC)
    _top = int(_m.group(1)) if _m else 10
    agg = df.groupby(cat_cols[0])[num].sum().sort_values(ascending=False).head(_top)
    head = [{cat_cols[0]: str(k), num: float(v)} for k, v in agg.items()]
    out = {'rows': len(head), 'columns': [cat_cols[0], num], 'head': head}
else:
    out = {'rows': int(len(df)), 'columns': list(df.columns), 'head': df.head(5).astype(str).to_dict(orient='records')}
print(json.dumps(out, ensure_ascii=False))
"""
        return (
            header
            + "print(json.dumps({'rows': int(len(df)), 'columns': list(df.columns), "
            + "'head': df.head(5).astype(str).to_dict(orient='records')}, ensure_ascii=False))\n"
        )

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        name = self._agent_name(system)
        if name in self.overrides:
            return self.overrides[name]
        if name == "executor":
            return self._executor_code(messages)
        defaults = {
            "planner": '{"question": "", "time_base": null, "tasks": []}',
            "inspector": '{"task_id": 1, "status": "PASS", "checks": [], "suggestion": null}',
            "critic": '{"verdict": "PASS", "rounds": 1, "issues": []}',
            "visualizer": (
                "import os\n"
                "import matplotlib\n"
                "matplotlib.use('Agg')\n"
                "import matplotlib.pyplot as plt\n"
                "plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']\n"
                "plt.rcParams['axes.unicode_minus'] = False\n"
                "import pandas as pd\n"
                "df = pd.read_csv(os.environ['DATA_PATH'])\n"
                "num_cols = df.select_dtypes(include='number').columns.tolist()\n"
                "ctype = os.environ.get('CHART_TYPE', 'bar')\n"
                "if ctype == 'line' and num_cols:\n"
                "    for c in df.columns:\n"
                "        if any(k in str(c) for k in ('日期', 'date')):\n"
                "            df[c] = pd.to_datetime(df[c])\n"
                "            df = df.set_index(c)\n"
                "            break\n"
                "    df.groupby(df.index)[num_cols[0]].sum().tail(30).plot(kind='line', title='Trend ' + num_cols[0])\n"
                "elif num_cols:\n"
                "    df[num_cols[0]].head(20).plot(kind='bar', title='Top-20 ' + num_cols[0])\n"
                "else:\n"
                "    df.head(10).plot(kind='bar')\n"
                "plt.tight_layout()\n"
                "plt.savefig(os.environ['CHART_PATH'], dpi=100)\n"
            ),
            "reporter": (
                "【总体概况】本次分析共完成若干任务，数据规模以表格为准。\n"
                "【趋势分析】趋势变化请结合图表查看。\n"
                "【结论建议】建议重点关注表格中的关键指标与异常值。"
            ),
        }
        if name in defaults:
            return defaults[name]
        return json.dumps(
            {"mock": True, "agent": name, "messages_count": len(messages)},
            ensure_ascii=False,
        )
