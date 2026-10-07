"""SQLAlchemy 2.0 模型层：与今天 SQLite 库逐表对应，另加多租户与持久事件三张新表。

为什么现在建这一层（P1）：路由里 52 条裸 SQL 各自持有归属谓词与方言写法，100 并发下
一是连接生命周期没人管（每条 SQL 开一次 `sqlite3.connect`），二是换 Postgres 就要重写全部
查询。模型层落地后，Repository 只需一处拿会话，租户谓词只有一处定义（见 `app/repository/`）。

三条口径：

1. **一库两方言**：DSN 由 `DATABASE_URL` 给。开发/测试默认 `sqlite+aiosqlite:///`（CI 不需要
   起服务），生产 `postgresql+asyncpg://`。所以这里不用任何只有某一方支持的类型：
   自增主键用 `BIGINT` 在 SQLite 上退化回 `INTEGER`（SQLite 只对 INTEGER PRIMARY KEY 自动增），
   JSON 一律存 TEXT 由应用层编解码（今天也是这样，不改语义）。
2. **时间戳带时区**：旧库用 `datetime('now','localtime')`——本地时间没有时区，跨机器不可比。
   新列一律 `TIMESTAMP(timezone=True)` + 服务端 UTC；旧列先保持原样，迁移时只做加法，
   不改旧行的值（改了历史产物上的时间就成了说谎）。
3. **`user_id` 不再有 `DEFAULT 1`**：默认归 admin 是跨用户可见性事故的种子。多租户表在
   模型层就要求 `org_id` 非空——漏写就是插入失败，不是悄悄变成一个能看别人数据的人。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite 只对 INTEGER PRIMARY KEY 自动增；Postgres 用 BIGINT。这个变体让同一份模型两边都能建表
PkType = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    """声明式基类。表名沿用旧库，迁移就是"补列 + 加新表"，不是重起一套名字。"""


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())


class Organization(Base):
    """企业（租户）。"同企业成员共享数据集与报告"这句产品要求，物理上就是这一张表 + memberships。"""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    """一个用户属于哪个企业、在企业里是什么角色。

    没有这一行就没有任何可见性：授权判定问的是"这个人是不是这个企业的成员"，
    而不是"这条记录的 user_id 等不等于他"——协作共享要求后者被换掉。
    """

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "org_id", name="uq_membership_user_org"),
        Index("ix_membership_org", "org_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # admin / member
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (Index("ix_datasets_org", "org_id", "id"),)

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    # 旧列 user_id 保留（历史行要靠它回填 org），新的可见性判定一律看 org_id
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    columns: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())


class Job(Base):
    """作业行。**状态必须活在库里，不能活在进程内存里**——P2 拆 worker 的全部理由。"""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_org_status", "org_id", "status"),
        Index("ix_jobs_user", "user_id", "id"),
        Index("ix_jobs_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="mock")
    session_id: Mapped[str | None] = mapped_column(String(32))
    pack: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    # queued / running / success / partial / degraded / failed / cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    # 谁在跑它：多 worker 水平扩之后，"这个 job 归哪个 worker"必须查得回来，
    # 否则 worker 死了没人能把它认领回来（P2 的租约字段）
    worker: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime())


class JobEvent(Base):
    """事件落库：SSE 断线重连、换进程读、事后审计，都要求事件不在内存里。

    `seq` 是每 job 递增的游标，客户端用 `Last-Event-ID` 带回来 ⇒ 服务端从 seq 之后读，
    不重放（今天的行为是从头重放，读数在 `.appdata/load_before_*.json` 的 `sse` 一栏）。
    """

    __tablename__ = "job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_job_event_seq"),
        Index("ix_job_events_job", "job_id", "seq"),
    )

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(32), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_org", "org_id", "id"),)

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    dataset_path: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())


class Bundle(Base):
    __tablename__ = "bundles"
    __table_args__ = (Index("ix_bundles_org", "org_id", "id"),)

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    bundle_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    root: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    error: Mapped[str | None] = mapped_column(Text)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    table_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())


class BundleFile(Base):
    __tablename__ = "bundle_files"
    __table_args__ = (Index("ix_bundle_files_bundle", "bundle_id", "id"),)

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    bundle_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="skipped")
    table_ref: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)
    hint: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())


class BundleTable(Base):
    __tablename__ = "bundle_tables"
    __table_args__ = (Index("ix_bundle_tables_bundle", "bundle_id", "table_ref"),)

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    bundle_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    table_ref: Mapped[str] = mapped_column(String(16), nullable=False)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    columns: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    encoding: Mapped[str | None] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(), server_default=func.now())


class OrgQuota(Base):
    """每企业配额（P4）。数值存库里而不是写在配置文件里：配额是要按客户谈的东西，
    改了不该要发版，也不该在重启时丢。`limit_*` 为 None 表示不设限。"""

    __tablename__ = "org_quotas"
    __table_args__ = (UniqueConstraint("org_id", name="uq_quota_org"),)

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_concurrent_jobs: Mapped[int | None] = mapped_column(Integer)
    limit_jobs_per_day: Mapped[int | None] = mapped_column(Integer)
    limit_llm_calls_per_day: Mapped[int | None] = mapped_column(Integer)
    limit_upload_bytes: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


def jsonable(row: Any) -> dict[str, Any]:
    """行转 dict 给 Pydantic/JSON 用：DateTime 转 iso，其他原样。"""
    return {
        column.name: (value.isoformat() if isinstance(value, datetime) else value)
        for column in row.__table__.columns
        for value in (getattr(row, column.name),)
    }
