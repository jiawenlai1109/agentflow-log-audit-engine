"""Planner → TaskList（结构化任务清单，含 depends_on / upstream_refs / 约束）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ChartType = Literal["none", "line", "bar", "pie", "hist"]


class UserConstraints(BaseModel):
    """用户约束（一等公民）：Planner 抽取，Orchestrator 注入下游 Agent。"""

    time_scope: str | None = None
    display: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    custom: list[str] = Field(default_factory=list)


class Task(BaseModel):
    task_id: int = Field(ge=1)
    description: str
    required_columns: list[str]
    code_hint: str = ""
    chart_type: ChartType = "none"
    depends_on: list[int] = Field(default_factory=list)
    upstream_refs: list[str] = Field(default_factory=list)
    rule_params: dict[str, Any] | None = None  # 场景包规则任务：{"id": "R1"}（工作规划 §6.2）
    # M2-3 数据契约声明：本任务要读哪几张表（Bundle 里的 table id）与按哪些键连。
    # 空 = 单表语义（沿用主表），因此既有计划一个字节都不变。
    dataset_refs: list[str] = Field(default_factory=list)
    join_keys: list[str] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def _deps_must_be_smaller(cls, value: list[int], info) -> list[int]:
        task_id = info.data.get("task_id")
        if task_id is not None and any(dep >= task_id for dep in value):
            raise ValueError("depends_on 只能引用更小的 task_id")
        return value

    @field_validator("dataset_refs")
    @classmethod
    def _refs_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("dataset_refs 不得重复声明同一张表")
        return value

    @field_validator("join_keys")
    @classmethod
    def _keys_need_two_refs(cls, value: list[str], info) -> list[str]:
        if value and len(info.data.get("dataset_refs") or []) < 2:
            raise ValueError("join_keys 只在声明两张以上表时有意义")
        return value


class TaskList(BaseModel):
    question: str
    time_base: dict | None = None
    constraints: UserConstraints | None = None
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
