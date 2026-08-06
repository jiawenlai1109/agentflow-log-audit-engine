"""LLM 接入层：OpenAI 兼容客户端 + MockLLM 离线模式 + 结构化输出校验。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class LLMError(RuntimeError):
    """LLM 调用失败（网络 / 鉴权 / 响应结构异常）。"""


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
    """LLM 统一接口。"""

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
    """OpenAI 兼容 Chat Completions 客户端（标准库实现，零重依赖）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise LLMError("未配置 OPENAI_API_KEY（可写入 .env 或环境变量）")
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL") or "gpt-4o-mini"
        self.timeout = timeout

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")[:500]
            raise LLMError(f"LLM HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM 网络错误: {exc.reason}") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常: {data}") from exc


class MockLLM(BaseLLM):
    """离线确定性 LLM：按 Agent 名返回预设输出，用于验收与测试。"""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.overrides = overrides or {}

    def _agent_name(self, system: str) -> str:
        for name in (
            "explorer",
            "planner",
            "executor",
            "inspector",
            "visualizer",
            "reporter",
            "critic",
        ):
            if re.search(rf"\b{name}\b", system, re.IGNORECASE):
                return name
        return "default"

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
        defaults = {
            "planner": '{"question": "", "time_base": null, "tasks": []}',
            "inspector": '{"task_id": 1, "status": "PASS", "checks": [], "suggestion": null}',
            "critic": '{"verdict": "PASS", "rounds": 1, "issues": []}',
        }
        if name in defaults:
            return defaults[name]
        return json.dumps(
            {"mock": True, "agent": name, "messages_count": len(messages)},
            ensure_ascii=False,
        )
