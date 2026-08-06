"""Planner → TaskList（结构化任务清单，含 depends_on）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ChartType = Literal["none", "line", "bar", "pie", "hist"]


class Task(BaseModel):
    task_id: int = Field(ge=1)
    description: str
    required_columns: list[str]
    code_hint: str = ""
    chart_type: ChartType = "none"
    depends_on: list[int] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def _deps_must_be_smaller(cls, value: list[int], info) -> list[int]:
        task_id = info.data.get("task_id")
        if task_id is not None and any(dep >= task_id for dep in value):
            raise ValueError("depends_on 只能引用更小的 task_id")
        return value


class TaskList(BaseModel):
    question: str
    time_base: dict | None = None
    tasks: list[Task] = Field(min_length=1, max_length=5)

    @field_validator("tasks")
    @classmethod
    def _ids_sequential(cls, value: list[Task]) -> list[Task]:
        ids = [task.task_id for task in value]
        if len(ids) != len(set(ids)):
            raise ValueError("task_id 必须唯一")
        if ids != sorted(ids) or ids != list(range(1, len(ids) + 1)):
            raise ValueError("task_id 必须从 1 递增且连续")
        return value
