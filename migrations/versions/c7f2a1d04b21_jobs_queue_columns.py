"""jobs 加上队列两列：spec 与 attempts

Revision ID: c7f2a1d04b21
Revises: 9adfa33b3dfe
Create Date: 2026-10-07 23:55:00.000000

为什么要单独一条：P2 把"队列的真相"搬进库之后，`jobs` 多了两列——`spec`（这个 job 到底要
跑什么，存的是 bundle:<id> / dataset:<id> 这种**引用**，不是绝对路径）与 `attempts`
（被认领过几次，崩溃恢复靠它封顶）。运行时迁移 `app/db.py:_ADDED_COLUMNS` 有它们，
alembic baseline 没有 ⇒ "用 init_db 建的库"和"用 alembic upgrade head 建的库"是两座
不同的库，而当时每一条用例都是绿的（表名对得上就够了）。
判据见 tests/test_database_layer.py::test_migration_and_models_agree_column_by_column。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7f2a1d04b21"
down_revision = "9adfa33b3dfe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("spec", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("jobs", "attempts")
    op.drop_column("jobs", "spec")
