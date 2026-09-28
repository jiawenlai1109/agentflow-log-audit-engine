"""MCP client adapter（M4-C 骨架）：把外部 server 的工具挂进**现有** ToolRegistry。

命名空间：`mcp:<server>:<tool>`。之所以走同一张注册表而不是另开一条通道，是因为本项目
已经有四条强制链——白名单、参数/路径守卫、审计（transcript）、预算。另开一条就等于
给自己写一份"绕过所有闸门的捷径"。

骨架覆盖 / 未覆盖（诚实边界，见优化总纲 M4）：
- 覆盖：`initialize` / `tools/list` / `tools/call`、行分隔 JSON-RPC 2.0 over stdio、
  调用超时、能力分级与人在环审批、默认零文件系统、出站数据标记、外部结果按不可信数据处理。
- 未覆盖：resources / prompts / sampling 三类 server 能力、进度与取消通知、会话恢复、
  streamable-HTTP 传输、与真实 npm server 的互操作实测。补这些时**不许**新增执行通道，
  仍然只能落到 `mcp:<server>:<tool>` 上。

三条安全线：
1. **外部结果按不可信数据处理**：返回值统一带 `untrusted=True`，且默认 `evidence_only`
   ——只进证据、不进数字来源池（I1）。能力分级的权威口径在本地配置里，
   server 自述只用来"往更严的方向修正"，不用来放宽。
2. **默认零文件系统权限**：任何路径类入参一律拒，除非 server 配置里按键显式放行。
3. **出站数据标记**：每次调用记下"哪些参数、什么形状、发给了哪个 server"，事后能回答。

闸门顺序本身是设计的一部分：被拒的调用**不会发出**，所以"MCP 越权"最坏也只是多一条审计
记录，而不是一个已经发生的外部副作用。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# core/mcp.py → agentflow → src → 项目根
DEFAULT_MCP_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "mcp.yaml"
PROJECT_ROOT = Path(__file__).resolve().parents[3]

TIERS = ("read", "compute", "write", "network")
APPROVAL_TIERS = frozenset({"write", "network"})  # 这两级必须人在环批准，缺批准即拒
TOOL_NAMESPACE = "mcp"

# 外部 server 拿到一个路径参数，就可能读到我们授权之外的东西——所以这里用于"拒绝"，
# 与 core/tools.py 里用于"放行校验"的同名清单是两件不同的事。
PATHISH_KEYS = frozenset(
    {
        "path",
        "file",
        "dir",
        "filename",
        "file_path",
        "output_path",
        "data_path",
        "work_dir",
        "result_path",
        "url",
        "uri",
        "command",
        "cwd",
    }
)


class McpError(RuntimeError):
    """MCP 调用失败（传输、协议或 server 侧报错）。"""


class McpDeniedError(McpError):
    """被本地闸门拒绝：这类调用没有发到 server，因此不可能已产生外部副作用。"""


class McpApprovalRequired(McpDeniedError):
    """write / network 能力缺少人在环批准。"""


def _tier_rank(tier: Any) -> int:
    try:
        return TIERS.index(str(tier))
    except ValueError:
        return len(TIERS)  # 不认识的分级按最严处理


@dataclass
class McpToolSpec:
    """一个外部工具在本地的登记信息。tier / evidence_only 是**本地策略**，不是 server 自述。"""

    name: str
    description: str = ""
    tier: str = "read"
    evidence_only: bool = True
    allow_path_params: tuple[str, ...] = ()
    server_declared_tier: str = ""

    def effective_tier(self) -> str:
        """本地分级与 server 自述取更严的一侧：server 说"我只是个读工具"不构成降级依据。"""
        declared = self.server_declared_tier
        if not declared:
            return self.tier
        if declared not in TIERS:
            # 看不懂的自述按最严一档处理：`APPROVAL_TIERS` 判的是字符串是否等于
            # write/network，返回原文等于让一个自述为 "trust-me" 的工具绕过人在环闸门。
            return TIERS[-1]
        return self.tier if _tier_rank(self.tier) >= _tier_rank(declared) else declared


@dataclass
class McpServerSpec:
    name: str
    command: list[str]
    tools: list[McpToolSpec] = field(default_factory=list)
    allowed_agents: tuple[str, ...] = ()
    timeout_seconds: int = 10
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)

    def tool(self, name: str) -> McpToolSpec | None:
        return next((tool for tool in self.tools if tool.name == name), None)

    def qualified(self, tool_name: str) -> str:
        return f"{TOOL_NAMESPACE}:{self.name}:{tool_name}"


@dataclass
class EvidencePull:
    """配置声明的确定性消费点：跑哪个场景包时、由哪个角色、向哪个 server 取什么证据。

    SQL 由配置给出而非模型生成——否则"接入外部数据"就变成"把写 SQL 的权力交给模型，
    再交给一个不受我们白名单约束的执行环境"。
    """

    server: str
    agent: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    when_pack: str = ""
    label: str = ""


@dataclass
class McpConfig:
    servers: list[McpServerSpec]
    pulls: list[EvidencePull] = field(default_factory=list)
    max_calls: int = 5
    approvals: dict[str, bool] = field(default_factory=dict)

    def server(self, name: str) -> McpServerSpec | None:
        return next((item for item in self.servers if item.name == name), None)

    def pulls_for(self, pack_name: str | None, agent_name: str) -> list[EvidencePull]:
        return [
            pull
            for pull in self.pulls
            if pull.agent == agent_name and (not pull.when_pack or pull.when_pack == (pack_name or ""))
        ]


def load_mcp_config(path: str | Path | None = None) -> McpConfig | None:
    """读 `config/mcp.yaml`。文件不存在 = 一个外部 server 都不接（返回 None，不报错）。"""
    import yaml

    config_path = Path(path) if path is not None else DEFAULT_MCP_CONFIG_PATH
    if not config_path.exists():
        return None
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    servers: list[McpServerSpec] = []
    for entry in data.get("servers") or []:
        tools = [
            McpToolSpec(
                name=str(tool["name"]),
                description=str(tool.get("description", "")),
                tier=str(tool.get("tier", "read")),
                evidence_only=bool(tool.get("evidence_only", True)),
                allow_path_params=tuple(str(k) for k in (tool.get("allow_path_params") or [])),
            )
            for tool in (entry.get("tools") or [])
        ]
        for tool in tools:
            if tool.tier not in TIERS:
                raise ValueError(
                    f"mcp server {entry.get('name')} 的工具 {tool.name} 分级非法：{tool.tier}"
                    f"（只认 {list(TIERS)}）"
                )
        servers.append(
            McpServerSpec(
                name=str(entry["name"]),
                command=[str(part) for part in (entry.get("command") or [])],
                tools=tools,
                allowed_agents=tuple(str(a) for a in (entry.get("allowed_agents") or [])),
                timeout_seconds=int(entry.get("timeout_seconds", 10)),
                # 默认工作目录=仓库根：server 的相对路径参数（如 --db demo/data/x.sqlite）
                # 需要一个确定的基准，否则"换台机器跑不起来"会被误诊成外部数据源坏了。
                cwd=str(entry.get("cwd") or PROJECT_ROOT),
                env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            )
        )
    pulls = [
        EvidencePull(
            server=str(item["server"]),
            agent=str(item["agent"]),
            tool=str(item["tool"]),
            arguments=dict(item.get("arguments") or {}),
            when_pack=str(item.get("when_pack") or ""),
            label=str(item.get("label") or ""),
        )
        for item in (data.get("evidence_pulls") or [])
    ]
    limits = data.get("limits") or {}
    return McpConfig(
        servers=servers,
        pulls=pulls,
        max_calls=int(limits.get("max_calls", 5)),
        approvals={str(k): bool(v) for k, v in (data.get("approvals") or {}).items()},
    )


class StdioMcpClient:
    """行分隔 JSON-RPC 2.0 over stdio 的最小 MCP 客户端。

    响应读取放在守护线程 + 带超时的队列取：server 卡住时调用方拿到的是超时异常，
    而不是一次永远不返回的 `readline()`（那会吃掉整条 run 的墙钟）。
    """

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, spec: McpServerSpec, timeout_seconds: int | None = None) -> None:
        self.spec = spec
        self.timeout = int(timeout_seconds or spec.timeout_seconds)
        self._proc: subprocess.Popen[Any] | None = None
        self._queue: Any = None
        self._id = 0
        self._lock = threading.Lock()

    @property
    def started(self) -> bool:
        return self._proc is not None

    def start(self) -> None:
        if self._proc is not None:
            return
        if not self.spec.command:
            raise McpError(f"mcp server {self.spec.name} 没有 command（骨架只支持 stdio 子进程）")
        import os
        from queue import Queue

        # 两个占位符：{python}=当前解释器绝对路径，{root}=仓库根。
        # 写死机器相关路径的配置在另一台机器上会以"server 起不来"的形式失败，
        # 而那看起来像外部数据源的问题——占位符让失败至少是可诊断的。
        command = [
            part.replace("{python}", sys.executable).replace("{root}", str(PROJECT_ROOT))
            for part in self.spec.command
        ]
        env = {**os.environ, **self.spec.env, "PYTHONUTF8": "1"}
        proc = subprocess.Popen(  # noqa: S603 - 命令来自仓库配置，不来自模型输出
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.spec.cwd or None,
            env=env,
        )
        queue: Queue[Any] = Queue()
        reader = threading.Thread(target=self._pump, args=(proc, queue), daemon=True)
        reader.start()
        self._proc, self._queue = proc, queue
        self.request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentflow", "version": "0.4"},
            },
        )
        self.notify("notifications/initialized", {})

    @staticmethod
    def _pump(proc: subprocess.Popen[Any], queue: Any) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        for line in stdout:
            line = line.strip()
            if not line:
                continue
            try:
                queue.put(json.loads(line))
            except json.JSONDecodeError:
                queue.put({"error": {"code": -32700, "message": "server 输出不是合法 JSON"}})
        # 管道结束后必须唤醒等待者：否则调用方会一直等到超时，把墙钟白吃掉
        queue.put({"__pipe_closed__": True})

    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError(f"mcp server {self.spec.name} 未启动")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> Any:
        if self._proc is None:
            self.start()
        with self._lock:
            self._id += 1
            request_id = self._id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"mcp server {self.spec.name} 调用 {method} 超时（{self.timeout}s）")
            message = self._poll(min(remaining, 0.2))
            if message is None:
                continue
            if message.get("__pipe_closed__"):
                raise McpError(self._dead_server_message())
            if message.get("id") != request_id:
                continue  # 通知或别的请求的回包：不匹配就继续等自己那条
            if "error" in message:
                error = message["error"] or {}
                raise McpError(
                    f"mcp server {self.spec.name} 的 {method} 报错 "
                    f"{error.get('code')}：{str(error.get('message'))[:200]}"
                )
            return message.get("result")

    def _poll(self, timeout: float) -> dict[str, Any] | None:
        from queue import Empty

        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def _dead_server_message(self) -> str:
        proc = self._proc
        code = proc.poll() if proc is not None else None
        stderr = ""
        if proc is not None and proc.stderr is not None:
            try:
                stderr = proc.stderr.read() or ""
            except Exception:  # noqa: BLE001 - 诊断信息读不到不该改变结论
                stderr = ""
        return (
            f"mcp server {self.spec.name} 已退出（code={code}）：{stderr[:300]}"
            if stderr
            else f"mcp server {self.spec.name} 连接已关闭（code={code}）"
        )

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {}) or {}
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.request("tools/call", {"name": name, "arguments": dict(arguments)}) or {}
        text = _text_of(result)
        if result.get("isError"):
            raise McpError(f"mcp tool {self.spec.name}:{name} 执行失败：{text[:300]}")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"text": text}

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - 关不掉就杀，别把测试挂住
            proc.kill()


def _text_of(result: dict[str, Any]) -> str:
    blocks = result.get("content") or []
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        return str(blocks[0].get("text", ""))
    return str(result.get("text", ""))


def _mark(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _is_pathish(value: Any) -> bool:
    text = str(value)
    if not text or len(text) > 500:
        return False
    return ("\\" in text or "/" in text) and bool(Path(text).suffix)


class McpHub:
    """一次运行的 MCP 门面：持有 client、跑闸门、审计出站调用、累计外部调用预算。"""

    def __init__(
        self,
        config: McpConfig,
        registry: Any = None,
        transcript: Any = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.transcript = transcript
        self.clients: dict[str, StdioMcpClient] = {}
        self.calls: list[dict[str, Any]] = []
        self.denials: list[dict[str, Any]] = []
        self.external_evidence: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._used = 0

    # ------------------------------------------------------------ 生命周期
    def client(self, spec: McpServerSpec) -> StdioMcpClient:
        with self._lock:
            client = self.clients.get(spec.name)
            if client is None:
                client = StdioMcpClient(spec)
                self.clients[spec.name] = client
        if not client.started:
            client.start()
            # 建连放在锁外：子进程启动不该阻塞别的角色
            self._reconcile_tiers(spec, client)
        return client

    def close(self) -> None:
        with self._lock:
            clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            client.close()

    def _reconcile_tiers(self, spec: McpServerSpec, client: StdioMcpClient) -> None:
        """把 server 自述的能力分级读回来：只用来**收紧**，永远不用来放宽。

        读不到（server 没实现 tools/list、或压根没声明 tier）就当它没说——此时以本地配置为准。
        一次对不上就留一条审计，因为"外部声称自己是只读工具"这件事本身值得被看见。
        """
        try:
            listed = client.list_tools()
        except McpError as error:
            self._audit({"event": "mcp_tools_list_failed", "server": spec.name, "error": str(error)[:200]})
            return
        declared: dict[str, str] = {}
        for item in listed:
            name = str(item.get("name") or "")
            tier = str((item.get("annotations") or {}).get("tier") or item.get("tier") or "")
            if name and tier:
                declared[name] = tier
        with self._lock:
            for tool in spec.tools:
                if tool.name in declared:
                    tool.server_declared_tier = declared[tool.name]
        for name, tier in sorted(declared.items()):
            spec_tool = spec.tool(name)
            if spec_tool is None:
                self._audit(
                    {
                        "event": "mcp_tool_not_registered",
                        "server": spec.name,
                        "tool": name,
                        "tier": tier,
                        "reason": "server 提供了配置里没登记的工具，不注册=不可调用",
                    }
                )

    def _audit(self, record: dict[str, Any]) -> None:
        if self.transcript is not None:
            self.transcript.write(record)

    # ------------------------------------------------------------ 调用
    def invoke(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        agent_name: str = "unknown",
        approvals: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        spec = self.config.server(server_name)
        if spec is None:
            raise McpDeniedError(f"未登记的 mcp server：{server_name}（配置之外的 server 一律不接）")
        tool = spec.tool(tool_name)
        if tool is None:
            raise McpDeniedError(f"mcp server {server_name} 未登记工具 {tool_name}")
        qualified = spec.qualified(tool_name)

        granted = set(self.registry.whitelist_for(agent_name)) if self.registry is not None else set()
        if qualified not in granted:
            raise self._deny(
                "mcp_denied_whitelist",
                agent=agent_name,
                tool=qualified,
                reason="该角色的工具白名单里没有这个 mcp 工具（交给 registry 拒并留痕）",
            )
        if agent_name not in spec.allowed_agents:
            raise self._deny(
                "mcp_denied_agent",
                agent=agent_name,
                tool=qualified,
                reason="server 侧声明的 allowed_agents 不含该角色",
            )

        tier = tool.effective_tier()
        approved = dict(self.config.approvals)
        approved.update(approvals or {})
        if tier in APPROVAL_TIERS and not approved.get(qualified, False):
            raise self._deny(
                "mcp_denied_capability",
                error_cls=McpApprovalRequired,
                agent=agent_name,
                tool=qualified,
                tier=tier,
                reason=f"{tier} 能力需要人在环批准（approvals 里给出 {qualified}=true）后才可调",
            )

        blocked = sorted(
            key
            for key, value in arguments.items()
            if (key in PATHISH_KEYS or _is_pathish(value)) and key not in tool.allow_path_params
        )
        if blocked:
            raise self._deny(
                "mcp_denied_filesystem",
                agent=agent_name,
                tool=qualified,
                params=blocked,
                reason="默认零文件系统权限：路径类入参必须先在 server 配置里按键放行",
            )

        with self._lock:
            if self._used >= self.config.max_calls:
                over = self._used + 1
            else:
                self._used += 1
                over = 0
        if over:
            raise self._deny(
                "mcp_denied_budget",
                agent=agent_name,
                tool=qualified,
                used=over,
                limit=self.config.max_calls,
                reason="外部调用预算已触顶（与 LLM 预算分列，两条都得有上界）",
            )

        self._audit(
            {
                "event": "mcp_outbound",
                "agent": agent_name,
                "server": server_name,
                "tool": tool_name,
                "tier": tier,
                # 出站数据标记：发了哪些键、键值长什么样（截断），事后能回答"我们往外发了什么"
                "argument_keys": sorted(arguments),
                "arguments_preview": {k: str(v)[:120] for k, v in arguments.items()},
                "payload_sha": _mark(arguments),
            }
        )
        started = time.monotonic()
        try:
            payload = self.client(spec).call_tool(tool_name, arguments)
        except McpError as error:
            self._audit(
                {
                    "event": "mcp_error",
                    "server": server_name,
                    "tool": tool_name,
                    "error": str(error)[:300],
                    "ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
            raise
        columns, rows = _shape_of(payload)
        record = {
            "event": "mcp_result",
            "agent": agent_name,
            "server": server_name,
            "tool": tool_name,
            "tier": tier,
            "untrusted": True,  # 外部结果一律按不可信数据处理
            "evidence_only": tool.evidence_only,
            "rows": len(rows),
            "columns": columns,
            "sha256": _mark(payload),
            "ms": round((time.monotonic() - started) * 1000, 1),
        }
        self._audit(record)
        with self._lock:
            self.calls.append(dict(record, arguments_preview={k: str(v)[:120] for k, v in arguments.items()}))
            if tool.evidence_only:
                self.external_evidence.append(dict(record))
        return {"untrusted": True, "evidence_only": tool.evidence_only, "source": qualified, "result": payload}

    def _deny(
        self, event: str, error_cls: type[McpDeniedError] = McpDeniedError, **item: Any
    ) -> McpDeniedError:
        """留痕与抛错绑在一个动作里：先写审计，再返回异常给调用方 raise。

        异常类型也按闸门分（审批类抛 McpApprovalRequired）——调用方要能只捕这一类，
        把"缺人签字"与"根本越权"混成同一个错误，就等于把两种不同的处置混在一起。
        """
        entry = {"event": event, **item}
        with self._lock:
            self.denials.append(entry)
        self._audit(entry)
        return error_cls(f"{event}: {item.get('tool')} {item.get('reason', '')}")

    def summary(self) -> dict[str, Any]:
        return {
            "servers": [
                {
                    "name": spec.name,
                    "allowed_agents": list(spec.allowed_agents),
                    "tools": [
                        {
                            "name": tool.name,
                            "tier": tool.effective_tier(),
                            "declared_tier": tool.tier,
                            "server_declared_tier": tool.server_declared_tier,
                            "evidence_only": tool.evidence_only,
                        }
                        for tool in spec.tools
                    ],
                }
                for spec in self.config.servers
            ],
            "calls": self.calls,
            "denials": self.denials,
            "external_evidence": self.external_evidence,
            "used": self._used,
            "max_calls": self.config.max_calls,
        }


def _shape_of(payload: Any) -> tuple[list[str], list[Any]]:
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return list(payload.get("columns") or rows[0].keys()), rows
        return sorted(str(key) for key in payload), []
    if isinstance(payload, list):
        columns = list(payload[0].keys()) if payload and isinstance(payload[0], dict) else []
        return columns, payload
    return [], []


def attach_tools(
    registry: Any,
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    transcript: Any = None,
) -> McpHub | None:
    """把配置里的 mcp 工具注册成 `mcp:<server>:<tool>` 进现有 ToolRegistry。

    注册即受管：`ToolRegistry.call()` 的白名单校验、参数守卫、审计、错误路由全部照旧生效。
    本函数只负责"挂上去"，不负责另建一条通道。
    """
    mcp_config = load_mcp_config(config_path)
    if mcp_config is None or not mcp_config.servers:
        return None
    hub = McpHub(mcp_config, registry=registry, transcript=transcript)
    for spec in mcp_config.servers:
        for tool in spec.tools:
            _register_tool(registry, hub, spec, tool)
    return hub


def _register_tool(registry: Any, hub: McpHub, spec: McpServerSpec, tool: McpToolSpec) -> None:
    server_name, tool_name = spec.name, tool.name

    def handler(ctx: Any = None, **arguments: Any) -> dict[str, Any]:
        agent = registry.current_agent() if hasattr(registry, "current_agent") else "unknown"
        approvals = dict(getattr(ctx, "mcp_approvals", None) or {})
        return hub.invoke(server_name, tool_name, arguments, agent_name=agent, approvals=approvals)

    registry.register_mcp_tool(
        name=spec.qualified(tool_name),
        description=f"[MCP {server_name}] {tool.description or tool_name}",
        handler=handler,
        tier=tool.effective_tier(),
        parameters={"type": "object", "properties": {}},
    )
