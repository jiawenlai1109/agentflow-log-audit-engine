"""Visualizer → FigureResult。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class FigureResult(BaseModel):
    task_id: int
    chart_type: Literal["none", "line", "bar", "pie", "hist"] = "none"
    title: str = ""
    file_path: str | None = None
    note: str = ""
