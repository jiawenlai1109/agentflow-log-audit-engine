"""M4-C：MCP 骨架——外部工具必须走同一条强制链。

验收判据是"**MCP 越权工具调用被 registry 拒并留审计**"。这个文件把它拆成能逐个跑红的断言：
被拒的调用不许发出去、发出去的调用必须有出站标记、外部结果一律不可信且只作证据。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentflow.core.config import load_config  # noqa: E402
from agentflow.core.grading import _p_external_evidence, numbers_traceable  # noqa: E402
from agentflow.core.mcp import (  # noqa: E402
    APPROVAL_TIERS,
    McpApprovalRequired,
    McpDeniedError,
    McpError,
    McpHub,
    McpToolSpec,
    TIERS,
    attach_tools,
    load_mcp_config,
)
from agentflow.core.tools import ToolError, build_default_registry  # noqa: E402

INTEL_DB = ROOT / "demo" / "data" / "soc_intel.sqlite"
TRIAGE = ROOT / "demo" / "data" / "triage"
SERVER_MODULE = "agentflow.mcp_servers.sqlite_server"


class FakeTranscript:
    def __init__(self):
        self.entries = []

    def write(self, record):
        self.entries.append(record)

    def events(self):
        return [entry.get("event") for entry in self.entries]

    def find(self, event):
        return [entry for entry in self.entries if entry.get("event") == event]


def _config(**overrides):
    """现造一份 McpConfig：默认指向仓库里那只真 server 与真库，改哪个键就覆盖哪个。"""
    base = {
        "servers": [
            {
                "name": "soc_intel",
                "command": [
                    "{python}",
                    "-m",
                    SERVER_MODULE,
                    "--db",
                    str(INTEL_DB),
                ],
                "timeout_seconds": 20,
                "allowed_agents": ["explorer"],
                "tools": [
                    {"name": "query", "tier": "read"},
                    {"name": "list_tables", "tier": "read"},
                    {"name": "write_note", "tier": "write"},
                ],
            }
        ],
        "limits": {"max_calls": 5},
        "approvals": {},
    }
    config = dict(base)
    config.update(overrides)
    import yaml

    path = Path(_config.tmpdir) / "mcp.yaml"  # type: ignore[attr-defined]
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return load_mcp_config(path)


@pytest.fixture(autouse=True)
def _tmpdir(tmp_path):
    _config.tmpdir = str(tmp_path)
    yield


def _registry_with(config):
    """按这份 mcp 配置造一个 registry：把 server 授权给谁，就在谁的白名单里挂上对应工具名。

    生产里这件事写在 `config/agents.yaml`。测试直接调 hub.invoke 时也必须带上同一份白名单，
    否则"hub 自己也会查白名单"这条纵深防御就永远走不到，测出来的是假的。
    """
    import copy

    from agentflow.core.tools import build_default_registry as build

    cfg = copy.deepcopy(load_config(None))
    agents = cfg.setdefault("agents", {})
    for spec in config.servers:
        for agent in spec.allowed_agents:
            entry = dict(agents.get(agent) or {})
            tools = list(entry.get("tools") or build({}).whitelist_for(agent))
            entry["tools"] = tools + [spec.qualified(tool.name) for tool in spec.tools]
            agents[agent] = entry
    return build(cfg)


def _hub(config=None, registry=None, transcript=None) -> McpHub:
    cfg = config if config is not None else _config()
    hub = McpHub(
        cfg,
        registry=registry if registry is not None else _registry_with(cfg),
        transcript=transcript,
    )
    _hub.instances.append(hub)
    return hub


_hub.instances = []


@pytest.fixture(autouse=True)
def _close_hubs():
    yield
    for hub in _hub.instances:
        hub.close()
    _hub.instances.clear()


# ---------------------------------------------------------------- 真实子进程往返


def test_real_stdio_roundtrip_reads_the_external_database():
    """行分隔 JSON-RPC over stdio 走通一次：这一步不成立，后面所有断言都是空的。"""
    hub = _hub()
    outcome = hub.invoke("soc_intel", "query", {"sql": "SELECT host, score FROM intel ORDER BY host"}, agent_name="explorer")
    rows = outcome["result"]["rows"]
    assert len(rows) == 6, "6 行外部情报"
    assert {row["score"] for row in rows} >= {4242}, "独特数字在场：它进报告即违反 I1"
    assert outcome["untrusted"] is True and outcome["evidence_only"] is True
    hub.close()
    # 关掉之后子进程必须真的没了：不关就是每跑一次漏一个进程
    assert hub.clients == {}


def test_server_only_offers_write_tool_when_explicitly_given_a_writable_database(tmp_path):
    database = tmp_path / "note.db"
    sqlite3.connect(database).close()
    config = _config(
        servers=[
            {
                "name": "db",
                "command": ["{python}", "-m", SERVER_MODULE, "--db", str(INTEL_DB), "--writable-db", str(database)],
                "allowed_agents": ["explorer"],
                "tools": [{"name": "write_note", "tier": "write"}],
            }
        ],
        approvals={"mcp:db:write_note": True},  # 人在环批准已给出：这一条测的是"批了就能真做"
    )
    hub = _hub(config)
    outcome = hub.invoke("db", "write_note", {"host": "10.0.0.15", "note": "已复核"}, agent_name="explorer")
    assert outcome["result"]["inserted"] is True
    conn = sqlite3.connect(str(database))
    try:
        assert conn.execute("SELECT COUNT(*) FROM triage_note").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_server_that_never_answers_fails_within_the_timeout_not_hangs_the_run():
    config = _config(
        servers=[
            {
                "name": "silent",
                "command": ["{python}", "-c", "import time; time.sleep(30)"],
                "timeout_seconds": 2,
                "allowed_agents": ["explorer"],
                "tools": [{"name": "query", "tier": "read"}],
            }
        ]
    )
    hub = _hub(config)
    import time

    started = time.monotonic()
    with pytest.raises(McpError):
        hub.invoke("silent", "query", {"sql": "SELECT 1"}, agent_name="explorer")
    assert time.monotonic() - started < 8, "超时上限要真生效"


def test_garbage_from_a_server_becomes_an_error_not_a_silent_empty_result():
    config = _config(
        servers=[
            {
                "name": "junk",
                "command": ["{python}", "-c", "print('this is not json'); import time; time.sleep(3)"],
                "timeout_seconds": 4,
                "allowed_agents": ["explorer"],
                "tools": [{"name": "query", "tier": "read"}],
            }
        ]
    )
    hub = _hub(config)
    with pytest.raises(McpError):
        hub.invoke("junk", "query", {"sql": "SELECT 1"}, agent_name="explorer")


# ---------------------------------------------------------------- 越权与审批


def test_unregistered_server_is_refused_before_anything_is_started():
    hub = _hub()
    with pytest.raises(McpDeniedError, match="未登记的 mcp server"):
        hub.invoke("evil", "query", {}, agent_name="explorer")
    assert hub.clients == {}, "配置之外的 server 连进程都不该起"


def test_unregistered_tool_on_a_known_server_is_refused():
    hub = _hub()
    with pytest.raises(McpDeniedError, match="未登记工具"):
        hub.invoke("soc_intel", "drop_table", {}, agent_name="explorer")


def test_role_outside_the_whitelist_is_denied_by_the_registry_and_audited():
    """验收判据：越权的 MCP 工具调用被 registry 拒掉，并且留下审计。"""
    transcript = FakeTranscript()
    registry = build_default_registry(load_config(None))
    hub = attach_tools(registry, load_config(None), transcript=transcript)
    assert hub is not None
    assert "mcp:soc_intel:query" not in registry.whitelist_for("executor")
    with pytest.raises(ToolError, match="越权工具调用"):
        registry.call("executor", "mcp:soc_intel:query", SimpleCtx(transcript=transcript), sql="SELECT 1")
    denials = transcript.find("tool_denied_whitelist")
    assert denials and denials[0]["tool"] == "mcp:soc_intel:query" and denials[0]["agent"] == "executor"
    assert hub.clients == {}, "被拒的调用没有发到 server——最坏只是多一条审计"


class SimpleCtx:
    """最小的 ctx：只带 transcript 与 mcp_approvals，够 registry 走完强制链。"""

    def __init__(self, transcript):
        self.transcript = transcript
        self.mcp_approvals = {}
        self.outputs_dir = ROOT / "outputs"


def test_write_tier_without_approval_is_blocked_and_recorded():
    transcript = FakeTranscript()
    hub = _hub(transcript=transcript)
    with pytest.raises(McpApprovalRequired):
        hub.invoke("soc_intel", "write_note", {"host": "h", "note": "n"}, agent_name="explorer")
    denied = transcript.find("mcp_denied_capability")
    assert denied and denied[0]["tier"] == "write"
    assert hub.denials and hub.denials[0]["event"] == "mcp_denied_capability"
    assert hub.clients == {}, "没批准的写能力不能已经打进来"


def test_capability_tiers_are_a_closed_ladder_and_two_of_them_need_a_human():
    assert TIERS == ("read", "compute", "write", "network")
    assert APPROVAL_TIERS == frozenset({"write", "network"})


def test_server_self_report_can_only_tighten_never_loosen():
    """外部说"我只是个只读工具"不构成降级依据——分级口径取更严的一侧。"""
    strict = McpToolSpec(name="query", tier="write", server_declared_tier="read")
    assert strict.effective_tier() == "write"
    loosening = McpToolSpec(name="query", tier="read", server_declared_tier="network")
    assert loosening.effective_tier() == "network"
    assert McpToolSpec(name="query", tier="read").effective_tier() == "read"
    # 看不懂的自述按最严一档处理：返回原文会让 APPROVAL_TIERS 的字符串比较放过它
    assert McpToolSpec(name="q", tier="read", server_declared_tier="trust-me").effective_tier() == "network"


def test_path_like_arguments_are_refused_by_default(tmp_path):
    transcript = FakeTranscript()
    hub = _hub(transcript=transcript)
    with pytest.raises(McpDeniedError, match="mcp_denied_filesystem"):
        hub.invoke("soc_intel", "query", {"sql": "SELECT 1", "output_path": str(tmp_path / "x.csv")}, agent_name="explorer")
    assert "mcp_denied_filesystem" in transcript.events()
    with pytest.raises(McpDeniedError, match="mcp_denied_filesystem"):
        hub.invoke("soc_intel", "query", {"sql": "SELECT 1", "look": "D:/windows/path.csv"}, agent_name="explorer")
    assert hub.clients == {}, "默认零文件系统权限：这条也不能已经发出去"


def test_path_argument_is_allowed_only_when_declared_key_by_key(tmp_path):
    """按键放行才谈得上"默认关"：放行一个键不等于放行所有看起来像路径的东西。"""
    config = _config(
        servers=[
            {
                "name": "db",
                "command": ["{python}", "-m", SERVER_MODULE, "--db", str(INTEL_DB)],
                "allowed_agents": ["explorer"],
                "tools": [{"name": "query", "tier": "read", "allow_path_params": ["note_file"]}],
            }
        ]
    )
    hub = _hub(config)
    outcome = hub.invoke("db", "query", {"sql": "SELECT 1 AS ok", "note_file": "a/b.txt"}, agent_name="explorer")
    assert outcome["result"]["rows"] == [{"ok": 1}]
    with pytest.raises(McpDeniedError, match="mcp_denied_filesystem"):
        hub.invoke("db", "query", {"sql": "SELECT 1", "other_file": "a/b.txt"}, agent_name="explorer")


def test_external_call_budget_is_its_own_ceiling():
    config = _config(limits={"max_calls": 2})
    transcript = FakeTranscript()
    hub = _hub(config, transcript=transcript)
    for _ in range(2):
        hub.invoke("soc_intel", "list_tables", {}, agent_name="explorer")
    with pytest.raises(McpDeniedError, match="mcp_denied_budget"):
        hub.invoke("soc_intel", "list_tables", {}, agent_name="explorer")
    assert "mcp_denied_budget" in transcript.events()
    assert hub.summary()["used"] == 2, "被拒的调用不计入已用额度（它没花出去）"


# ---------------------------------------------------------------- 出站标记与不可信标注


def test_outbound_marking_records_what_left_the_building():
    transcript = FakeTranscript()
    hub = _hub(transcript=transcript)
    hub.invoke("soc_intel", "query", {"sql": "SELECT host FROM intel LIMIT 1"}, agent_name="explorer")
    outbound = transcript.find("mcp_outbound")
    assert len(outbound) == 1
    entry = outbound[0]
    assert entry["server"] == "soc_intel" and entry["tool"] == "query" and entry["tier"] == "read"
    assert entry["argument_keys"] == ["sql"], "出站数据标记：发了哪些键必须逐键记下来"
    assert "SELECT host" in entry["arguments_preview"]["sql"]
    assert len(entry["payload_sha"]) == 12
    result = transcript.find("mcp_result")[0]
    assert result["untrusted"] is True and result["evidence_only"] is True
    assert result["rows"] == 1 and result["columns"] == ["host"]


def test_registered_tool_names_are_three_part_and_marked_external():
    registry = build_default_registry(load_config(None))
    hub = attach_tools(registry, load_config(None))
    assert hub is not None
    assert registry.get("mcp:soc_intel:query").tier == "read"
    assert registry.is_mcp("mcp:soc_intel:query") and not registry.is_mcp("profile_bundle")
    from agentflow.core.tools import Tool

    with pytest.raises(ToolError, match="三段式"):
        registry.register_mcp_tool(name="execute_python2", description="", handler=lambda **kw: None)
    with pytest.raises(ToolError, match="三段式"):
        registry.register_mcp_tool(name="mcp:soc_intel", description="", handler=lambda **kw: None)


def test_absent_default_config_means_no_external_server(tmp_path, monkeypatch):
    """默认路径下没有 `config/mcp.yaml` = 一个外部 server 都不接（正常状态，不是错误）。"""
    from agentflow.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "DEFAULT_MCP_CONFIG_PATH", tmp_path / "absent.yaml")
    registry = build_default_registry(load_config(None))
    assert attach_tools(registry, load_config(None)) is None
    from agentflow.core.tools import ToolError

    with pytest.raises(ToolError, match="未注册的工具"):
        registry.get("mcp:soc_intel:query")


def test_explicitly_named_mcp_config_must_exist(tmp_path):
    """显式点名了外部 server 配置却读不到 ⇒ 报错，不能退成"什么都没接"。

    与 `load_config` 同一条规矩：否则"以为接了情报库"与"压根没接"在下游长成同一个样子
    （只是 external_evidence 为空），而这两种情况的处置完全不同。
    """
    registry = build_default_registry(load_config(None))
    with pytest.raises(FileNotFoundError, match="MCP 配置不存在"):
        attach_tools(registry, load_config(None), config_path=tmp_path / "typo.yaml")


def test_config_with_no_servers_is_not_an_error(tmp_path):
    registry = build_default_registry(load_config(None))
    empty = tmp_path / "empty.yaml"
    empty.write_text("servers: []\n", encoding="utf-8")
    assert attach_tools(registry, load_config(None), config_path=empty) is None


# ---------------------------------------------------------------- server 自带的下限


def test_server_side_sql_guard_blocks_writes_and_multi_statements():
    from agentflow.mcp_servers.sqlite_server import _check_read_sql

    assert _check_read_sql("SELECT 1;").startswith("SELECT 1")
    assert _check_read_sql("with t as (select 1 as a) select * from t").lower().startswith("with")
    for bad in ("DELETE FROM intel", "SELECT 1; DROP TABLE intel", "PRAGMA table_info(intel)", ""):
        with pytest.raises(ValueError):
            _check_read_sql(bad)


def test_server_forces_an_outer_limit():
    from agentflow.mcp_servers.sqlite_server import DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT

    assert DEFAULT_ROW_LIMIT <= MAX_ROW_LIMIT
    config = _config(
        servers=[
            {
                "name": "db",
                "command": ["{python}", "-m", SERVER_MODULE, "--db", str(INTEL_DB)],
                "tools": [{"name": "query", "tier": "read"}],
                "allowed_agents": ["explorer"],
            }
        ]
    )
    hub = _hub(config)
    payload = hub.invoke("db", "query", {"sql": "SELECT host FROM intel", "limit": 2}, agent_name="explorer")["result"]
    assert len(payload["rows"]) == 2 and payload["truncated"] is True
    with pytest.raises(McpError):  # server 侧 isError 回传，不当成成功空集
        hub.invoke("db", "query", {"sql": "DELETE FROM intel"}, agent_name="explorer")




# ---------------------------------------------------------------- 请求侧批准（第二入口）


def _write_db_config(tmp_path: Path, approvals: dict[str, bool], grantable: list[str]):
    """起一只真带 write 工具的 server，批准与可授予名单都按参数给。"""
    import yaml

    database = tmp_path / "note.db"
    sqlite3.connect(database).close()
    config = {
        "servers": [
            {
                "name": "db",
                "command": [
                    "{python}", "-m", SERVER_MODULE,
                    "--db", str(INTEL_DB), "--writable-db", str(database),
                ],
                "allowed_agents": ["explorer"],
                "tools": [{"name": "write_note", "tier": "write"}],
            }
        ],
        "approvals": approvals,
        "grantable_approvals": grantable,
    }
    path = tmp_path / "mcp.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return load_mcp_config(path)


def test_request_approval_only_counts_for_grantable_tools(tmp_path):
    """第二入口的正面与反面：名单里 ⇒ 请求签字生效；名单外 ⇒ 照旧拒，并留下"被丢掉"的痕迹。

    这条刻意**不经 HTTP**直接调 hub：判定权如果在 API 层，绕过 API 的调用方就把闸门绕开了。
    """
    transcript = FakeTranscript()
    config = _write_db_config(tmp_path, approvals={}, grantable=["mcp:db:write_note"])
    hub = _hub(config, transcript=transcript)
    outcome = hub.invoke(
        "db", "write_note", {"host": "h", "note": "n"},
        agent_name="explorer", approvals={"mcp:db:write_note": True},
    )
    assert outcome["result"]["inserted"] is True

    other = _write_db_config(tmp_path, approvals={}, grantable=[])
    hub2 = _hub(other, transcript=transcript)
    with pytest.raises(McpApprovalRequired):
        hub2.invoke(
            "db", "write_note", {"host": "h", "note": "n"},
            agent_name="explorer", approvals={"mcp:db:write_note": True},
        )
    ignored = transcript.find("mcp_approval_ignored")
    assert ignored and ignored[0]["tool"] == "mcp:db:write_note"
    assert hub2.summary()["ignored_approvals"], "被丢掉的批准也要进 summary"


def test_request_may_revoke_a_grantable_config_approval(tmp_path):
    """降权永远安全：运维签过的，请求可以这一次不消。"""
    config = _write_db_config(
        tmp_path, approvals={"mcp:db:write_note": True}, grantable=["mcp:db:write_note"]
    )
    hub = _hub(config)
    with pytest.raises(McpApprovalRequired):
        hub.invoke(
            "db", "write_note", {"host": "h", "note": "n"},
            agent_name="explorer", approvals={"mcp:db:write_note": False},
        )


def test_ungrantable_names_are_visible_in_the_summary(tmp_path):
    config = _write_db_config(tmp_path, approvals={"mcp:db:write_note": True}, grantable=[])
    summary = _hub(config).summary()
    assert summary["approvals_from_config"] == {"mcp:db:write_note": True}
    assert summary["grantable"] == [], "名单是空的这件事本身要能被查见"


def test_grantable_naming_an_absent_tool_fails_at_load(tmp_path):
    """名单里点了不存在的工具 ⇒ 装载就报错。

    留到运行时的样子是"我明明批了却还是缺批准"，那种红看着像闸门坏了，
    其实是配置写错了名字——两类原因必须在这里就分开。
    """
    import yaml

    payload = {
        "servers": [
            {
                "name": "db",
                "command": ["{python}", "-m", SERVER_MODULE, "--db", str(INTEL_DB)],
                "tools": [{"name": "query", "tier": "read"}],
            }
        ],
        "grantable_approvals": ["mcp:db:no_such_tool"],
    }
    path = tmp_path / "mcp.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="并不存在"):
        load_mcp_config(path)


def test_partition_approvals_is_a_pure_function():
    """纯函数级钉住语义：名单外只丢真值（false 本来就是 no-op，不该报"被拒"）。"""
    from agentflow.core.mcp import McpConfig, partition_approvals

    config = McpConfig(servers=[], grantable=frozenset({"mcp:a:b"}))
    accepted, rejected = partition_approvals(
        config, {"mcp:a:b": True, "mcp:x:y": True, "mcp:q:r": False, "mcp:a:b2": False}
    )
    assert accepted == {"mcp:a:b": True}
    assert rejected == ["mcp:x:y"]


def test_run_origin_and_approval_breakdown_reach_the_transcript(tmp_path):
    """批准链有第二个入口之后，"谁批的、批了算不算数"必须查得回来。

    这里走真 run_analysis（不经 HTTP），用仓库默认 MCP 配置：
    `grantable_approvals` 默认为空 ⇒ 请求带来的签字应当**全部被丢**，
    而 `mcp_attached` 事件要同时留下"运维签的""请求想批的""被丢的"三栏。
    """
    from agentflow.pipeline import run_analysis

    result = run_analysis(
        "生产域主机的异常告警有哪些",
        [str(TRIAGE / "auth.csv"), str(TRIAGE / "assets.csv"), str(TRIAGE / "edr.csv")],
        outputs_root=tmp_path,
        pack="sigma_triage",
        mcp_approvals={"mcp:soc_intel:write_note": True},
        run_origin={"source": "web", "actor_user_id": 7, "actor_username": "p_owner"},
    )
    entries = [
        json.loads(line)
        for line in (Path(result["outputs_dir"]) / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    attached = [entry for entry in entries if entry.get("event") == "mcp_attached"][0]
    assert attached["approvals_requested"] == {"mcp:soc_intel:write_note": True}
    assert attached["approvals_accepted"] == {}, "默认名单为空，请求签字不该生效"
    assert attached["approvals_rejected"] == ["mcp:soc_intel:write_note"]
    assert attached["grantable"] == []
    origin = [entry for entry in entries if entry.get("event") == "run_origin"][0]
    assert origin["actor_username"] == "p_owner" and origin["source"] == "web"
    # 外部证据照旧只作证据：报告里不许出现只存在于外部库的那个数字
    report = (Path(result["outputs_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "4242" not in report


# ---------------------------------------------------------------- 数字来源这条线（I1）


def test_external_numbers_are_not_provenance_for_report_claims():
    """外部库里的 4242 一旦进报告，追溯率必须判红——这就是"外部数据只作证据"的牙齿。

    两份证据唯一的区别是报告里有没有引用外部数字，其它一切相同。
    """
    external = {
        "server": "soc_intel",
        "tool": "query",
        "status": "ok",
        "rows": 5,
        "columns": ["host", "score"],
        "untrusted": True,
    }
    base_evaluation = {
        "results": {"1": {"summary": {"aggregate": {"命中数": 6}, "rows": 6}}},
        "dataset_rows": 282,
        "external_evidence": [external],
    }
    clean = {"evaluation": base_evaluation, "report": "本轮命中 6 条发现，审计范围 282 行。", "transcript": [], "plan": {}}
    leaked = {"evaluation": base_evaluation, "report": clean["report"] + " 外部情报评分 4242。", "transcript": [], "plan": {}}
    ratio_clean, unexplained_clean = numbers_traceable(clean)
    ratio_leaked, unexplained_leaked = numbers_traceable(leaked)
    assert ratio_clean == 1.0 and not unexplained_clean
    assert "4242" in unexplained_leaked and ratio_leaked < ratio_clean, "外部数字进报告 = 造数"

def test_external_evidence_predicate_checks_local_audit_not_server_words():
    evidence = {
        "evaluation": {
            "external_evidence": [
                {"server": "soc_intel", "tool": "query", "status": "ok", "rows": 5, "columns": ["host", "score"], "untrusted": True}
            ]
        },
        "report": "",
        "transcript": [],
        "plan": {},
    }
    params = {"server": "soc_intel", "tool": "query", "rows": 5, "columns": ["host", "score"], "evidence_only": True}
    assert _p_external_evidence(evidence, params, "mock")[0]
    # server 说自己返回了 5 行不算证据：本地记成 4 行就要红
    wrong = json.loads(json.dumps(evidence))
    wrong["evaluation"]["external_evidence"][0]["rows"] = 4
    passed, detail = _p_external_evidence(wrong, params, "mock")
    assert not passed and "期望 5 行" in detail
    failed = json.loads(json.dumps(evidence))
    failed["evaluation"]["external_evidence"][0].update({"status": "failed", "reason": "越权"})
    assert not _p_external_evidence(failed, params, "mock")[0]
