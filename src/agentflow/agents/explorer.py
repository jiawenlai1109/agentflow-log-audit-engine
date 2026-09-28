"""ExplorerAgent：数据探查员（确定性画像 + 可选 LLM 归纳 + 外部证据拉取）。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agentflow.agents.base import BaseAgent
from agentflow.core.messages import AgentMessage
from agentflow.core.prompts import load_prompt


class ExplorerAgent(BaseAgent):
    name = "explorer"
    system_prompt = load_prompt("explorer")

    def run(self, ctx: Any, message: AgentMessage) -> AgentMessage:
        profile = self.registry.call(self.name, "profile_bundle", ctx)
        self._pull_external_evidence(ctx)
        return self.reply(
            ctx,
            receiver="orchestrator",
            kind="schema_profile",
            content=json.dumps(profile, ensure_ascii=False),
            metadata={"row_count": profile["row_count"], "tables": len(profile.get("tables") or [1])},
        )

    def _pull_external_evidence(self, ctx: Any) -> None:
        """按配置拉取外部证据（M4-C 的确定性消费点）。

        三条约束就是这个函数存在的原因：
        ① SQL 来自 `config/mcp.yaml` 而不是模型——否则"接入外部数据"等于把写 SQL 的权力
           交给模型，再交给一个不受我们白名单约束的执行环境；
        ② 取回来的东西**只落 artifacts/ 当证据**，不进画像、不进 prompt、不进报告，
           所以报告一旦引用它就是数字追溯率判红（I1：外部数据只能作证据，不能作数字来源）；
        ③ 拉取失败不许拖垮本次分析——它是可选证据，不是必需输入，但失败原因必须留痕。
        """
        hub = getattr(ctx, "mcp", None)
        if hub is None:
            return
        pack_name = getattr(getattr(ctx, "pack", None), "name", None)
        for pull in hub.config.pulls_for(pack_name, self.name):
            qualified = f"mcp:{pull.server}:{pull.tool}"
            entry: dict[str, Any] = {
                "server": pull.server,
                "tool": pull.tool,
                "tool_name": qualified,
                "label": pull.label or qualified,
                "untrusted": True,
            }
            try:
                outcome = self.registry.call(self.name, qualified, ctx, **pull.arguments)
                payload = (outcome or {}).get("result") or {}
            except RuntimeError as error:  # ToolError / McpDeniedError / McpError 都算
                reason = str(error)[:300]
                if ctx.transcript is not None:
                    ctx.transcript.write({"event": "mcp_evidence_failed", "tool": qualified, "reason": reason})
                ctx.external_evidence.append({**entry, "status": "failed", "reason": reason})
                continue
            rows = payload.get("rows") if isinstance(payload, dict) else None
            rows = rows if isinstance(rows, list) else []
            columns = list(
                payload.get("columns")
                or (rows[0].keys() if rows and isinstance(rows[0], dict) else [])
            )
            artifact = ctx.artifacts_dir / f"external_{pull.server}_{pull.tool}.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                json.dumps(
                    {
                        "untrusted": True,
                        "evidence_only": True,
                        "source": qualified,
                        "query": str(pull.arguments.get("sql", "")),
                        "columns": [str(column) for column in columns],
                        "rows": rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            ctx.external_evidence.append(
                {
                    **entry,
                    "status": "ok",
                    "rows": len(rows),
                    "columns": [str(column) for column in columns],
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()[:12],
                    "path": str(artifact),
                }
            )
