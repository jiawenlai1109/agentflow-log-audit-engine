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
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 与 core/grading.py 的数字口径保持一致（两处各留一份是刻意的：运行侧不该依赖评分器，
# 但任何一方改了口径都必须同步另一方，否则"绿"的含义在两侧不同）
_META_NUMBER_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}:\d{2}:\d{2}\b")
_DOTTED_QUAD_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

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
    return issues


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
