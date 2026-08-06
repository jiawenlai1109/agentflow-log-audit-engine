"""工具系统：统一 ToolRegistry + 能力授权（grants）+ 路径守卫。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class ToolError(RuntimeError):
    """工具调用失败。"""


class PathViolationError(ToolError):
    """路径越界：请求路径不在授权根目录内。"""


def ensure_within(root: Path, path: str | Path) -> Path:
    """校验 path 位于 root 之内（解析后），拒绝 .. 与越界绝对路径。"""
    root = Path(root).resolve()
    raw = Path(path)
    target = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if target != root and root not in target.parents:
        raise PathViolationError(f"路径越界：{path} 不在 {root} 内")
    return target


@dataclass
class Tool:
    """工具定义：名称、描述、参数 JSON Schema、处理函数、资源授权。"""

    name: str
    description: str
    handler: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    grants: list[str] = field(default_factory=list)


class ToolRegistry:
    """统一工具注册表：实现一次，按 Agent 白名单注入可见性。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"未注册的工具：{name}") from exc

    def allowed_tools(
        self, agent_name: str, config: dict[str, Any] | None = None
    ) -> list[Tool]:
        """按 config.agents.<agent>.tools 白名单返回该 Agent 可见的工具。"""
        whitelist: list[str] = []
        if config:
            whitelist = (config.get("agents", {}).get(agent_name, {}) or {}).get(
                "tools", []
            )
        return [self._tools[name] for name in whitelist if name in self._tools]

    def call(
        self,
        agent_name: str,
        tool_name: str,
        ctx: Any,
        **params: Any,
    ) -> Any:
        """调用工具；对所有带 path 语义的参数先做路径守卫。"""
        tool = self.get(tool_name)
        guarded: dict[str, Any] = {}
        for key, value in params.items():
            if key in {"path", "file", "dir", "file_path", "output_path"} and isinstance(
                value, (str, Path)
            ):
                guarded[key] = ensure_within(ctx.outputs_dir, value)
            else:
                guarded[key] = value
        return tool.handler(ctx=ctx, **guarded)


def _read_artifact(ctx: Any, path: str | Path) -> str:
    """示例工具：读取授权范围内的产物文件内容。"""
    target = ensure_within(ctx.outputs_dir, path)
    return target.read_text(encoding="utf-8")


def register_default_tools(registry: ToolRegistry) -> None:
    """注册核心基础工具（profile_csv / execute_python 在 Phase 2 注册）。"""
    registry.register(
        Tool(
            name="read_artifact",
            description="读取运行目录内授权产物的文件内容（路径必须位于 run 根目录内）",
            handler=_read_artifact,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    )
