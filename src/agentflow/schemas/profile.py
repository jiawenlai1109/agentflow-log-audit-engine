"""Explorer → SchemaProfile（真实表结构画像）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ColumnProfile(BaseModel):
    name: str
    dtype: str
    missing_rate: float = 0.0
    unique_rate: float | None = None
    sample: list = Field(default_factory=list)
    is_date: bool = False
    min: str | None = None
    max: str | None = None


class SchemaProfile(BaseModel):
    file_path: str
    encoding: str = "utf-8"
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    columns: list[ColumnProfile]
    suggested_date_column: str | None = None
    issues: list[str] = Field(default_factory=list)
