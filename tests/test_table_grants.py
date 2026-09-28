"""表级授权与多表独立校验（#19）：声明了的才算授权，且校验器按声明的表重放。

两条不变量：
1. **越表即拒**：任务声明 `dataset_refs` 后，读未声明的表（归一化 CSV 或原件副本）都在
   工具参数守卫这一层被拒，并留审计事件——不是"prompt 里没写所以看不见"；
2. **三处同数**：预检（value_counts 相乘）、执行（LLM 写的 merge）、重放（校验器另一条
   算法）必须给出同一个 join 行数。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.core import dataset_scope  # noqa: E402
from agentflow.core.ingest import build_bundle  # noqa: E402
from agentflow.core.tools import (  # noqa: E402
    PathViolationError,
    TableViolationError,
    build_default_registry,
    ensure_authorized,
)
from agentflow.core.verification import run_verification  # noqa: E402


@pytest.fixture()
def bundle(tmp_path):
    """三张表：t1 events / t2 dim / t3 other——用来验"声明两张、第三张就碰不到"。"""
    left = tmp_path / "events.csv"
    left.write_text("主机,事件数\nh1,3\nh2,5\nh3,7\n", encoding="utf-8")
    right = tmp_path / "dim.csv"
    right.write_text("主机,域\nh1,生产\nh2,测试\n", encoding="utf-8")
    third = tmp_path / "other.csv"
    third.write_text("主机,敏感\nh1,yes\n", encoding="utf-8")
    return build_bundle([left, right, third], tmp_path / "bd", strict=True)


def _ctx(bundle, tmp_path):
    return SimpleNamespace(
        outputs_dir=tmp_path / "run_x",
        bundle=bundle,
        data_path=str(bundle.primary.path),
        readable_paths=bundle.readable_paths(),
    )


# ---------------------------------------------------------------- 表级放行


def test_undeclared_table_path_is_rejected(tmp_path, bundle):
    ctx = _ctx(bundle, tmp_path)
    hidden = bundle.tables[2].path
    with pytest.raises(TableViolationError) as raised:
        ensure_authorized(ctx, hidden, task_id=2, grants=[], dataset_refs=["t1", "t2"])
    assert "表级越权" in str(raised.value)
    assert "t1" in str(raised.value) and "t2" in str(raised.value)


def test_declared_table_path_is_granted(tmp_path, bundle):
    ctx = _ctx(bundle, tmp_path)
    granted = ensure_authorized(
        ctx, bundle.tables[1].path, task_id=2, grants=[], dataset_refs=["t1", "t2"]
    )
    assert Path(granted) == Path(bundle.tables[1].path).resolve()


def test_original_copy_in_sources_is_also_denied(tmp_path, bundle):
    """原件副本是同一条数据的另一扇门：只堵归一化 CSV 等于没堵。"""
    ctx = _ctx(bundle, tmp_path)
    with pytest.raises(TableViolationError):
        ensure_authorized(
            ctx, bundle.tables[2].source_path, task_id=2, grants=[], dataset_refs=["t1", "t2"]
        )


def test_run_level_role_keeps_full_bundle_view(tmp_path, bundle):
    """task_id/refs 为空 = 运行级角色（Explorer 要画像全部表才能提议 refs）。"""
    ctx = _ctx(bundle, tmp_path)
    assert ensure_authorized(ctx, bundle.tables[2].path, task_id=None, grants=None)


def test_single_ref_declaration_does_not_narrow(tmp_path, bundle):
    """声明不足两张 = 单表语义，不收窄——既有计划与场景包规则任务都不带 refs。"""
    ctx = _ctx(bundle, tmp_path)
    assert ensure_authorized(ctx, bundle.tables[2].path, task_id=1, grants=[], dataset_refs=[])


def test_table_violation_is_a_path_violation(tmp_path, bundle):
    """子类关系是刻意的：所有按"路径越界"处置的通道（自愈/审计/路由）继续生效。"""
    assert issubclass(TableViolationError, PathViolationError)


# ---------------------------------------------------------------- 审计留痕


def test_registry_call_audits_table_denial(tmp_path, bundle):
    transcript_lines: list[dict] = []

    class _Writer:
        def write(self, record):
            transcript_lines.append(record)

    ctx = _ctx(bundle, tmp_path)
    ctx.transcript = _Writer()
    ctx.config = {}
    registry = build_default_registry({})
    with pytest.raises(TableViolationError):
        registry.call(
            "executor",
            "execute_python",
            ctx,
            _scope={"task_id": 2, "grants": [], "dataset_refs": ["t1", "t2"]},
            code="print(1)",
            work_dir=Path(ctx.outputs_dir) / "work" / "2",
            data_path=bundle.tables[2].path,
        )
    denial = [line for line in transcript_lines if line.get("event") == "tool_denied_table"]
    assert denial, f"越表必须留审计事件：{transcript_lines}"
    assert denial[0]["declared_refs"] == ["t1", "t2"]
    assert "t3.csv" in denial[0]["path"]  # 未声明表的归一化路径原样进审计


# ---------------------------------------------------------------- 作用域口径


def test_scope_helpers_treat_one_ref_as_single_table(tmp_path, bundle):
    assert dataset_scope.scope_refs({"dataset_refs": ["t1"]}) == []
    assert dataset_scope.task_tables(_ctx(bundle, tmp_path), {"dataset_refs": ["t1"]}) == []
    refs = {"dataset_refs": ["t2", "t3"]}
    ctx = _ctx(bundle, tmp_path)
    assert [t.id for t in dataset_scope.task_tables(ctx, refs)] == ["t2", "t3"]
    # 主表 = 声明的第一张，而不是 Bundle 主表
    assert dataset_scope.primary_path(ctx, refs) == str(bundle.tables[1].path)
    assert set(dataset_scope.table_paths(ctx, refs)) == {"t2", "t3"}


def test_column_scope_excludes_undeclared_tables(tmp_path, bundle):
    ctx = _ctx(bundle, tmp_path)
    columns = dataset_scope.column_scope(ctx, {"dataset_refs": ["t1", "t2"]})
    assert "事件数" in columns and "域" in columns
    assert "敏感" not in columns  # 只在未声明的 t3 里


# ---------------------------------------------------------------- join 重放校验


def _result(aggregate):
    return {"status": "success", "summary": {"aggregate": aggregate}}


def _truth(bundle):
    """独立算一遍真值（不用系统任何代码）：t1⋈t2 on 主机。"""
    import pandas as pd

    left = pd.read_csv(bundle.tables[0].path)
    right = pd.read_csv(bundle.tables[1].path)
    merged = left.merge(right, on="主机", how="inner")
    return len(merged), float(merged["事件数"].sum())


def test_join_replay_passes_when_numbers_agree(tmp_path, bundle):
    rows, total = _truth(bundle)
    task = {"dataset_refs": ["t1", "t2"], "join_keys": ["主机"], "_question": "关联汇总"}
    outcome = run_verification(
        task,
        _result({"join_行数": rows, "合计_事件数": total}),
        str(bundle.tables[0].path),
        {"columns": [{"name": "事件数", "dtype": "int64"}]},
        table_paths=dataset_scope.table_paths(_ctx(bundle, tmp_path), task),
    )
    assert outcome["status"] == "pass", outcome
    assert "join 重放" in outcome["message"]


def test_join_replay_fails_when_reported_rows_are_wrong(tmp_path, bundle):
    _, total = _truth(bundle)
    task = {"dataset_refs": ["t1", "t2"], "join_keys": ["主机"], "_question": "关联汇总"}
    outcome = run_verification(
        task,
        _result({"join_行数": 999, "合计_事件数": total}),
        str(bundle.tables[0].path),
        {"columns": [{"name": "事件数", "dtype": "int64"}]},
        table_paths=dataset_scope.table_paths(_ctx(bundle, tmp_path), task),
    )
    assert outcome["status"] == "fail", outcome


def test_join_replay_skips_without_declared_key(tmp_path, bundle):
    task = {"dataset_refs": ["t1", "t2"], "join_keys": [], "_question": "关联汇总"}
    outcome = run_verification(
        task,
        _result({"join_行数": 2}),
        str(bundle.tables[0].path),
        {},
        table_paths=dataset_scope.table_paths(_ctx(bundle, tmp_path), task),
    )
    assert outcome["status"] == "skipped"
    assert "join_keys" in outcome["message"]


def test_single_table_task_uses_category_template(tmp_path, bundle):
    """没声明 refs 的任务不受 join 分支影响：仍是原来的类别模板路径。"""
    outcome = run_verification(
        {"dataset_refs": [], "description": "总销售额是多少", "_question": "总销售额是多少"},
        _result({"合计_销售额": 1.0}),
        str(bundle.tables[0].path),
        {"columns": [{"name": "销售额", "dtype": "float64"}]},
        table_paths={},
    )
    assert outcome["status"] in ("pass", "fail", "skipped")
    assert "join 重放" not in outcome["message"]
