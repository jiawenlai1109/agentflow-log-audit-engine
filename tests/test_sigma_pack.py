"""M3-2 场景包 #2：SOC 多源告警分诊（三源列名不统一，规则按角色寻址而非表 id）。

这个文件里的每条期望值都是从原始 CSV 用 stdlib 手工数出来的（`_recount`），
不从任何一次运行结果里抄——抄来的断言只能证明"没改坏"，不能证明"算对了"。
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.core.dataset_scope import join_pairs, primary_path, scope_refs
from agentflow.core.executor import LocalBackend
from agentflow.core.ingest import build_bundle
from agentflow.core.pack import (
    available_columns,
    load_pack,
    pack_plan_tasks,
    role_env,
    verify_findings,
)
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIAGE = PROJECT_ROOT / "demo" / "data" / "triage"
AUTH, ASSETS, EDR = TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"

# 刻意埋的误报陷阱：非生产域但失败很多（含外部 IP 爆破）/ 有高危 EDR 告警但只失败一次
TRAP_NON_PRODUCTION = "10.0.0.8"
TRAP_NOISY_EDR = "10.0.0.13"
TRAP_EXTERNAL = "203.0.113.7"


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _span_minutes(stamps: list[str]) -> float:
    moments = sorted(datetime.strptime(s, "%Y-%m-%d %H:%M:%S") for s in stamps)
    return (moments[-1] - moments[0]).total_seconds() / 60.0


def _inner_join_rows(left: list[str], right: list[str]) -> int:
    """inner join 行数的定义式：同一键值两侧行数相乘再求和（不物化连接结果）。"""
    right_counts = Counter(right)
    left_counts = Counter(left)
    return sum(count * right_counts[key] for key, count in left_counts.items())


def _recount() -> dict:
    """独立重算三条规则与两个连接基数（纯 stdlib，不读包内任何实现）。"""
    auth, assets, edr = _csv(AUTH), _csv(ASSETS), _csv(EDR)
    per_pair: dict[tuple[str, str], list[str]] = defaultdict(list)
    per_host: dict[str, list[str]] = defaultdict(list)
    for row in auth:
        if row["auth_result"] == "failed":
            per_pair[(row["src_ip"], row["account"])].append(row["time"])
            per_host[row["src_ip"]].append(row["time"])

    production = {row["主机"] for row in assets if row["是否生产"] == "Y"}
    high = {row["主机"] for row in edr if row["严重级"] == "high"}
    return {
        "T1": {
            f"{ip}->{acct}": len(stamps)
            for (ip, acct), stamps in per_pair.items()
            if len(stamps) >= 8 and _span_minutes(stamps) <= 5
        },
        "T3": {
            host: len(stamps)
            for host, stamps in per_host.items()
            if host in production and len(stamps) >= 3
        },
        "T4": {
            host: len(stamps) for host, stamps in per_host.items() if host in high and len(stamps) >= 2
        },
        "JOIN_AUTH_ASSETS": _inner_join_rows(
            [row["src_ip"] for row in auth], [row["主机"] for row in assets]
        ),
        "JOIN_EDR_AUTH": _inner_join_rows(
            [row["主机"] for row in edr], [row["src_ip"] for row in auth]
        ),
        "ROWS": {"auth": len(auth), "assets": len(assets), "edr": len(edr)},
    }


@pytest.fixture(scope="module")
def pack():
    return load_pack("sigma_triage")


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return build_bundle([AUTH, ASSETS, EDR], tmp_path_factory.mktemp("sigma_bundle"), strict=True)


@pytest.fixture(scope="module")
def bundle_reordered(tmp_path_factory):
    """同一批文件、换个上传顺序：表 id 会变，规则的角色解析不能变。"""
    return build_bundle([EDR, AUTH, ASSETS], tmp_path_factory.mktemp("sigma_bundle_rev"), strict=True)


def _table(bundle, filename: str):
    return next(table for table in bundle.tables if table.source_file == filename)


def _source_of(bundle, table_id: str) -> str:
    return next(t.source_file for t in bundle.tables if str(t.id) == str(table_id))


def _source_for_path(bundle, path) -> str:
    """归一化 CSV 路径 → 原始文件名：比较"解析到哪张表"要看源文件，表 id 会随上传顺序变。"""
    target = str(Path(path).resolve())
    return next(
        t.source_file
        for t in bundle.tables
        if str(Path(t.path).resolve()) == target
    )


def _roles(pack, bundle, rule) -> dict[str, str]:
    """角色 → 原始文件名（而不是表 id）：id 取决于上传顺序，比较 id 等于什么都没验。"""
    return {
        key.replace("DATA_PATH_", "").lower(): _source_for_path(bundle, value)
        for key, value in role_env(pack, bundle, rule).items()
    }


def _primary_of(pack, bundle, rule) -> Path:
    """规则第一个角色表：T1 的 DATA_PATH 必须是认证日志，不能是按文件名排第一的资产台账。"""
    resolved = rule.resolve(pack, bundle)
    return Path(next(iter(resolved.values())).path) if resolved else bundle.primary.path


def _produce(pack, bundle, rule, work_dir: Path) -> dict:
    """跑规则的参考实现（pandas 路径），env 与执行器给它的完全一致。"""
    outcome = LocalBackend().execute(
        rule.reference_code,
        work_dir=work_dir,
        env={"DATA_PATH": str(_primary_of(pack, bundle, rule)), **role_env(pack, bundle, rule)},
        timeout=120,
    )
    assert outcome.success, outcome.stderr[-400:]
    start = outcome.stdout.index("{")
    return json.loads(outcome.stdout[start : outcome.stdout.rindex("}") + 1])


def _task(pack, bundle, rule_id: str) -> dict:
    return next(t for t in pack_plan_tasks(pack, bundle) if t["rule_params"]["id"] == rule_id)


# ------------------------------------------------------------ 包目录


def test_pack_declares_three_cross_source_rules(pack, bundle):
    assert [rule.id for rule in pack.rules] == ["T1", "T3", "T4"]
    assert pack.column_aliases == {"src_ip": "主机", "host": "主机"}
    assert pack.subject_label == "多源告警分诊"
    assert pack.report_sections == ["分诊队列", "证据链", "处置建议", "研判摘要", "分诊说明"]
    # 必需列一律按规范名声明：`src_ip` 经别名映射后就是 `主机`，写在这里会永远校验不过
    assert "src_ip" not in pack.required_columns
    assert set(pack.required_columns) <= available_columns(pack, bundle)
    for rule in pack.rules:
        assert rule.severity in {"critical", "high", "medium", "low"}
        assert rule.disposition and rule.detection_spec
        assert rule.reference_code and rule.verify_code, rule.id


def test_rules_address_roles_not_table_ids(pack):
    by_id = {rule.id: rule for rule in pack.rules}
    assert set(by_id["T1"].requires) == {"auth"}
    assert set(by_id["T3"].requires) == {"auth", "assets"}
    assert set(by_id["T4"].requires) == {"edr", "auth"}
    assert by_id["T1"].join_keys == []
    assert by_id["T3"].join_keys == by_id["T4"].join_keys == ["主机"]
    for rule in pack.rules:
        # 异构校验器：实现用 pandas，校验器用 stdlib，否则两边会一起错在同一个地方
        assert "pandas" in rule.reference_code
        assert "csv.DictReader" in rule.verify_code and "pandas" not in rule.verify_code
        assert "DATA_PATH_" in rule.reference_code and "DATA_PATH_" in rule.verify_code


def test_role_env_resolves_to_the_right_files(pack, bundle):
    for rule_id, expected in (
        ("T1", {"auth": "auth.csv"}),
        ("T3", {"auth": "auth.csv", "assets": "assets.csv"}),
        ("T4", {"edr": "edr.csv", "auth": "auth.csv"}),
    ):
        assert _roles(pack, bundle, pack.rule(rule_id)) == expected, rule_id
    # 注入实现/校验器的环境变量名按角色大写，与 pack 内代码读的键一致
    assert set(role_env(pack, bundle, pack.rule("T3"))) == {"DATA_PATH_AUTH", "DATA_PATH_ASSETS"}


def test_role_resolution_survives_upload_reorder(pack, bundle, bundle_reordered):
    tasks_a = {t["code_hint"]: t for t in pack_plan_tasks(pack, bundle)}
    tasks_b = {t["code_hint"]: t for t in pack_plan_tasks(pack, bundle_reordered)}
    for rule in pack.rules:
        hint = f"rule_pack:{rule.id}"
        assert _roles(pack, bundle, rule) == _roles(pack, bundle_reordered, rule), rule.id
        ctx_a = SimpleNamespace(bundle=bundle, pack=pack, data_path=bundle.primary.path)
        ctx_b = SimpleNamespace(
            bundle=bundle_reordered, pack=pack, data_path=bundle_reordered.primary.path
        )
        assert _source_for_path(bundle, primary_path(ctx_a, tasks_a[hint])) == _source_for_path(
            bundle_reordered, primary_path(ctx_b, tasks_b[hint])
        ), rule.id


def test_plan_tasks_carry_refs_and_primary(pack, bundle):
    tasks = pack_plan_tasks(pack, bundle)
    assert [t["code_hint"] for t in tasks] == ["rule_pack:T1", "rule_pack:T3", "rule_pack:T4"]
    single = _task(pack, bundle, "T1")
    # 单角色规则不构成跨表任务（不收窄授权范围），但主表必须钉死
    assert single["dataset_refs"] == [] and not scope_refs(single)
    assert single["primary_ref"] == _table(bundle, "auth.csv").id
    assert primary_path(SimpleNamespace(bundle=bundle, pack=pack), single) == str(
        _table(bundle, "auth.csv").path
    )
    for rule_id in ("T3", "T4"):
        task = _task(pack, bundle, rule_id)
        assert len(task["dataset_refs"]) == 2, task
        assert task["join_keys"] == ["主机"]
        assert sorted(_source_of(bundle, ref) for ref in task["dataset_refs"]).count("auth.csv") == 1


def test_join_pairs_gives_both_sides_actual_column_names(pack, bundle):
    ctx = SimpleNamespace(bundle=bundle, pack=pack)
    by_rule = {
        rule_id: join_pairs(ctx, _task(pack, bundle, rule_id)) for rule_id in ("T3", "T4")
    }
    # 两侧实际列名不同（src_ip vs 主机），执行器据此写 rename、校验器据此重放
    for rule_id, pairs in by_rule.items():
        assert len(pairs) == 1, rule_id
        pair = pairs[0]
        assert pair["canonical"] == "主机"
        assert {pair["left_column"], pair["right_column"]} == {"src_ip", "主机"}


def test_rule_with_unresolvable_role_is_dropped_not_guessed(pack, bundle):
    """宁缺毋滥：角色找不到承载表时这条规则不生成任务，而不是悄悄指向另一张表。"""
    stripped = SimpleNamespace(
        tables=[t for t in bundle.tables if "是否生产" not in t.columns],
        documents=[],
        primary=bundle.primary,
    )
    assert [t["rule_params"]["id"] for t in pack_plan_tasks(pack, stripped)] == ["T1", "T4"]


# ------------------------------------------------------------ 两套实现一致 + 与独立重算一致


@pytest.mark.parametrize("rule_id", ["T1", "T3", "T4"])
def test_reference_implementation_matches_independent_recount(pack, bundle, tmp_path, rule_id):
    produced = _produce(pack, bundle, pack.rule(rule_id), tmp_path / rule_id)
    reported = {f["subject"]: f["value"] for f in produced["findings"]}
    assert reported == _recount()[rule_id], rule_id
    assert produced["aggregate"] == {f"规则{rule_id}命中数": len(reported)}
    assert produced["rows"] == len(reported)


@pytest.mark.parametrize("rule_id", ["T1", "T3", "T4"])
def test_verify_code_agrees_with_reference_code(pack, bundle, tmp_path, rule_id):
    rule = pack.rule(rule_id)
    produced = _produce(pack, bundle, rule, tmp_path / f"{rule_id}_ref")
    outcome = verify_findings(
        pack=pack,
        task=_task(pack, bundle, rule_id),
        result={"summary": produced},
        data_path=str(_primary_of(pack, bundle, rule)),
        bundle=bundle,
    )
    assert outcome["status"] == "pass", outcome
    assert {f["subject"] for f in outcome["expected"]} == set(_recount()[rule_id])


@pytest.mark.parametrize(
    "rule_id,subject",
    [("T3", TRAP_NON_PRODUCTION), ("T3", TRAP_EXTERNAL), ("T4", TRAP_NOISY_EDR)],
)
def test_reporting_a_trap_is_caught_as_false_positive(pack, bundle, rule_id, subject):
    """误报会被独立校验抓住：陷阱主体在两套实现里都不该出现，出现即"多报"。"""
    assert subject not in _recount()[rule_id]
    rule = pack.rule(rule_id)
    outcome = verify_findings(
        pack=pack,
        task=_task(pack, bundle, rule_id),
        result={"summary": {"findings": [{"rule_id": rule_id, "subject": subject, "value": 99}]}},
        data_path=str(_primary_of(pack, bundle, rule)),
        bundle=bundle,
    )
    assert outcome["status"] == "fail"
    assert "误报" in outcome["message"]


@pytest.mark.parametrize("rule_id", ["T3", "T4"])
def test_verify_reads_the_same_role_tables(pack, bundle, rule_id):
    """校验器读的是角色解析出来的表：抽掉认证日志后它应当算不出来（skipped），而不是拿主表硬算。"""
    stripped = SimpleNamespace(
        tables=[t for t in bundle.tables if "auth_result" not in t.columns],
        documents=[],
        primary=bundle.primary,
    )
    assert not role_env(pack, stripped, pack.rule(rule_id))
    outcome = verify_findings(
        pack=pack,
        task={"task_id": 1, "rule_params": {"id": rule_id}},
        result={"summary": {"findings": []}},
        data_path=str(stripped.primary.path),
        bundle=stripped,
    )
    assert outcome["status"] == "skipped", outcome


# ------------------------------------------------------------ 端到端（mock）


def test_e2e_triage_run(pack, tmp_path):
    result = run_analysis(
        "生产域主机的异常告警有哪些？哪些需要立刻处置",
        [str(AUTH), str(ASSETS), str(EDR)],
        outputs_root=tmp_path,
        pack="sigma_triage",
    )
    assert result["status"] == "success", (result.get("status"), result.get("degraded_reason"))
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    expected = _recount()

    by_rule: dict[str, dict[str, int]] = defaultdict(dict)
    for row in evaluation["results"].values():
        for finding in (row.get("summary") or {}).get("findings") or []:
            by_rule[str(finding["rule_id"])][str(finding["subject"])] = finding["value"]
    assert dict(by_rule) == {key: expected[key] for key in ("T1", "T3", "T4")}

    verdicts = [row.get("verdict") or {} for row in evaluation["results"].values()]
    assert len(verdicts) == 3
    assert all(v.get("verification") == "ok" for v in verdicts)
    assert all(v.get("status") == "PASS" for v in verdicts)

    # 跨表任务在派发前经过预检，且预检算出的连接行数与独立重算的 inner join 一致
    preflight = evaluation.get("join_preflight") or {}
    assert len(preflight) == 2, preflight
    for entry in preflight.values():
        assert entry["ok"] and len(entry["refs"]) == 2, entry
        for pair in entry["pairs"]:
            assert pair["key"] == "主机"
            assert {pair["left_column"], pair["right_column"]} == {"src_ip", "主机"}
    assert sorted(entry["pairs"][0]["expected_rows"] for entry in preflight.values()) == sorted(
        [expected["JOIN_AUTH_ASSETS"], expected["JOIN_EDR_AUTH"]]
    )

    report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
    for section in pack.report_sections:
        assert section in report
    assert "203.0.113.7->admin" in report
    assert "10.0.0.7" in report  # 生产域 7 次失败
    for trap in (TRAP_NON_PRODUCTION, TRAP_NOISY_EDR):
        assert trap not in report
    # 报告头不得再把"按文件名排第一的表"说成"认证日志"，也不得给出没有依据的行数
    assert "主表记录数" not in report
    for name, count in expected["ROWS"].items():
        assert f"{name}.csv {count} 行" in report
    assert "独立复算" in report


def test_e2e_conclusion_is_independent_of_upload_order(tmp_path):
    """换上传顺序（表 id 全变），分诊队列必须逐行一致——角色寻址的全部意义就在这。"""
    queues: list[list[str]] = []
    for index, order in enumerate(([AUTH, ASSETS, EDR], [EDR, AUTH, ASSETS])):
        result = run_analysis(
            "生产域主机的异常告警有哪些？哪些需要立刻处置",
            [str(path) for path in order],
            outputs_root=tmp_path / f"order{index}",
            pack="sigma_triage",
        )
        assert result["status"] == "success", (order, result.get("degraded_reason"))
        report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
        queues.append(
            [line for line in report.splitlines() if line.startswith("| ") and "---" not in line]
        )
    assert queues[0] == queues[1]
    assert any("203.0.113.7->admin" in line for line in queues[0])
    assert not any(TRAP_NON_PRODUCTION in line for line in queues[0])
