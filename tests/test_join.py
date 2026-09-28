"""join 预检器（M2-3）单测：基数精确性 + 三类拒绝 + 空语义放行。

核心一条：**预检算出的行数必须等于真正 join 出来的行数**（value_counts 相乘是生产者，
pandas merge 是异构校验者）。这条不成立，"派发前拦截"就变成了凭感觉拦。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.core.bundle import Bundle  # noqa: E402
from agentflow.core.ingest import build_bundle  # noqa: E402
from agentflow.core.join import (  # noqa: E402
    MAX_EXPANSION_RATIO,
    check_pair,
    preflight,
    resolve_key,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOC = PROJECT_ROOT / "demo" / "data" / "soc"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _pair(tmp_path: Path, left_text: str, right_text: str, key: str, left: str = "l", right: str = "r"):
    lp = _write(tmp_path, f"{left}.csv", left_text)
    rp = _write(tmp_path, f"{right}.csv", right_text)
    return lp, rp, key, list(pd.read_csv(lp).columns), list(pd.read_csv(rp).columns)


# ---------------------------------------------------------------- 基数精确性


@pytest.mark.parametrize(
    "left_text,right_text",
    [
        # 一对多：左主键唯一，右多行命中同一键
        ("k,v\nA,1\nB,2\n", "k,w\nA,x\nA,y\nB,z\n"),
        # 多对多：同键两侧都有多行 → 行数是乘积
        ("k,v\nA,1\nA,2\nB,3\n", "k,w\nA,x\nA,y\nC,z\n"),
        # 空值键：pandas merge 把两侧空值当相等，预检必须同口径
        ("k,v\nA,1\n,2\n,3\n", "k,w\nA,x\n,9\n"),
        # 零命中（含空值键两侧都有但对不上）
        ("k,v\nA,1\nB,2\n", "k,w\nX,x\nY,y\n"),
        # 一侧空表
        ("k,v\n", "k,w\nA,x\n"),
    ],
)
def test_expected_rows_equal_real_merge(tmp_path, left_text, right_text):
    lp, rp, key, lcols, rcols = _pair(tmp_path, left_text, right_text, key="k")
    check = check_pair("l", lp, "r", rp, key, left_columns=lcols, right_columns=rcols)
    merged = len(pd.read_csv(lp).merge(pd.read_csv(rp), on=key))
    assert check.expected_rows == merged


def test_expansion_guard_rejects_near_cartesian(tmp_path):
    """两侧键都不唯一 → 行数按乘积膨胀，必须在派发前拒。"""
    left = "k,v\n" + "\n".join(f"same,{i}" for i in range(60))
    right = "k,w\n" + "\n".join(f"same,{i}" for i in range(60))
    lp, rp, key, lcols, rcols = _pair(tmp_path, left, right, key="k")
    check = check_pair("l", lp, "r", rp, key, left_columns=lcols, right_columns=rcols)
    assert check.ok is False
    assert check.reason_code == "expansion"
    assert check.expected_rows == 60 * 60  # 3600 行，正是笛卡尔积本身


def test_expansion_threshold_boundary_is_inclusive(tmp_path):
    """膨胀比恰等于上界时放行、超出即拒：阈值语义必须可测，不是注释里的形容词。"""
    limit = int(MAX_EXPANSION_RATIO)
    # 左 8 行（A×4）⋈ 右 8 行（A×8）→ 期望 32 行 = baseline 8 的 4.0 倍，恰在上界
    at_limit_left = "k,v\n" + "\n".join(["A,1"] * 4 + ["B,2", "C,3", "D,4", "E,5"])
    at_limit_right = "k,w\n" + "\n".join(f"A,{i}" for i in range(8))
    lp, rp, key, lcols, rcols = _pair(tmp_path, at_limit_left, at_limit_right, key="k")
    check = check_pair("l", lp, "r", rp, key, left_columns=lcols, right_columns=rcols)
    assert check.ok is True, check.detail
    assert check.expansion_ratio == pytest.approx(MAX_EXPANSION_RATIO)
    # 只多一个重复键 → 5.0 倍，立刻拒
    over_left = "k,v\n" + "\n".join(["A,1"] * 5 + ["B,2", "C,3", "D,4"])
    lp, rp, key, lcols, rcols = _pair(tmp_path, over_left, at_limit_right, key="k")
    check = check_pair("l", lp, "r", rp, key, left_columns=lcols, right_columns=rcols)
    assert check.ok is False and check.reason_code == "expansion"


def test_zero_overlap_is_rejected_as_plan_error(tmp_path):
    lp, rp, key, lcols, rcols = _pair(
        tmp_path, "k,v\nA,1\nB,2\n", "k,w\nX,x\nY,y\n", key="k"
    )
    check = check_pair("l", lp, "r", rp, key, left_columns=lcols, right_columns=rcols)
    assert check.ok is False
    assert check.reason_code == "no_overlap"


def test_empty_side_passes_with_empty_semantic(tmp_path):
    """空事实表 join 恒为空，但那是合法的空语义输入，不该判成计划错误。"""
    lp, rp, key, lcols, rcols = _pair(tmp_path, "k,v\n", "k,w\nA,x\n", key="k")
    check = check_pair("l", lp, "r", rp, key, left_columns=lcols, right_columns=rcols)
    assert check.ok is True
    assert check.reason_code == "empty_side"


def test_numeric_vs_text_key_reports_dtype_mismatch(tmp_path):
    """数值键 join 文本键在执行期直接抛 ValueError，预检要说清是连不上而非零重叠。"""
    lp, rp, key, lcols, rcols = _pair(
        tmp_path, "k,v\n1,a\n2,b\n", "k,w\n1,x\n2,y\nZ,q\n", key="k"
    )
    # 右表混入文本值 → 整列被判 object，左列仍是数值
    check = check_pair("l", lp, "r", rp, key, left_columns=lcols, right_columns=rcols)
    assert check.ok is False
    assert check.reason_code == "dtype_mismatch"


def test_missing_key_column_reports_no_key(tmp_path):
    lp, rp, _, _, _ = _pair(tmp_path, "src_ip,a\n1,2\n", "host,b\n1,2\n", key="src_ip")
    check = check_pair(
        "l", lp, "r", rp, "src_ip",
        left_columns=list(pd.read_csv(lp).columns),
        right_columns=list(pd.read_csv(rp).columns),
    )
    assert check.ok is False
    assert check.reason_code == "no_key"
    # 拒绝理由必须指出修复入口，否则用户只知道连不上、不知道去哪儿连
    assert "column_aliases" in check.detail


# ---------------------------------------------------------------- Bundle 级


@pytest.fixture()
def soc_bundle(tmp_path) -> Bundle:
    """真实 SOC 三源入包：firewall.csv(src_ip…) / assets.tsv(主机…) / edr.json(主机…)。"""
    return build_bundle(
        [SOC / "firewall.csv", SOC / "assets.tsv", SOC / "edr.json"],
        tmp_path / "bd",
        strict=True,
    )


def test_preflight_accepts_bundle_unknown_refs(soc_bundle):
    check = preflight(soc_bundle, ["t9"])
    assert check.ok is True and check.reason_code == "single_table"


def test_preflight_rejects_invented_table_id(soc_bundle):
    check = preflight(soc_bundle, ["t1", "t99"])
    assert check.ok is False
    assert check.reason_code == "unknown_ref"
    assert "t99" in check.detail


def test_preflight_rejects_firewall_to_assets_without_alias(soc_bundle):
    """列名故意不统一（src_ip vs 主机）：没有包内列映射就连不上，预检必须挡住。"""
    check = preflight(soc_bundle, ["t1", "t2"])
    assert check.ok is False
    assert check.reason_code in ("no_key", "no_overlap")
    assert "t1" in str(check) and "t2" in str(check)


def test_preflight_accepts_assets_to_edr_on_host(soc_bundle):
    check = preflight(soc_bundle, ["t2", "t3"], keys=["主机"])
    assert check.ok is True, check.detail
    pair = check.pairs[0]
    assert pair.key == "主机"
    assert pair.overlap > 0 and pair.expected_rows > 0


def test_resolve_key_prefers_declared_then_highest_overlap(soc_bundle):
    assert resolve_key(soc_bundle, ["t2", "t3"], keys=["主机"]) == "主机"
    # 未声明时从 Bundle.join_candidates 里取（t2↔t3 的 主机）
    assert resolve_key(soc_bundle, ["t2", "t3"]) == "主机"
    assert resolve_key(soc_bundle, ["t1"]) is None


def test_preflight_multi_ref_chain_fails_if_any_pair_fails(soc_bundle):
    check = preflight(soc_bundle, ["t1", "t2", "t3"], keys=["主机", "主机"])
    assert check.ok is False
    assert len(check.pairs) == 2  # (t1,t2) 与 (t2,t3) 都算过
    assert check.pairs[0].ok is False
    assert check.pairs[1].ok is True


def test_preflight_dict_shape_is_transcript_friendly(soc_bundle):
    payload = preflight(soc_bundle, ["t2", "t3"], keys=["主机"]).as_dict()
    assert set(payload) >= {"refs", "ok", "reason", "pairs", "expected_rows"}
    assert payload["refs"] == ["t2", "t3"]
    assert payload["pairs"][0]["left"] == "t2"


def test_low_overlap_warning_does_not_reject(tmp_path):
    """部分匹配在事实↔维表里是正常的：只警告，不拦。"""
    from agentflow.core.join import LOW_OVERLAP_WARNING

    assert LOW_OVERLAP_WARNING == 0.05
    left = "k,v\n" + "\n".join(f"A{i},{i}" for i in range(25))
    right = "k,w\nA0,x\n"
    bundle = build_bundle(
        [_write(tmp_path, "left.csv", left), _write(tmp_path, "right.csv", right)],
        tmp_path / "bd_low",
        strict=True,
    )
    check = preflight(bundle, ["t1", "t2"], keys=["k"])
    assert check.ok is True, check.detail
    assert check.pairs[0].overlap < LOW_OVERLAP_WARNING
    assert "警告" in check.detail


# ---------------------------------------------------------------- 端到端（派发前拦截）


def _two_table_bundle(tmp_path, left_text, right_text):
    from agentflow.core.ingest import build_bundle

    return build_bundle(
        [
            _write(tmp_path, "events.csv", left_text),
            _write(tmp_path, "dim.csv", right_text),
        ],
        tmp_path / "bd_e2e",
        strict=True,
    )


def test_run_blocks_cartesian_task_before_dispatch(tmp_path):
    """两侧键都大量重复 → 预检判定膨胀，任务根本不该被派发（子进程一次都不跑）。"""
    from agentflow.pipeline import run_analysis

    bundle = _two_table_bundle(
        tmp_path,
        "k,v\n" + "\n".join(f"A,{i}" for i in range(8)),
        "k,w\n" + "\n".join(f"A,{i}" for i in range(8)),
    )
    result = run_analysis("按 k 关联两张表汇总 v", bundle, outputs_root=tmp_path / "out")
    evaluation = _read_eval(result)
    states = {int(key): value for key, value in evaluation["task_states"].items()}
    join_tasks = [tid for tid, state in states.items() if state == "FAILED"]
    assert join_tasks, "跨表任务应被拦下"
    for tid in join_tasks:
        assert evaluation["results"][str(tid)]["error_class"] == "JOIN_PRECHECK"
        # 派发前拦截的判据：这个任务的 work 目录根本没被创建过
        assert not (Path(result["outputs_dir"]) / "work" / str(tid)).exists()
        assert not (Path(result["outputs_dir"]) / "artifacts" / f"step_{tid}_result.json").exists()
    assert evaluation["join_preflight"], "预检判定必须留痕"
    assert any(
        check["reason"] == "expansion" for check in evaluation["join_preflight"].values()
    )
    # 拦截不该把整条 run 打死：单表任务照常出结果
    assert result["status"] in ("partial", "success")
    succeeded = [tid for tid, state in states.items() if state == "SUCCEEDED"]
    assert succeeded, "同批里的单表任务必须照常完成"


def test_run_executes_healthy_join_and_number_matches_independent_merge(tmp_path):
    """合法多对一 join 要真的跑起来，且 mock 报的行数与独立重算的 merge 行数一致。"""
    import pandas as pd

    from agentflow.pipeline import run_analysis

    left_text = "主机,域\n" + "\n".join(f"h{i},域{i % 3}\n" for i in range(6))
    right_text = "主机,事件数\n" + "\n".join(
        f"h{i % 6},{10 + i}" for i in range(12)
    ) + "\n"
    bundle = _two_table_bundle(tmp_path, left_text, right_text)
    result = run_analysis("按主机关联两张表汇总事件数", bundle, outputs_root=tmp_path / "out2")
    evaluation = _read_eval(result)

    truth = len(
        pd.read_csv(tmp_path / "events.csv").merge(
            pd.read_csv(tmp_path / "dim.csv"), on="主机", how="inner"
        )
    )
    join_results = [
        r
        for r in evaluation["results"].values()
        if isinstance(r.get("summary"), dict)
        and "join_行数" in (r["summary"].get("aggregate") or {})
    ]
    assert join_results, f"跨表任务应成功执行：{evaluation['task_states']}"
    assert int(join_results[0]["summary"]["aggregate"]["join_行数"]) == truth
    assert all(check["ok"] for check in evaluation["join_preflight"].values())
    # 三处同数：预检（value_counts 相乘）== 执行（LLM 写的 merge）== 校验器重放（另一条算法）
    verdict = next(
        r["verdict"] for r in evaluation["results"].values() if r.get("verdict")
    )
    assert "aggregate_match_check:PASS" in verdict["checks"], verdict["checks"]


def test_single_table_run_records_no_join_preflight(tmp_path):
    """单表语义必须一个字节不变：不声明 refs，就不产生任何 join 判定。"""
    from agentflow.pipeline import run_analysis

    data = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"
    result = run_analysis("总销售额是多少？", str(data), outputs_root=tmp_path / "out3")
    evaluation = _read_eval(result)
    assert evaluation["status"] == "success"
    assert evaluation["join_preflight"] == {}
    plan = _read_json(Path(result["outputs_dir"]) / "plan.json")
    assert all(not task.get("dataset_refs") for task in plan["tasks"])


def _read_eval(result):
    import json
    from pathlib import Path

    return json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))


def _read_json(path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 包内列别名（M3-1）

ALIAS = {"src_ip": "主机", "host": "主机"}


@pytest.fixture()
def aliased_bundle(tmp_path):
    """防火墙叫 src_ip、资产表叫 主机 —— 同一实体两种叫法（SOC 三源的真实形状）。"""
    from agentflow.core.ingest import build_bundle

    events = _write(
        tmp_path,
        "firewall.csv",
        "time,src_ip,account\n2026-09-05 20:00:01,10.0.0.1,u1\n"
        "2026-09-05 20:00:02,10.0.0.2,u2\n2026-09-05 20:00:03,10.0.0.1,u3\n",
    )
    assets = _write(
        tmp_path, "assets.tsv", "主机\t域\n10.0.0.1\t生产\n10.0.0.2\t测试\n"
    )
    return build_bundle([events, assets], tmp_path / "bd_alias", strict=True)


def test_candidates_without_alias_find_nothing(aliased_bundle):
    """没有别名时两表确实连不上——这条是"别名不是装饰"的前提事实。"""
    assert aliased_bundle.join_candidates() == []


def test_candidates_with_alias_propose_canonical_key(aliased_bundle):
    candidates = aliased_bundle.join_candidates(ALIAS)
    assert len(candidates) == 1
    item = candidates[0]
    assert item["column"] == "主机"
    assert item["left_column"] == "src_ip" and item["right_column"] == "主机"
    assert item["usable"] is True


def test_preflight_accepts_aliased_pair_and_counts_exactly(aliased_bundle):
    check = preflight(aliased_bundle, ["t1", "t2"], ["主机"], ALIAS)
    assert check.ok is True, check.detail
    pair = check.pairs[0]
    assert (pair.left_column, pair.right_column) == ("src_ip", "主机")
    import pandas as pd

    truth = len(
        pd.read_csv(aliased_bundle.tables[0].path)
        .rename(columns={"src_ip": "主机"})
        .merge(pd.read_csv(aliased_bundle.tables[1].path), on="主机")
    )
    assert pair.expected_rows == truth == 3


def test_preflight_canonical_name_wins_over_alias(aliased_bundle):
    """两侧都有同名 `主机` 列时不该被别名规则改语义（先精确命中再走别名）。"""
    from agentflow.core.bundle import column_for_canonical

    assert column_for_canonical(["主机", "src_ip"], "主机", ALIAS) == "主机"
    assert column_for_canonical(["src_ip"], "主机", ALIAS) == "src_ip"
    assert column_for_canonical(["其他"], "主机", ALIAS) is None


def test_executor_prompt_states_the_rename(tmp_path, aliased_bundle):
    """别名只有落到"先 rename 再 merge"这句指令上，执行器才真的用得上。"""
    from types import SimpleNamespace

    from agentflow.agents.executor import ExecutorAgent
    from agentflow.core.llm import MockLLM

    ctx = SimpleNamespace(
        bundle=aliased_bundle,
        schema_profile={"columns": [{"name": "time"}, {"name": "src_ip"}]},
        pack=SimpleNamespace(column_aliases=ALIAS),
    )
    task = {"task_id": 1, "dataset_refs": ["t1", "t2"], "join_keys": ["主机"], "description": "关联"}
    lines = ExecutorAgent(llm=MockLLM())._join_lines(ctx, task)
    joined = "\n".join(lines)
    assert "侧列名 'src_ip'" in joined and "侧列名 '主机'" in joined
    assert "先把 'src_ip' 重命名为 '主机'" in joined


def test_mock_generated_code_renames_before_merge(tmp_path, aliased_bundle):
    """mock 生成的代码必须真的 rename：否则 join 出空表，三处同数就成了三处同错。"""
    from types import SimpleNamespace

    from agentflow.agents.executor import ExecutorAgent
    from agentflow.core.llm import MockLLM

    ctx = SimpleNamespace(
        bundle=aliased_bundle,
        schema_profile={"columns": [{"name": "time"}, {"name": "src_ip"}]},
        pack=SimpleNamespace(column_aliases=ALIAS),
    )
    task = {"task_id": 1, "dataset_refs": ["t1", "t2"], "join_keys": ["主机"], "description": "关联"}
    prompt = ExecutorAgent(llm=MockLLM())._task_prompt(ctx, task)
    code = MockLLM()._executor_code([{"role": "user", "content": prompt}])
    assert "rename(columns=" in code and "_KEY" in code
    work = tmp_path / "run" / "work" / "1"
    work.mkdir(parents=True)
    env = {
        "DATA_PATH_T1": aliased_bundle.tables[0].path,
        "DATA_PATH_T2": aliased_bundle.tables[1].path,
    }
    from agentflow.core.executor import LocalBackend

    outcome = LocalBackend().execute(code, work_dir=work, env=env, timeout=60)
    assert outcome.success, outcome.stderr[-300:]
    import json

    payload = json.loads(outcome.stdout[outcome.stdout.index("{") : outcome.stdout.rindex("}") + 1])
    assert payload["aggregate"]["join_行数"] == 3
