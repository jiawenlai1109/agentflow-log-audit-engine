"""jobs.idempotency_key + 唯一索引（P5-1）

为什么单独一条迁移：受理层拆分（P5）之后，"客户端重试"是设计的一部分而不是异常，
而重试最坏后果是同一次提问变成两个作业、双份上游调用。这把锁必须落在**库层**——
在应用里"先查再插"不算：两个并发重试会同时读到"没有"，然后各插一行。
名字与运行时 SCHEMA / 模型里那份一字不差（`uq_jobs_user_idem`）：两条 schema 路径上
出现两个同义索引，比没有索引更难查。

键为空的所有行不受这条约束影响——SQLite 与 Postgres 的唯一索引都把 NULL 视为互不相同，
所以"没带幂等键"不会被当成"带了同一个键"。

判据见 tests/test_database_layer.py::test_migration_and_models_agree_column_by_column
与 tests/test_idempotency.py。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b41d2f9ac7e5"
down_revision = "c7f2a1d04b21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.create_index("uq_jobs_user_idem", "jobs", ["user_id", "idempotency_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_jobs_user_idem", table_name="jobs")
    op.drop_column("jobs", "idempotency_key")
