"""记忆工具：相关性检索、可读轮次视图、回忆判定、冲突检测。"""

from __future__ import annotations

import re
from typing import Any

RECALL_KEYWORDS = (
    "之前",
    "上次",
    "上一次",
    "第一轮",
    "第二轮",
    "第一个问题",
    "刚才",
    "还记得",
    "历史",
    "前面",
)

SUMMARIZER_SYSTEM = """你是会话记忆管理员。把旧会话摘要与新一轮对话合并为新的会话摘要 JSON。
输出必须符合结构：
{"goals": ["用户核心目标，只增补不删除，除非用户明确改变目标"],
 "data_refs": ["数据文件/列/时间范围"],
 "key_findings": [{"conclusion": "结论", "value": 数值或null, "turn": 轮次, "run_id": "来源"}],
 "constraints": ["用户约束与偏好"],
 "pending": ["未决事项"],
 "last_focus": "最近一轮的关注点"}
规则：goals 只增补不删除；key_findings 保留重要结论（可截断最旧的）；constraints/pending 去重。"""


def clean_turn(
    question: str,
    summary: str,
    key_numbers: dict[str, float],
    mentioned_columns: list[str],
) -> dict[str, Any]:
    """TurnCleaner：把一轮对话压缩为结构化条目（去语气词 + 结论 + 关键数字 + 涉及列）。"""
    cleaned_question = re.sub(r"^(你好|您好|请问|请|麻烦)", "", question)
    cleaned_question = cleaned_question.replace("吗", "").replace("呢", "").replace("啊", "")
    return {
        "question_clean": cleaned_question.strip(),
        "answer_summary": clean_summary(summary)[:300],
        "key_numbers": key_numbers,
        "mentioned_columns": mentioned_columns,
    }


def merge_summary_mock(
    old_summary: dict[str, Any] | None, turn: dict[str, Any]
) -> dict[str, Any]:
    """Mock 模式确定性摘要合并：goals/constraints 去重追加，key_findings 追加并截断。"""
    old = old_summary or {}
    constraints = list(old.get("constraints", []))
    question = str(turn.get("question", ""))
    if any(keyword in question for keyword in ("以后", "每次", "都", "只", "统一", "一直")):
        constraint = clean_summary(question)[:100]
        if constraint and constraint not in constraints:
            constraints.append(constraint)
    findings = list(old.get("key_findings", []))
    for metric, value in (turn.get("key_numbers") or {}).items():
        findings.append(
            {
                "conclusion": f"{metric}={value}",
                "value": value,
                "turn": turn.get("turn"),
                "run_id": turn.get("run_id"),
            }
        )
    return {
        "goals": list(old.get("goals", [])),
        "data_refs": list(old.get("data_refs", [])),
        "key_findings": findings[-20:],
        "constraints": constraints,
        "pending": list(old.get("pending", [])),
        "last_focus": clean_summary(question)[:50],
    }


def render_summary_text(summary: dict[str, Any] | None) -> str:
    """把结构化摘要渲染为可读文本（供 Planner 注入）。"""
    if not summary:
        return "（无）"
    if "summary" in summary and isinstance(summary["summary"], str):
        # 兼容旧格式快照
        return str(summary["summary"])[:800]
    parts: list[str] = []
    if summary.get("goals"):
        parts.append("目标：" + "；".join(summary["goals"]))
    if summary.get("constraints"):
        parts.append("约束：" + "；".join(summary["constraints"]))
    findings = summary.get("key_findings") or []
    if findings:
        parts.append(
            "关键结论："
            + "；".join(
                str(finding.get("conclusion", ""))[:120] for finding in findings[:10]
            )
        )
    if summary.get("data_refs"):
        parts.append("数据引用：" + "；".join(summary["data_refs"][:5]))
    if summary.get("pending"):
        parts.append("待办：" + "；".join(summary["pending"]))
    if summary.get("last_focus"):
        parts.append("最近关注：" + str(summary["last_focus"]))
    return "\n".join(parts) or "（无）"


def is_recall_question(question: str) -> bool:
    """判断问题是否在回忆/引用历史轮次。"""
    return any(keyword in question for keyword in RECALL_KEYWORDS)


def clean_summary(summary: str) -> str:
    """去掉摘要的章节标记并压缩空白，得到可读结论。"""
    text = summary.replace("【总体概况】", "").replace("【趋势分析】", "").replace("【结论建议】", "")
    return re.sub(r"\s+", " ", text).strip()


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


def score_turn(turn: dict[str, Any], question: str) -> int:
    """按 CJK 二元组重叠给历史轮次与当前问题打分（轻量相关性检索）。"""
    question_grams = _bigrams(question)
    if not question_grams:
        return 0
    body = _bigrams(
        str(turn.get("question", ""))
        + " "
        + clean_summary(turn.get("answer_summary") or turn.get("summary") or "")
    )
    return len(question_grams & body)


def build_turn_view(
    turns: list[dict[str, Any]],
    question: str,
    top_k: int = 2,
    max_chars: int = 1600,
) -> str:
    """相关性检索 + 可读轮次视图：相关轮次优先，最新轮次兜底。"""
    if not turns:
        return "（无历史对话）"
    scored = sorted(turns, key=lambda turn: score_turn(turn, question), reverse=True)
    selected = scored[:top_k]
    latest = turns[-1]
    if latest not in selected and len(turns) > 1:
        selected.append(latest)
    selected.sort(key=lambda turn: int(turn.get("turn", 0)))
    lines = []
    for turn in selected:
        key_numbers = turn.get("key_numbers") or {}
        numbers_text = "；".join(f"{k}={v}" for k, v in key_numbers.items())
        conclusion = clean_summary(turn.get("answer_summary") or turn.get("summary") or "")[:200]
        trace = str(turn.get("run_id", ""))[-8:]
        lines.append(
            f"第{turn.get('turn')}轮（来源 run_{trace}）：问题：{turn.get('question', '')} "
            f"→ 结论：{conclusion}"
            + (f"；关键数字：{numbers_text}" if numbers_text else "")
        )
    text = "\n".join(lines)
    return text[:max_chars]


def extract_key_numbers(results: dict[str, Any]) -> dict[str, float]:
    """从执行结果中提取关键指标（aggregate），用于冲突检测与记忆溯源。"""
    extracted: dict[str, float] = {}
    for result in results.values():
        aggregate = (result.get("summary") or {}).get("aggregate") or {}
        if not isinstance(aggregate, dict):
            aggregate = {}  # LLM 可能把 aggregate 输出成数组，防御处理
        for key, value in aggregate.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                extracted[str(key)] = float(value)
    return extracted


def detect_conflicts(
    previous_turns: list[dict[str, Any]], key_numbers: dict[str, float]
) -> list[str]:
    """同一指标名在不同轮次数值不一致 → 返回冲突警告（记忆溯源 + 口径提醒）。"""
    warnings: list[str] = []
    seen: dict[str, float] = {}
    for turn in previous_turns:
        for metric, value in (turn.get("key_numbers") or {}).items():
            if metric in seen and abs(seen[metric] - value) > max(1e-6, 1e-4 * abs(value)):
                warnings.append(
                    f"指标'{metric}'在轮次间数值不一致（{seen[metric]} vs {value}），口径可能不同，请核实"
                )
            seen.setdefault(metric, value)
    for metric, value in key_numbers.items():
        if metric in seen and abs(seen[metric] - value) > max(1e-6, 1e-4 * abs(value)):
            warnings.append(
                f"指标'{metric}'与历史轮次数值不一致（{seen[metric]} vs {value}），口径可能不同，请核实"
            )
    return warnings
