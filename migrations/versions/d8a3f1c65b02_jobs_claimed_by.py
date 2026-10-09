"""jobs.claimed_by —— 执行归因的留痕列（P5-3）

`worker` 是租约协议的"当前持有者"，作业跑完就清空；所以"这个 job 是在哪个进程里跑的"
在作业结束后查不到。分进程形态（受理层 `WEB_DISPATCH=off` + 独立 `scripts/worker.py`）
要的正是这一格：压测归因原本只能趁作业在跑的那几秒采样，采样漏了就得一份空表——
实测把单进程那一轮也报成"受理进程认领 0 个 job"，而那是假的。

这一列**不进任何接口响应**：里面是本机 `主机名:进程号:随机尾`，属内部标识
（与撤掉 `report_path`、把别人的 `user_id` 换成 `is_mine` 同一条口径），只给运维在库里查。

判据见 tests/test_database_layer.py（两条 schema 路径都要有这一列）与
tests/test_deployment_form.py::test_the_claimer_survives_the_finished_job。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8a3f1c65b02"
down_revision = "b41d2f9ac7e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("claimed_by", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "claimed_by")
