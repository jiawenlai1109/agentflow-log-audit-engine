"""② 任务线程内的守卫异常收容 + ③ Bundle 缓存目录的写入保护。

② 的现场：`agents/executor.py` 的 `registry.call` 外面没有 try。任务线程里抛出的
`PathViolationError` / `ToolError` 会一路穿出 `orchestrator` 的 `future.result()`，
于是**整条 run** 被判 degraded、`task_states` 清空——同一批里已经完成并出图的任务跟着一起作废。
单任务失败本来就有正规出口（FAILED → 错误路由），这条通道偏偏漏了这种异常。

③ 的现场：`<outputs>/bundles/bd_<指纹>` 是跨 run 复用的缓存，而"建缓存"是好几个文件依次落盘。
两个请求同时冷启动同一批源文件时，后来者可能读到半成品。24 次并发没观测到坏读，
但"没观测到"不等于"已证明无害"——这是结构性隐患，不是已结案的安全结论。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from agentflow.core.bundle import MANIFEST_NAME
from agentflow.core.tools import PathViolationError, ToolError, ToolRegistry
from agentflow.pipeline import as_bundle, run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIAGE = PROJECT_ROOT / "demo" / "data" / "triage"
SOURCES = [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"]
QUESTION = "生产域主机的异常告警有哪些？哪些需要立刻处置"


def _triage_run(outputs_root: Path):
    return run_analysis(
        question=QUESTION,
        sources=[str(path) for path in SOURCES],
        mode="mock",
        outputs_root=outputs_root,
        pack="sigma_triage",
    )


# ---------------------------------------------------------------- ② 收容


def test_guard_violation_fails_only_its_own_task(tmp_path, monkeypatch):
    baseline = {int(k): v for k, v in _triage_run(tmp_path / "base")["task_states"].items()}
    assert list(baseline.values()).count("SUCCEEDED") >= 2, baseline  # 前提：多任务且大部分成功

    original = ToolRegistry.call

    def violating(self, agent_name, tool_name, ctx, _scope=None, **params):
        if tool_name == "execute_python" and int((_scope or {}).get("task_id") or 0) == 2:
            raise PathViolationError("路径越界：该任务未获准读取这张表")
        return original(self, agent_name, tool_name, ctx, _scope=_scope, **params)

    monkeypatch.setattr(ToolRegistry, "call", violating)
    violated = _triage_run(tmp_path / "violated")
    states = {int(k): v for k, v in violated["task_states"].items()}

    # 收容的判据就一条：别的任务不许跟着一起没了（修之前这里是空字典 + 整条 run degraded）
    assert states, "task_states 被清空 ⇒ 守卫异常仍在连坐整条 run"
    assert [t for t, state in states.items() if state == "SUCCEEDED"], states
    assert states.get(2) == "FAILED", "越界的那个任务必须记成失败，收容不等于放过"

    outputs = Path(violated["outputs_dir"])
    records = [
        json.loads(line)
        for line in (outputs / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    contained = [r for r in records if r.get("event") == "task_guard_contained"]
    assert contained and contained[-1]["task_id"] == 2, "越界没留痕就成了静默少一个任务"
    assert "PathViolationError" in contained[-1]["error"]


def test_guard_violation_does_not_retry_three_times(tmp_path, monkeypatch):
    """守卫拒绝的是"这段代码不该跑"，再写三轮也还是越界——预算不许烧在这里。"""
    calls = {"n": 0}
    original = ToolRegistry.call

    def counting(self, agent_name, tool_name, ctx, _scope=None, **params):
        if tool_name == "execute_python" and int((_scope or {}).get("task_id") or 0) == 2:
            calls["n"] += 1
            raise ToolError("越权工具调用：executor 未被授权使用 execute_python")
        return original(self, agent_name, tool_name, ctx, _scope=_scope, **params)

    monkeypatch.setattr(ToolRegistry, "call", counting)
    _triage_run(tmp_path)
    assert calls["n"] == 1, f"守卫异常被当成普通失败重试了 {calls['n']} 次"


def test_self_heal_loop_stops_on_a_guard_violation(tmp_path, monkeypatch):
    """同样的"不重试"要成立于**非包**路径——自愈循环在那里才有 max_executor_attempts 轮。"""
    calls = {"n": 0}
    original = ToolRegistry.call

    def counting(self, agent_name, tool_name, ctx, _scope=None, **params):
        if tool_name == "execute_python" and int((_scope or {}).get("task_id") or 0) == 1:
            calls["n"] += 1
            raise PathViolationError("路径越界：生成的代码想读仓库外的文件")
        return original(self, agent_name, tool_name, ctx, _scope=_scope, **params)

    monkeypatch.setattr(ToolRegistry, "call", counting)
    result = run_analysis(
        question="统计各账号的登录失败次数，列出风险最高的账号",
        sources=str(PROJECT_ROOT / "demo" / "data" / "login_auth.csv"),
        mode="mock",
        outputs_root=tmp_path,
    )
    attempts = max(
        int((detail or {}).get("attempts") or 0)
        for detail in json.loads(
            (Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8")
        )["results"].values()
    )
    assert calls["n"] == 1, f"守卫异常进了自愈循环，重试到第 {calls['n']} 次"
    assert attempts == 1, f"任务记录的尝试次数是 {attempts}，与「守卫拒绝即终止」不符"


# ---------------------------------------------------------------- ③ 缓存写入


def test_concurrent_cold_start_only_hands_out_complete_bundles(tmp_path):
    """八路同时冷启动同一批源文件：每个拿到的 Bundle 都必须三表齐、文件都在。"""
    outputs_root = tmp_path / "outputs"
    bundles: list = []
    errors: list[BaseException] = []
    gate = threading.Barrier(8)

    def worker() -> None:
        try:
            gate.wait()
            bundles.append(as_bundle([str(path) for path in SOURCES], outputs_root))
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要能在主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)

    assert not errors, errors
    assert len(bundles) == 8
    for bundle in bundles:
        assert len(bundle.tables) == 3, [table.source_file for table in bundle.tables]
        assert all(Path(table.path).exists() for table in bundle.tables), "读到了半份缓存"
    cache_dirs = sorted(path.name for path in (outputs_root / "bundles").iterdir())
    assert len(cache_dirs) == 1, cache_dirs
    # 中转文件用完必须消失：留着就是每次跑批复写一份、目录越堆越深
    leftovers = [p.name for p in (outputs_root / "bundles").rglob("*.tmp-*")]
    assert not leftovers, leftovers


def test_cache_hit_does_not_rewrite_the_directory(tmp_path):
    outputs_root = tmp_path / "outputs"
    first = as_bundle([str(path) for path in SOURCES], outputs_root)
    manifest = next((outputs_root / "bundles").glob(f"*/{MANIFEST_NAME}"))
    written = manifest.stat().st_mtime_ns

    second = as_bundle([str(path) for path in SOURCES], outputs_root)
    assert [t.source_file for t in second.tables] == [t.source_file for t in first.tables]
    assert manifest.stat().st_mtime_ns == written, "命中缓存却把目录又写了一遍"


def test_half_written_cache_is_not_served(tmp_path):
    """manifest 是发布的唯一信号：只有表文件、没有清单的目录必须被当作"没有缓存"重建。"""
    outputs_root = tmp_path / "outputs"
    bundle = as_bundle([str(path) for path in SOURCES], outputs_root)
    root = Path(bundle.root)
    (root / MANIFEST_NAME).unlink()  # 模拟上一次崩溃在写完表之后、写清单之前

    rebuilt = as_bundle([str(path) for path in SOURCES], outputs_root)
    assert len(rebuilt.tables) == 3, [table.source_file for table in rebuilt.tables]
    assert (root / MANIFEST_NAME).exists()


def test_short_cache_is_rebuilt_not_served(tmp_path):
    """"少一张表"的缓存正是这次要防的症状：成员数对不上就必须重建，不能将就用。"""
    outputs_root = tmp_path / "outputs"
    bundle = as_bundle([str(path) for path in SOURCES], outputs_root)
    root = Path(bundle.root)
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["tables"] = manifest["tables"][:2]  # 一份"当时只落了 2/3 张表"的清单
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    rebuilt = as_bundle([str(path) for path in SOURCES], outputs_root)
    assert len(rebuilt.tables) == 3, [table.source_file for table in rebuilt.tables]
