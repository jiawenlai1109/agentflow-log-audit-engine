"""report_lint：报告分档的确定性检查（M3-3）。

"三档分离"（事实 → 建议 → 推断）是场景包模板对读者的承诺。承诺只写在模板注释里，
下一次改叙述就会悄悄破掉。这里把它变成四条可判定的检查，两个消费方共用同一口径：

- Critic（`core/tools._check_report`）：运行时判红，问题清单回流 Reporter 重写；
- 评分器（`core.grading._p_report_layers`）：门禁看得见，绿/红可回归。

检查的是**结构契约**，不是措辞：
1. 声明过的每一档都必须真的出现在报告里（模板与包配置对不上 ⇒ 该档永远不会被读到）；
2. 每条 finding 的主体必须出现在事实层（漏一条 = 报告没把算出来的东西说出去）；
3. 每条 finding 的主体必须出现在建议层（有发现没处置 = 报告不可行动）；
4. 推断层出现的每个数字，都必须能在事实层 / 建议层 / 头部 / 规则阈值里找到出处
   ——**模型可以解读，不可以造数**（I1 在报告侧的形式）。

刻意不算的东西：语义是否"合理"、句子通不通——那是 LLM 评审的活，且不可判定。

另有一个入口 `lint_fact_triples`：事实层每一行的 (主体, 指标, 数值) 与账本双向逐字相等。
它不在上面四条里，因为那四条只查"主体在不在档里"，而分诊场景最要害的数值（失败几次、
命中几条）全在一百以下，数字可追溯率那条线按大小把它们放过了。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 与 core/grading.py 的数字口径保持一致（两处各留一份是刻意的：运行侧不该依赖评分器，
# 但两边必须同判据——2026-10-07 一起补 #46 的三条掩码时也是两处同改）。
#
# 为什么这里没有 `\b`：Python 的 `\w` 含汉字，"阻断10.0.0.15" 里 断 与 1 之间**不构成
# 词边界**，于是 IP 没被挖掉、评分侧把 `0.15` 当成一个没人背书的数（real E23 实测
# 追溯率被压到 0.5000）。中文报告里的数字旁边就是汉字，边界只能按"前后是不是数字/点"判。
_META_NUMBER_RE = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)|(?<!\d)\d{2}:\d{2}:\d{2}(?!\d)|(?<!\d)\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?"
)
_DOTTED_QUAD_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
# 但任何一方改了口径都必须同步另一方，否则"绿"的含义在两侧不同。
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
# 千分位分隔符（缺陷 #46，2026-10-07 用 real 产物实测）：报告把账本里的 1234 写成 "1,234"
# 时，上面那条会拆成 1 与 234 两个数 ⇒ "老实抄了数字"被判成造数。
# 实测数字（同一份 report.md / evaluation.json，只换尺子）：E01 0.5385 → 0.9091（去千分位）
# → 1.0000（再补上头两条 CJK 边界掩码）；E23 0.5000 → 1.0000（全靠 CJK 边界）。
# 判据只认"逗号后面正好三个数字且不再跟数字"，不动 "1, 234"（列表）也不动 12345。
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")

FACT = "fact"
ACTION = "action"
INFERENCE = "inference"
HEADER_HEADING = "头部"


def sections(text: str) -> list[tuple[str, str]]:
    """按二级标题切报告：返回 [(标题行, 正文)]，第一段（标题之前的引用块）记为「头部」。"""
    out: list[tuple[str, str]] = []
    heading, buf = HEADER_HEADING, []
    for line in text.splitlines():
        if line.startswith("## "):
            out.append((heading, "\n".join(buf)))
            heading, buf = line.strip(), []
        else:
            buf.append(line)
    out.append((heading, "\n".join(buf)))
    return out


def layer_text(text: str, names: Iterable[str]) -> str:
    """若干档名对应的正文合并（档名按子串匹配标题，容忍"## 一、分诊队列（事实层）"这种写法）。"""
    wanted = [str(name) for name in names if str(name).strip()]
    if not wanted:
        return ""
    return "\n".join(
        body for heading, body in sections(text) if any(name in heading for name in wanted)
    )


def declared_layers(pack: Any) -> dict[str, list[str]] | None:
    """包声明的分档映射；没声明就返回 None（分档检查只对承诺过的包生效）。"""
    layers = getattr(pack, "report_layers", None) or {}
    return {str(k): [str(n) for n in v] for k, v in layers.items()} if layers else None


def scan_numbers(text: str) -> set[float]:
    """报告文本里"构成结论的数字"：先挖掉时间戳与点分四段（IP/主机名），否则全是假红。"""
    cleaned = _DOTTED_QUAD_RE.sub(" ", text or "")
    for token in _META_NUMBER_RE.findall(cleaned):
        cleaned = cleaned.replace(token, " ")
    cleaned = _THOUSANDS_RE.sub("", cleaned)
    out: set[float] = set()
    for token in _NUMBER_RE.findall(cleaned):
        try:
            value = float(token)
        except ValueError:
            continue
        if _is_claim(value):
            out.add(value)
    return out


def _is_claim(value: float) -> bool:
    """与 `core/grading.numbers_traceable` 同口径：小整数（序号、规则号、队列条数）不算结论。

    这条口径**刻意偏宽**——规则号 T1/T3 自己就往池子里贡献了 1 和 3，所以本检查拦得住
    "凭空写出 1200 台 / 占比 47.5%"，拦不住"把 3 台说成 4 台"。后者是语义正确性，
    归 LLM 评审管；把它塞进确定性检查只会制造假红，而假红会把人训练成忽略这条闸门。
    """
    return not (value == int(value) and abs(value) < 100)


def lint_report(
    text: str,
    findings: list[dict[str, Any]],
    layers: dict[str, list[str]],
    thresholds: Iterable[float] = (),
) -> list[dict[str, str]]:
    """跑分档检查，返回 Critic 的 issue 形状（severity / section / message）。

    `thresholds` 是规则包里的阈值（如 8、3、2）：研判里复述"失败次数 ≥ 3"是引用规则，
    不是造数——它的出处在包内，不在数据里。
    """
    issues: list[dict[str, str]] = []
    parsed = sections(text)
    heads = [heading for heading, _ in parsed]

    fact_names = layers.get(FACT) or []
    action_names = layers.get(ACTION) or []
    inference_names = layers.get(INFERENCE) or []
    for kind, names in ((FACT, fact_names), (ACTION, action_names), (INFERENCE, inference_names)):
        for name in names:
            if not any(name in heading for heading in heads):
                issues.append(
                    {
                        "severity": "high",
                        "section": name,
                        "message": f"报告缺少{kind}档「{name}」：包声明了它，模板却没渲染出来",
                    }
                )

    fact_body = layer_text(text, fact_names) + "\n" + _header_text(parsed)
    action_body = layer_text(text, action_names)
    inference_body = layer_text(text, inference_names)

    subjects = {str(finding.get("subject")) for finding in findings if isinstance(finding, dict)}
    for subject in sorted(subjects):
        if subject and subject not in fact_body:
            issues.append(
                {
                    "severity": "high",
                    "section": "、".join(fact_names) or FACT,
                    "message": f"发现 {subject} 未出现在事实层（算出来却没说出去）",
                }
            )
        if subject and subject not in action_body:
            issues.append(
                {
                    "severity": "high",
                    "section": "、".join(action_names) or ACTION,
                    "message": f"发现 {subject} 没有对应的处置建议（报告不可行动）",
                }
            )

    allowed = scan_numbers(fact_body) | scan_numbers(action_body) | scan_numbers(text.split("## ")[0])
    allowed |= {float(value) for value in thresholds if isinstance(value, (int, float))}
    allowed.add(float(len(findings)))  # 队列长度可数，不算造数
    for value in sorted(scan_numbers(inference_body)):
        if not any(abs(value - known) <= max(1e-6, 0.01 * abs(known)) for known in allowed):
            issues.append(
                {
                    "severity": "high",
                    "section": "、".join(inference_names) or INFERENCE,
                    "message": f"推断层出现事实层没有的数字 {value:g}（模型不得造数）",
                }
            )

    # 缺陷 #49（2026-10-07 real E23 实测）：同一份报告自相矛盾——头部一边列
    # "未能完成的规则：T3"，一边在命中统计里印 `T3=0`。SOC 值班读到 0 就是
    # "这条规则查过了，没问题"，而真因是那条规则根本没跑成。**0 是一个结论**，
    # 它必须由跑过的那道工序给出；没跑成就得说"没跑成"。
    # 这一条不依赖包给了哪些规则号，判据只看报告自己说的话——生产侧改了措辞，
    # 这里照样拦得住（producer 与 verifier 各走各的路）。
    header = _header_text(parsed)
    for rule in sorted(_declared_silent_rules(text, header)):
        if re.search(rf"{re.escape(rule)}\s*=\s*0(?!\d)", header):
            issues.append(
                {
                    "severity": "high",
                    "section": "报告头",
                    "message": (
                        f"{rule} 被自己声明为「未出结论」，命中统计却印成 {rule}=0——"
                        "「无风险」是从「没跑成」里编出来的数"
                    ),
                }
            )
    return issues


def _declared_silent_rules(text: str, header: str) -> set[str]:
    """报告自己承认"没跑成"的规则号。措辞来自包模板，两处都认得：未完成 / 未出结论。"""
    found: set[str] = set()
    for block in re.findall(r"(?:未能完成的规则|未出结论的规则)[:：]([^\n（(]+)", f"{text}\n{header}"):
        found.update(piece.strip() for piece in re.split(r"[、,，]", block) if piece.strip())
    return found


def _header_text(parsed: list[tuple[str, str]]) -> str:
    return "\n".join(body for heading, body in parsed if heading == HEADER_HEADING)


def pack_thresholds(pack: Any) -> list[float]:
    """规则参数里的数值（阈值即事实的出处之一，随包版本化，不是模型生成的）。"""
    out: list[float] = []
    for rule in getattr(pack, "rules", []) or []:
        for value in (getattr(rule, "params", None) or {}).values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append(float(value))
    return out


_ALIGN_RE = re.compile(r"^:?-{2,}:?$")


def row_cells(line: str) -> list[str] | None:
    """把 markdown 表格行拆成单元格；非表格行返回 None。"""
    text = line.strip()
    if not text.startswith("|"):
        return None
    return [cell.strip() for cell in text.strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_ALIGN_RE.match(cell) for cell in cells)


def fact_layer_rows(text: str, fact_names: Iterable[str]) -> list[list[str]]:
    """取事实层里的数据行，跳过表头与对齐行。

    表头按 markdown 惯例认"对齐行的上一行"，不靠列名——列名各包不同（而且按列名认
    等于假设模板永远没人改）。中间隔一行空行也要认得出来，模板渲染会插空行。
    """
    lines = layer_text(text, fact_names).splitlines()

    def next_cells(index: int) -> list[str] | None:
        for cursor in range(index + 1, len(lines)):
            if lines[cursor].strip():
                return row_cells(lines[cursor])
        return None

    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        cells = row_cells(line)
        if cells is None or _is_separator(cells):
            continue
        following = next_cells(index)
        if following is not None and _is_separator(following):
            continue
        rows.append(cells)
    return rows


def _as_cell(value: Any) -> str:
    """数值按报告里印出来的样子比：整数不写成 3.0，否则每一行都对不上。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


