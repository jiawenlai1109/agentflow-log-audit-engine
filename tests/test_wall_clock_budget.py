"""挂账 #43：`total_budget_seconds` 到底管什么，产物要自己说得出。

缺陷形状（2026-10-05/06 real 实测）：这个键写在 `execution` 里、默认 120，看起来是
整条 run 的硬顶；实际判定点只有**派发新工作**那一处（波次边界）。于是 E21 以
**236.656s** 跑完并 `success`，而产物里 `wall_clock_timeout` 零次——超支这件事在
评估记录上完全隐形，读的人只能靠"名字"相信它被管住了。

这一批用例守三件事，顺序即重要性：
1. **超支必留痕**：哪怕一个任务都没被砍，`exceeded` 也要说真话；
2. **判定点要点名**：`enforcement` 写的是 `dispatch_boundary`，不含糊；
3. **旧行为不许被这次改动顺带走**：预算为 0 时仍然是"取消未派发 + 运行级降级"，
   而一次没砍到人的超支**不许改判**。

刻意**没有**把这里做成"超支就砍任务"的断言：那正是 #43 里会砍掉现存长 run 的那个分支
（E21 236s、片1 后的 E14 244s，都在 120 以上），它需要一次独立的口径变更与归因。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from agentflow.agents.reporter import ReporterAgent
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUESTION = "对2026-09-05的登录日志做安全审计，列出失败次数最高的账号"
SOURCE = str(PROJECT_ROOT / "demo" / "data" / "login_auth.csv")
# 带包跑。不带 pack 时这条题在 mock 下本来就有任务失败，那与本缺陷无关，
# 却会让"超支不许改判"断言测在错的东西上。
PACK = "login_audit"


def _config(tmp_path: Path, budget_seconds: int) -> str:
    path = tmp_path / "agents.yaml"
    path.write_text(
        yaml.safe_dump({"execution": {"total_budget_seconds": budget_seconds, "max_concurrency": 3}}),
        encoding="utf-8",
    )
    return str(path)


def _go(tmp_path: Path, budget: int) -> tuple[dict, dict]:
    result = run_analysis(
        question=QUESTION,
        sources=SOURCE,
        config_path=_config(tmp_path, budget),
        mode="mock",
        pack=PACK,
        outputs_root=tmp_path / "outputs",
    )
    evaluation = json.loads(
        (Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8")
    )
    return result, evaluation


def test_over_budget_run_records_the_breach_without_cancelling(monkeypatch, tmp_path):
    """超支发生在闸门管不到的地方 ⇒ 谁也不该被砍，但产物必须说得出超了。

    拖慢的是报告阶段（在 DAG 之后）——E21 的真实形状就是整条 run 236s、预算 120s，
    而任务早已全部派发。刻意不拖慢任务本身：那会撞上派发闸门，是另一条路径，
    下面有它的回归位。
    """
    original = ReporterAgent.run

    def slow(self, ctx, message):  # noqa: ANN001 - 与被替换方法的形状一致
        time.sleep(1.4)
        return original(self, ctx, message)

    monkeypatch.setattr(ReporterAgent, "run", slow)
    result, evaluation = _go(tmp_path, 1)
    wall = evaluation["wall_clock"]
    assert wall["budget_seconds"] == 1, wall
    assert wall["elapsed_seconds"] > 1.0, f"实跑 {wall['elapsed_seconds']}s 却没说出超支：{wall}"
    assert wall["exceeded"] is True, wall
    assert wall["enforcement"] == "dispatch_boundary", wall
    assert result["status"] == "success", "超支留痕不该顺手把成功的 run 改判"
    assert evaluation["degraded_reason"] is None, evaluation["degraded_reason"]
    assert all(state == "SUCCEEDED" for state in result["task_states"].values()), result["task_states"]


def test_the_enforcement_point_is_named_not_implied(tmp_path):
    """没超支时同样要说清判定点：读产物的人不必翻代码才知道这栏是什么意思。"""
    _, evaluation = _go(tmp_path, 600)
    wall = evaluation["wall_clock"]
    assert wall["exceeded"] is False and wall["budget_seconds"] == 600, wall
    assert wall["enforcement"] == "dispatch_boundary", wall


def test_zero_budget_still_cancels_undispatched_work(tmp_path):
    """旧行为不动：预算 0 ⇒ 派发闸门直接生效，未派发的任务被取消并报运行级降级。"""
    result, evaluation = _go(tmp_path, 0)
    assert evaluation["wall_clock"]["exceeded"] is True, evaluation["wall_clock"]
    assert result["status"] == "degraded", result["status"]
    assert evaluation["degraded_reason"] == "wall_clock_timeout", evaluation["degraded_reason"]
    assert any(state == "CANCELLED" for state in result["task_states"].values()), result["task_states"]


@pytest.mark.parametrize("budget", [0, 1, 600])
def test_the_field_always_travels_with_the_artifact(tmp_path, budget):
    """每跑自检：这栏不是"某条路径上才有"。缺了它，#43 就等于没修。

    `exceeded` 必须由同栏的两个数算得出来——留痕自己跟自己对不上，就是下一个 #43。
    """
    _, evaluation = _go(tmp_path, budget)
    wall = evaluation["wall_clock"]
    assert set(wall) == {"budget_seconds", "elapsed_seconds", "enforcement", "exceeded"}, wall
    assert wall["budget_seconds"] == budget, wall
    assert wall["exceeded"] is (wall["elapsed_seconds"] > budget), wall
