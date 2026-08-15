"""记忆工具单元测试：相关性检索 / 轮次视图 / 冲突检测 / 回忆判定。"""

from agentflow.core.memory import (
    build_turn_view,
    clean_turn,
    clean_summary,
    detect_conflicts,
    extract_key_numbers,
    is_recall_question,
    merge_summary_mock,
    render_summary_text,
)


def test_is_recall_question():
    assert is_recall_question("我之前问的第一个问题的答案是什么？")
    assert not is_recall_question("总销售额是多少？")


def test_build_turn_view_prefers_relevant_turn():
    turns = [
        {
            "turn": 1,
            "question": "总销售额是多少？",
            "summary": "总销售额为100。",
            "run_id": "run_a1",
            "key_numbers": {"总销售额": 100.0},
        },
        {
            "turn": 2,
            "question": "华东地区的销售额呢？",
            "summary": "华东380条。",
            "run_id": "run_b2",
            "key_numbers": {},
        },
        {
            "turn": 3,
            "question": "退款金额是多少？",
            "summary": "退款汇总。",
            "run_id": "run_c3",
            "key_numbers": {},
        },
    ]
    view = build_turn_view(turns, "我第一轮问的总销售额答案是什么？", top_k=2)
    assert "第1轮" in view
    assert "100" in view


def test_detect_conflicts():
    turns = [{"key_numbers": {"合计_销售额": 100.0}}]
    assert detect_conflicts(turns, {"合计_销售额": 101.0})
    assert not detect_conflicts(turns, {"合计_销售额": 100.0})


def test_extract_key_numbers():
    results = {"1": {"summary": {"aggregate": {"合计_销售额": 123.4}, "head": []}}}
    assert extract_key_numbers(results) == {"合计_销售额": 123.4}


def test_clean_summary_strips_markers():
    cleaned = clean_summary("【总体概况】总销售额为100 【结论建议】建议关注")
    assert "总销售额" in cleaned
    assert "【" not in cleaned


def test_clean_turn_strips_filler_and_keeps_columns():
    cleaned = clean_turn(
        "请问总销售额是多少呢？",
        "【总体概况】总销售额为100",
        {"总销售额": 100.0},
        ["销售额", "订单日期"],
    )
    assert "请问" not in cleaned["question_clean"]
    assert "呢" not in cleaned["question_clean"]
    assert cleaned["mentioned_columns"] == ["销售额", "订单日期"]
    assert cleaned["key_numbers"]["总销售额"] == 100.0


def test_merge_summary_mock_accumulates_findings_and_constraints():
    old = {"goals": [], "data_refs": [], "key_findings": [], "constraints": [], "pending": []}
    merged = merge_summary_mock(old, {"question": "以后只用2024年数据", "key_numbers": {"总销售额": 100.0}, "turn": 2, "run_id": "r1"})
    assert merged["constraints"] == ["以后只用2024年数据"]
    assert len(merged["key_findings"]) == 1
    assert merged["last_focus"] != ""


def test_render_summary_text_structured():
    text = render_summary_text(
        {
            "goals": ["关注华东"],
            "key_findings": [{"conclusion": "总销售额=100"}],
            "constraints": ["只看2024"],
        }
    )
    assert "华东" in text and "总销售额=100" in text and "只看2024" in text
