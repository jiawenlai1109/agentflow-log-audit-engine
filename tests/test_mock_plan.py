"""mock Planner 语义增强测试：利润指标、Top-N、日期列排除。"""

from agentflow.agents.planner import PlannerAgent
from agentflow.core.llm import MockLLM

SCHEMA = {
    "suggested_date_column": "订单日期",
    "columns": [
        {"name": "订单日期", "dtype": "object", "is_date": True},
        {"name": "产品类别", "dtype": "object", "is_date": False},
        {"name": "销售额", "dtype": "float64", "is_date": False},
        {"name": "利润", "dtype": "float64", "is_date": False},
    ]
}


def _planner() -> PlannerAgent:
    return PlannerAgent(llm=MockLLM())


def test_mock_plan_profit_top3():
    plan = _planner()._mock_plan("近七天哪个产品的销售利润最高？把前三名的列出来", SCHEMA)
    task2 = plan["tasks"][1]
    assert "利润" in task2["required_columns"]
    assert "产品类别" in task2["required_columns"]
    assert "订单日期" not in task2["required_columns"]
    assert "前三" in task2["description"]


def test_mock_plan_recent_trend():
    plan = _planner()._mock_plan("近七天销售额走势如何？", SCHEMA)
    task2 = plan["tasks"][1]
    assert task2["chart_type"] == "line"
    assert "订单日期" in task2["required_columns"]
