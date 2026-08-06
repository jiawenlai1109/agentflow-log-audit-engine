"""BaseAgent 基类：统一 LLM 调用、结构化输出校验、预算、日志。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from agentflow.core.llm import BaseLLM, LLMError
from agentflow.core.messages import AgentMessage


class BaseAgent(ABC):
    """所有角色 Agent 的基类。

    子类需定义：name、system_prompt、allowed_tools、output_schema、temperature、max_tokens。
    """

    name: str = "base"
    system_prompt: str = ""
    output_schema: type[BaseModel] | None = None
    temperature: float = 0.2
    max_tokens: int = 2000

    def __init__(
        self,
        llm: BaseLLM,
        config: dict[str, Any] | None = None,
        transcript: Any = None,
        budget: Any = None,
    ) -> None:
        self.llm = llm
        self.config = config or {}
        self.transcript = transcript
        self.budget = budget
        agent_cfg = self.config.get("agents", {}).get(self.name, {})
        self.temperature = float(agent_cfg.get("temperature", self.temperature))
        self.max_tokens = int(agent_cfg.get("max_tokens", self.max_tokens))

    @abstractmethod
    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        """处理输入消息，返回结构化输出消息。"""

    def complete_structured(
        self,
        ctx: Any,
        schema: type[BaseModel],
        user_content: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> BaseModel:
        """统一结构化 LLM 调用：预算 → 调用（含校验重试）→ 日志。"""
        if self.budget is not None and not self.budget.spend():
            raise LLMError(f"LLM 调用预算耗尽（上限 {self.budget.limit} 次）")
        result = self.llm.complete_structured(
            system=system_prompt or self.system_prompt,
            messages=[{"role": "user", "content": user_content}],
            schema=schema,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
        )
        self._transcribe(
            ctx,
            kind=f"{self.name}_llm",
            content=user_content,
            result=result.model_dump(mode="json"),
        )
        return result

    def reply(
        self,
        ctx: Any,
        receiver: str,
        kind: str,
        content: str,
        artifacts: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentMessage:
        """构造并记录输出消息。"""
        message = AgentMessage(
            run_id=ctx.run_id,
            sender=self.name,
            receiver=receiver,
            kind=kind,
            content=content,
            artifacts=artifacts or [],
            metadata=metadata or {},
        )
        self._transcribe(ctx, kind=kind, content=content, result=message.to_dict())
        return message

    def _transcribe(
        self,
        ctx: Any,
        kind: str,
        content: str,
        result: Any,
    ) -> None:
        if ctx.transcript is not None:
            ctx.transcript.write(
                {
                    "run_id": ctx.run_id,
                    "sender": self.name,
                    "kind": kind,
                    "input": content[:1000],
                    "output": result,
                    "budget_used": getattr(ctx.budget, "used", None),
                }
            )