# 整个格子就是一个千分位数（1,234 / 1,234,567）时按数值比；带单位或带逗号的文本格不动。
_THOUSANDS_CELL_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+$")


def _plain_number(cell: str) -> str:
    return cell.replace(",", "") if _THOUSANDS_CELL_RE.match(cell.strip()) else cell


def finding_triple(finding: dict[str, Any]) -> tuple[str, str, str] | None:
    """一条发现的 (主体, 指标, 数值) 三元组；缺任何一格就不构成可核对的一行。"""
    subject = str(finding.get("subject") or "").strip()
    metric = str(finding.get("metric") or "").strip()
    value = finding.get("value")
    if not subject or not metric or value is None:
        return None
    return subject, metric, _as_cell(value)


def lint_fact_triples(
    text: str, findings: list[dict[str, Any]], layers: dict[str, list[str]]
) -> list[dict[str, str]]:
    """事实层的每一行都必须与账本的一条发现逐字相等，且账本每条发现都要有一行——双向。

    为什么要有这一条：数字可追溯率按"大小"决定哪些数字要核对，一百以下的整数被放过；
    分诊场景里 `生产域失败次数 = 7`、`命中统计 T1=1` 这些最要紧的数恰好全在防线之外
    （一份真实分诊报告过滤后剩 38 个数字，进那条线的只有 1 个）。而且那条线只问
    "这个数在不在账本里"，不问"这个数配的是不是这个主体"——把 A 主机的 7 次印成
    B 主机的 9 次，两个数都在同一本账里，追溯率一分不掉。SOC 里恰恰是这种张冠李戴
    最贵（办公机被说成生产机，有人半夜去隔离错的主机）。

    两个设计选择是这条线的命门，改了就不是同一条防线：

    1. **整格相等，不用子串**。时间窗那一格 `2026-09-05 13:34:00 ~ …` 里也有一堆数字，
       子串匹配会把 13 当成数值对上号——那种绿灯是假的。
    2. **按格子集合匹配，不按列位置**。表 id 与列序取决于模板和上传顺序；
       按位置取"第 6 列是数值"就是本项目已知的那类盲区（按 task_id 位置贴标签）。

    调用方负责只传"已被独立复算背书"的发现：这里比的是"报告说的 == 账本记的"，
    账本自己有没有被复算过属于上一道工序（`verdict.verification`），不在这条线的能力里。
    """
    issues: list[dict[str, str]] = []
    section = "、".join(layers.get(FACT) or []) or FACT
    rows = fact_layer_rows(text, layers.get(FACT) or [])
    triples = [triples for triples in (finding_triple(item) for item in findings if isinstance(item, dict)) if triples]

    if not triples and rows:
        issues.append(
            {
                "severity": "high",
                "section": section,
                "message": f"账本零命中，事实层却印了 {len(rows)} 行：安全场景里这是误报，不是发现",
            }
        )
        return issues

    matched: set[int] = set()
    for index, cells in enumerate(rows):
        # 千分位归一（缺陷 #46）：账本里的 1234 被模板印成 "1,234" 时，整格相等会把
        # 一行真发现判成"凭空多印的行"。只归一"整个格子就是一个千分位数"的情况，
        # 主体/时间窗那些格子里的逗号一个都不碰。
        occupied = {_plain_number(cell) for cell in cells}
        hit = next(
            (
                position
                for position, (subject, metric, value) in enumerate(triples)
                if subject in occupied and metric in occupied and value in occupied
            ),
            None,
        )
        if hit is None:
            issues.append(
                {
                    "severity": "high",
                    "section": section,
                    "message": f"事实层第 {index + 1} 行在账本里找不到对应发现（数值配错了主体，或是凭空多印的行）：{' | '.join(cells)[:120]}",
                }
            )
            continue
        matched.add(hit)

    for position, (subject, metric, value) in enumerate(triples):
        if position not in matched:
            issues.append(
                {
                    "severity": "high",
                    "section": section,
                    "message": f"账本里的发现 {subject}（{metric}={value}）在事实层没有对应行",
                }
            )
    return issues
