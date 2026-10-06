"""事实层三元组等值：报告印下的每一行，必须与账本里的 (主体, 指标, 数值) 逐字相等。

这条线补的是数字可追溯率放过的那一段。一份真实分诊报告过滤后剩 38 个数字，进那条线的
只有 1 个——`生产域失败次数 = 7`、`命中统计 T1=1` 这些一百以下的整数全在防线之外；
而且追溯率只问"这个数在不在账本里"，不问"这个数配的是不是这个主体"：把 A 机器的 7 次
印成 B 机器的 9 次，两个数都在同一本账里，追溯率一分不掉。SOC 里恰恰是这种张冠李戴最贵。

这里不做的位置假设是刻意的：按列位置取"第 6 列是数值"和本项目已知的那类盲区
（按 task_id 位置贴标签）是同一种错误，所以匹配只看格子的集合。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentflow.core.pack import load_pack
from agentflow.core.report_lint import declared_layers, fact_layer_rows, lint_fact_triples
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIAGE = PROJECT_ROOT / "demo" / "data" / "triage"
TRIAGE_CLEAN = PROJECT_ROOT / "demo" / "data" / "triage_clean"
LOGIN = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"

TRIAGE_QUESTION = "生产域主机的异常告警有哪些？哪些需要立刻处置"
LOGIN_QUESTION = "对今天的登录日志做安全审计"


def _verified_findings(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    """只收"经独立复算一致"的任务里的发现。

    `lint_fact_triples` 比的是"报告说的 == 账本记的"；账本自己有没有被复算过是上一道工序
    （`verdict.verification`）的事，这条线不能替它担保，所以调用方要把没背书的挡在门外。
    """
    out: list[dict[str, Any]] = []
    for row in (evaluation.get("results") or {}).values():
        verdict = (row or {}).get("verdict") or {}
        if verdict.get("verification") != "ok":
            continue
        out.extend((row.get("summary") or {}).get("findings") or [])
    return out


def _run(tmp_path: Path, sources: list[Path], question: str, pack: str) -> tuple[str, list[dict[str, Any]], dict]:
    result = run_analysis(question, [str(path) for path in sources], outputs_root=tmp_path, pack=pack)
    assert result["status"] == "success", (result["status"], result.get("degraded_reason"))
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
    layers = declared_layers(load_pack(pack)) or {}
    return report, _verified_findings(evaluation), layers


def _lint(report: str, findings: list[dict[str, Any]], layers: dict) -> list[str]:
    return [issue["message"] for issue in lint_fact_triples(report, findings, layers)]


# ------------------------------------------------------------------ 真跑：两个包都对得上


def test_triage_fact_layer_matches_the_ledger_row_by_row(tmp_path):
    report, findings, layers = _run(
        tmp_path, [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"], TRIAGE_QUESTION, "sigma_triage"
    )
    rows = fact_layer_rows(report, layers["fact"])
    assert findings and rows, "前提没成立：这本账是空的，断言就是空的"
    assert len(rows) == len(findings), f"事实层 {len(rows)} 行、账本 {len(findings)} 条，行数先对上"
    assert _lint(report, findings, layers) == []


def test_login_audit_fact_layer_matches_too(tmp_path):
    """第二个包：这条线不能只对着分诊模板成立，否则它是模板的附件而不是防线。"""
    report, findings, layers = _run(tmp_path, [LOGIN], LOGIN_QUESTION, "login_audit")
    assert findings, "登录审计夹具里必须埋着攻击，否则这条用例什么都没测"
    assert len(fact_layer_rows(report, layers["fact"])) == len(findings), "发现清单那几行要先被认出来"
    assert _lint(report, findings, layers) == []


# ------------------------------------------------------------------ 改坏必须红（四条）


def test_swapping_values_between_two_hosts_is_caught(tmp_path):
    """张冠李戴：把 10.0.0.7 的 7 次印成 9 次。追溯率看不见这一格，这里必须红。"""
    report, findings, layers = _run(
        tmp_path, [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"], TRIAGE_QUESTION, "sigma_triage"
    )
    original = next(line for line in report.splitlines() if "10.0.0.7" in line and "生产域失败次数" in line)
    assert "| 7 |" in original, original
    broken = report.replace(original, original.replace("| 7 |", "| 9 |"))
    assert broken != report, "替换没生效，断言会是空的"

    issues = _lint(broken, findings, layers)
    assert issues, "数值配错主体却没判红：这条线等于没装"
    assert any("10.0.0.7" in message for message in issues), issues


def test_dropping_one_row_is_caught(tmp_path):
    """算出来没说出去：整行从事实层消失，要点名是哪个主体。"""
    report, findings, layers = _run(
        tmp_path, [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"], TRIAGE_QUESTION, "sigma_triage"
    )
    dropped = next(line for line in report.splitlines() if "203.0.113.7->admin" in line and line.startswith("| "))
    broken = report.replace(dropped, "")

    issues = _lint(broken, findings, layers)
    assert any("203.0.113.7->admin" in message for message in issues), issues


def test_invented_row_is_caught(tmp_path):
    """凭空多一行：这台主机不在账本里，哪怕数值、指标名都借得对。"""
    report, findings, layers = _run(
        tmp_path, [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"], TRIAGE_QUESTION, "sigma_triage"
    )
    anchor = next(line for line in report.splitlines() if line.startswith("| 1 |"))
    forged = "| 99 | critical | 生产域主机大量认证失败（资产加权）（T3） | 10.0.0.99 | 生产域失败次数 | 5 | ~ |"
    broken = report.replace(anchor, f"{anchor}\n{forged}")
    assert forged in broken

    issues = _lint(broken, findings, layers)
    assert any("10.0.0.99" in message for message in issues), issues


def test_rows_without_any_ledger_entry_are_false_positives(tmp_path):
    """账本零命中而事实层有行 = 误报，不许当成"空语义通过"。"""
    report, findings, layers = _run(
        tmp_path, [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"], TRIAGE_QUESTION, "sigma_triage"
    )
    assert findings, "前提：这批数据本来就该有命中"
    issues = _lint(report, [], layers)
    assert len(issues) == 1 and "误报" in issues[0], issues


# ------------------------------------------------------------------ 边界与列序无关


def test_empty_semantics_pass_cleanly(tmp_path):
    """零命中且事实层没有数据行 ⇒ 通过。这条与上一条配对，缺一条就是半套防线。"""
    report, findings, layers = _run(
        tmp_path,
        [TRIAGE_CLEAN / "auth.csv", TRIAGE_CLEAN / "assets.csv", TRIAGE_CLEAN / "edr.csv"],
        TRIAGE_QUESTION,
        "sigma_triage",
    )
    assert findings == [], f"这批 clean 数据本该零命中，实得 {len(findings)} 条——用例前提变了"
    assert fact_layer_rows(report, layers["fact"]) == []
    assert _lint(report, findings, layers) == []


def _reverse_columns(text: str) -> str:
    """把报告里每个表格行的格子顺序整体倒过来（表头与对齐行一起倒，保持是合法表格）。"""
    out: list[str] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            out.append(line)
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        out.append("| " + " | ".join(reversed(cells)) + " |")
    return "\n".join(out)


def test_column_order_does_not_change_the_verdict(tmp_path):
    """把每一行的格子顺序整体倒过来，结论不许变。

    这一条守的是本项目已知的那类盲区：按位置取列 = 模板一改就静默对错题。
    """
    report, findings, layers = _run(
        tmp_path, [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"], TRIAGE_QUESTION, "sigma_triage"
    )
    shuffled = _reverse_columns(report)
    assert shuffled != report, "倒序没起作用，断言会是空的"
    assert fact_layer_rows(shuffled, layers["fact"]), "倒序后一行数据都没认出来，那条断言是空的"
    assert _lint(shuffled, findings, layers) == []


# ------------------------------------------------------- 谓词与题面：这条线要能被门禁消费

# 上面那些测的是 `lint_fact_triples` 这把尺本身。下面这几条测的是"尺子接在门禁上"：
# 注册进 PREDICATES、真实产物能过、题里真的在用它、没背书的发现不许被它盖章。


def _evidence(outputs_dir: Path) -> dict[str, Any]:
    from agentflow.core.grading import load_evidence

    return load_evidence(str(outputs_dir))


def _predicate(evidence: dict[str, Any], pack: str, **extra: Any) -> tuple[bool, str]:
    from agentflow.core.grading import PREDICATES

    return PREDICATES["fact_triples"](evidence, {"pack": pack, **extra}, "mock")


def test_predicate_certifies_a_real_triage_run(tmp_path):
    """真实产物（不是合成证据）必须能被这条线盖章——否则它只是一把没接上电的尺。"""
    sources = [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"]
    result = run_analysis(TRIAGE_QUESTION, [str(p) for p in sources], outputs_root=tmp_path, pack="sigma_triage")
    ok, detail = _predicate(_evidence(Path(result["outputs_dir"])), "sigma_triage")
    assert ok, detail
    assert "6 条发现" in detail, detail  # 不是"空对空通过"，是真核了 6 行


def test_predicate_catches_a_value_moved_between_two_subjects(tmp_path):
    """把 10.0.0.7 的 7 印成 9：追溯率一分不掉（9 也在同一本账里），这条线必须红。"""
    sources = [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"]
    result = run_analysis(TRIAGE_QUESTION, [str(p) for p in sources], outputs_root=tmp_path, pack="sigma_triage")
    evidence = _evidence(Path(result["outputs_dir"]))
    row = next(line for line in evidence["report"].splitlines() if line.startswith("| 4 |"))
    assert "10.0.0.7" in row and "| 7 |" in row, row
    from agentflow.core.grading import numbers_traceable

    before = numbers_traceable(evidence)[0]
    evidence["report"] = evidence["report"].replace(row, row.replace("| 7 |", "| 9 |"), 1)
    after = numbers_traceable(evidence)[0]
    # 这句话就是整条线的存在理由：张冠李戴之后，追溯率一分没掉
    assert after == before == 1.0, (before, after)
    ok, detail = _predicate(evidence, "sigma_triage")
    assert not ok and "找不到对应发现" in detail, detail


def test_predicate_refuses_to_certify_unverified_findings(tmp_path):
    """没过独立复算的发现，这条线拒绝为其背书——而且要说清是哪个任务。

    它比的是"报告 == 账本"。账本自己没被复算过时让这条线点头，等于让一条没复核的数字
    去给另一条数字作证，那条线只剩自证。这里把报告换成"没有数据行"的样子，
    让"逐行等值"本身无从反驳（lint 通过），从而单独测这道背书闸门。
    """
    sources = [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"]
    result = run_analysis(TRIAGE_QUESTION, [str(p) for p in sources], outputs_root=tmp_path, pack="sigma_triage")
    evidence = _evidence(Path(result["outputs_dir"]))
    fact_section = (declared_layers(load_pack("sigma_triage")) or {}).get("fact") or []
    evidence["report"] = f"## {fact_section[0]}\n\n本轮无队列行。\n"
    unverified = 0
    for task_id, row in evidence["evaluation"]["results"].items():
        if ((row or {}).get("summary") or {}).get("findings"):
            row["verdict"]["verification"] = "mismatch"
            unverified += 1
    assert unverified, "这份产物里没有带发现的任务，断言会是空的"
    ok, detail = _predicate(evidence, "sigma_triage")
    assert not ok and "未通过独立复算" in detail, detail


def test_predicate_says_so_when_the_pack_declared_nothing(monkeypatch, tmp_path):
    """包没承诺分档 ⇒ 报告"无承诺可核对"，不许退化成"没有破口所以通过"。"""
    import agentflow.core.pack as pack_module

    sources = [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"]
    result = run_analysis(TRIAGE_QUESTION, [str(p) for p in sources], outputs_root=tmp_path, pack="sigma_triage")
    evidence = _evidence(Path(result["outputs_dir"]))
    monkeypatch.setattr(pack_module, "load_pack", lambda name: SimpleNamespace(report_layers={}))
    ok, detail = _predicate(evidence, "sigma_triage")
    assert not ok and "未声明 report_layers" in detail, detail


def test_the_suite_actually_consumes_the_predicate():
    """注册了但题里没人用 = 这条线只在单测里活着；题里写了但没注册 = lint 当场红。两边都钉。"""
    import yaml

    from agentflow.core.grading import PREDICATES, lint_suite

    assert "fact_triples" in PREDICATES
    suite = yaml.safe_load((PROJECT_ROOT / "evals" / "suite.yaml").read_text(encoding="utf-8"))
    lint_suite(suite["cases"])  # 谓词名拼错/未注册会在这里抛
    gated = {case["id"]: ((case.get("mock") or {}).get("gate") or {}) for case in suite["cases"]}
    for case_id, pack in [
        ("E14", "login_audit"),
        ("E16", "login_audit"),
        ("E21", "sigma_triage"),
        ("E23", "sigma_triage"),
        ("E24", "sigma_triage"),
    ]:
        assert "fact_triples" in gated[case_id], f"{case_id} 没消费这条线"
        params = gated[case_id]["fact_triples"]
        assert params.get("pack") == pack, (case_id, params)
        # 锚点只挂在有 golden 数值映射的题上；写了 anchor 但 golden 里没这个键 ⇒ 谓词判红
        anchor = params.get("anchor")
        if anchor:
            assert isinstance((suite.get("golden") or {}).get(anchor), dict), (case_id, anchor)
        else:
            assert case_id in {"E14", "E16"}, f"{case_id} 应当带锚点：sigma 题的 golden 里有 fact_values"
    # 没带场景包的题不许被顺手拉进来（零售单表题没有"账本发现"这个概念）
    assert "fact_triples" not in gated["E01"], "E01 是零售单表题，这条线对它无意义"


# ---------------------------------------------------------------- 腿 2 / 腿 3：账本 == 复算 == 独立锚点


def _triage_run(tmp_path):
    sources = [TRIAGE / "auth.csv", TRIAGE / "assets.csv", TRIAGE / "edr.csv"]
    result = run_analysis(TRIAGE_QUESTION, [str(p) for p in sources], outputs_root=tmp_path, pack="sigma_triage")
    return _evidence(Path(result["outputs_dir"]))


def test_recompute_triples_are_persisted_even_when_verification_passes(tmp_path):
    """复算侧的三元组必须在 **PASS** 时也落盘：出事才留证据等于平时没法核对。"""
    evidence = _triage_run(tmp_path)
    rows = [row for row in evidence["evaluation"]["results"].values() if row]
    with_findings = [row for row in rows if ((row.get("summary") or {}).get("findings"))]
    assert with_findings, "这份产物里没有带发现的任务，断言会是空的"
    for row in with_findings:
        recompute = (row.get("verdict") or {}).get("recompute") or []
        assert recompute, f"复算侧三元组没落盘：{row.get('verdict')}"
        assert all({"subject", "metric", "value"} <= set(item) for item in recompute), recompute
    ledger = sum(len((row.get("summary") or {}).get("findings") or []) for row in with_findings)
    stored = sum(len((row.get("verdict") or {}).get("recompute") or []) for row in with_findings)
    assert stored == ledger, f"账本 {ledger} 条，复算侧只存了 {stored} 条"


def _find_ledger(evidence: dict[str, Any], rule: str, subject: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """按 (规则, 主体) 点名账本里的那一行。

    不用"第一个带发现的任务"：`evaluation.json` 里 `results` 的键序跟着线程完成顺序走，
    内容确定但顺序不确定——拿顺序当身份，用例就会今天绿明天红（2026-10-06 变异复测的
    对照位点 F0 就是这么抓出来的）。
    """
    for task_id, row in evidence["evaluation"]["results"].items():
        for item in ((row or {}).get("summary") or {}).get("findings") or []:
            if str(item.get("rule") or item.get("rule_id") or "") == rule and str(item.get("subject")) == subject:
                return str(task_id), row, item
    raise AssertionError(f"账本里找不到 {rule} {subject}")


def _swear_report_row(evidence: dict[str, Any], subject: str, old: int, new: int, rule: str) -> None:
    """把事实层里那一行的数值改掉，并且只改那一行（行内必须同时含主体、规则标签与旧值）。"""
    line = next(
        text
        for text in evidence["report"].splitlines()
        if text.strip().startswith("|") and subject in text and f"| {old} |" in text and f"（{rule}）" in text
    )
    evidence["report"] = evidence["report"].replace(line, line.replace(f"| {old} |", f"| {new} |", 1), 1)


def test_ledger_changed_but_report_follows_it_is_caught_by_the_recompute_leg(tmp_path):
    """账本被改、报告照抄改后的账本 ⇒ 腿 1 全绿，腿 2 必须红。

    这正是"只有报告==账本"那条线的能力边界：账本自己被人动过，而报告老实印出来，
    两格都对得上，只有拿复算侧比才露出来。
    """
    evidence = _triage_run(tmp_path)
    anchor = "sigma_attack_fact_values"
    evidence["golden"] = {anchor: _derive_anchor()}
    _task_id, _row, item = _find_ledger(evidence, "T3", "10.0.0.7")
    old = int(item["value"])
    assert old == 7, f"题面假设 T3/10.0.0.7 = 7，实测 {old}：数据变了，锚点也得跟着重算"
    item["value"] = old + 3
    _swear_report_row(evidence, "10.0.0.7", old, old + 3, "T3")
    ok, detail = _predicate(evidence, "sigma_triage", anchor=anchor)
    assert not ok, detail
    assert "账本 != 复算" in detail and "10.0.0.7" in detail, detail


def test_a_verifier_copying_the_producer_is_caught_by_the_external_anchor(tmp_path):
    """**这条是"校验器照抄生产实现"的变异用例**：腿 1、腿 2 都会绿，只有外部锚点能拦。

    做法是把复算侧改成账本的副本（producer 与 verifier 同源），同时把账本的一个数值改错。
    此时两腿等值完全成立——它与"全都对"长得一模一样；拿系统之外独立重算的数值一比才红。
    这也是为什么这条线不能只有"报告 == 账本 == 复算"三方就自称够了。
    """
    evidence = _triage_run(tmp_path)
    anchor = "sigma_attack_fact_values"
    evidence["golden"] = {anchor: _derive_anchor()}
    task_id, row, item = _find_ledger(evidence, "T3", "10.0.0.7")
    old = int(item["value"])
    item["value"] = old + 3  # 生产侧错了
    _swear_report_row(evidence, "10.0.0.7", old, old + 3, "T3")
    # 校验器照抄：把该任务的复算侧整个换成账本的副本
    row["verdict"]["recompute"] = [
        {
            "subject": str(finding.get("subject")),
            "metric": str(finding.get("metric")),
            "value": str(finding.get("value")),
        }
        for finding in (row.get("summary") or {}).get("findings") or []
    ]
    # 先坐实"两条腿拦不住"，才说明锚点不是摆设
    ok_two_legs, detail_two_legs = _predicate(evidence, "sigma_triage")
    assert ok_two_legs, f"两腿就拦住了？那这条用例证明不了锚点的必要：{detail_two_legs}"
    ok, detail = _predicate(evidence, "sigma_triage", anchor=anchor)
    assert not ok and "锚点" in detail and "10.0.0.7" in detail, detail
    assert f"{old + 3}" in detail and f"独立重算={old}" in detail, detail


def test_declaring_an_anchor_that_does_not_exist_is_red_not_skipped(tmp_path):
    """写了 anchor 而 golden 里没这个键 ⇒ 判红。静默跳过就是把"没比"说成"比过了"。"""
    evidence = _triage_run(tmp_path)
    evidence["golden"] = {}
    ok, detail = _predicate(evidence, "sigma_triage", anchor="sigma_nope_fact_values")
    assert not ok and "在 golden 里不存在" in detail, detail


def test_findings_without_a_finding_level_check_cannot_claim_three_way(tmp_path):
    """只有 aggregate 背书时不许自称"三方"：那条路给不出逐主体的复算值。"""
    evidence = _triage_run(tmp_path)
    for row in evidence["evaluation"]["results"].values():
        verdict = row.get("verdict") or {}
        if verdict.get("checks"):
            verdict["checks"] = ["aggregate_match_check:PASS"]
    ok, detail = _predicate(evidence, "sigma_triage")
    assert not ok and "只比了两方" in detail, detail


def _derive_anchor() -> dict[str, Any]:
    """独立锚点的测试副本：直接读原始 CSV 数（stdlib csv），不借系统任何一段代码。

    与 `scripts/run_eval.py` 里 pandas 那条路径**互为对照**——两边都对得上，
    才说得上"锚点这个数不是抄系统输出的"。
    """
    import csv
    from collections import defaultdict

    values: dict[str, dict[str, int]] = {"T1": {}, "T3": {}, "T4": {}}
    auth = list(csv.DictReader((TRIAGE / "auth.csv").open(encoding="utf-8-sig")))
    assets = list(csv.DictReader((TRIAGE / "assets.csv").open(encoding="utf-8-sig")))
    edr = list(csv.DictReader((TRIAGE / "edr.csv").open(encoding="utf-8-sig")))
    failed = [line for line in auth if line.get("auth_result") == "failed"]
    bursts: dict[str, int] = defaultdict(int)
    for line in failed:
        bursts[f"{line['src_ip']}->{line['account']}"] += 1
    per_host: dict[str, int] = defaultdict(int)
    for line in failed:
        per_host[line["src_ip"]] += 1
    production = {row["主机"] for row in assets if row.get("是否生产") == "Y"}
    high = {row["主机"] for row in edr if row.get("严重级") == "high"}
    for subject, count in bursts.items():
        if count >= 8:
            values["T1"][subject] = count
    for host, count in per_host.items():
        if host in production and count >= 3:
            values["T3"][host] = count
        if host in high and count >= 2:
            values["T4"][host] = count
    return values
