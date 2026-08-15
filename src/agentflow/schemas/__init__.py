"""Agent 输入输出 pydantic 模型（对应《输出格式设计.md》）。"""

from agentflow.schemas.profile import ColumnProfile, SchemaProfile
from agentflow.schemas.plan import Task, TaskList
from agentflow.schemas.result import ErrorClass, TaskExecutionResult
from agentflow.schemas.verdict import CheckResult, Verdict
from agentflow.schemas.figure import FigureResult
from agentflow.schemas.report import FailureInfo, ReportResult
from agentflow.schemas.review import Review, ReviewIssue
from agentflow.schemas.summary import KeyFinding, SessionSummary

__all__ = [
    "ColumnProfile",
    "SchemaProfile",
    "Task",
    "TaskList",
    "ErrorClass",
    "TaskExecutionResult",
    "CheckResult",
    "Verdict",
    "FigureResult",
    "FailureInfo",
    "ReportResult",
    "Review",
    "ReviewIssue",
    "KeyFinding",
    "SessionSummary",
]
