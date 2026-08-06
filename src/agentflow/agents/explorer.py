"""ExplorerAgent：数据探查员（确定性画像 + 可选 LLM 归纳）。"""

from __future__ import annotations

import json
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core.messages import AgentMessage


class ExplorerAgent(BaseAgent):
    name = "explorer"
    system_prompt = "你是资深数据探查员：负责输出真实、准确的表结构画像（SchemaProfile），列名以实际数据为准。"

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        data_path = message.metadata.get("data_path") or ctx.data_path
        profile = self.registry.call(self.name, "profile_csv", ctx, data_path=data_path)
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="schema_profile",
            content=json.dumps(profile, ensure_ascii=False),
            metadata={"row_count": profile["row_count"]},
        )
