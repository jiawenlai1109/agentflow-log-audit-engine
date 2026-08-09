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
        str(turn.get("question", "")) + " " + clean_summary(turn.get("summary", ""))
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
        conclusion = clean_summary(turn.get("summary", ""))[:200]
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
