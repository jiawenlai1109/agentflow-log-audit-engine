"""后端请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class AccountCreateRequest(BaseModel):
    """建号：用户名、口令，以及（可选）当场加入哪家企业。

    `extra="forbid"`：拼错的键（`orgn`、`pasword`）静默通过就等于"管理员以为把人建进企业了，
    实际那人是未归属"，而这种账号最坏的表现是**默认拒绝**——什么都看不见。
    """

    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    org: str | None = None  # 企业 slug 或数字 id；留空 = 未归属（只看得到自己的资源）


class AccountOut(BaseModel):
    id: int
    username: str
    org_id: int
    role: str


class OrgOut(BaseModel):
    id: int
    slug: str
    name: str


class MemberOut(BaseModel):
    """成员名单只回"协作需要知道的东西"：谁、在哪家企业、企业内角色、是不是我自己。"""

    user_id: int
    username: str
    org_id: int
    org_role: str
    is_me: bool


class AnalyzeRequest(BaseModel):
    """分析请求：数据源二选一，可选场景包与外部工具批准。

    `dataset_id` = 历史单文件路径；`bundle_id` = M2 的多文件快照。两条都缺或都给都拒——
    让调用方猜"哪个优先"是把歧义留在系统里。

    `extra="forbid"` 是有牙齿的一条：默认行为是**静默忽略未知键**，于是拼错的
    `pack`（写成 `packk`）会表现成"跑了一次普通分析"，而调用方以为自己已经在跑分诊。
    多字段请求模型不该有这种失败方式。
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    dataset_id: int | None = None
    bundle_id: str | None = None
    mode: str = Field(default="mock", pattern="^(mock|real)$")
    session_id: str | None = None
    pack: str | None = None
    mcp_approvals: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "AnalyzeRequest":
        if bool(self.dataset_id) == bool(self.bundle_id):
            raise ValueError("dataset_id 与 bundle_id 必须且只能提供一个")
        if self.pack and self.session_id:
            # 会话表只绑一个单文件 dataset_path，接不上多源 Bundle；
            # 让带包的续轮静默丢掉包 = 第二轮悄悄变成普通分析，那比拒掉危险得多。
            raise ValueError(
                "场景包运行暂不支持会话续轮：会话只绑定单文件数据集（sessions.dataset_path），"
                "请先不带 session_id 跑包分析"
            )
        return self


class SessionCreateRequest(BaseModel):
    title: str = "新会话"
    dataset_id: int | None = None


class MessageCreateRequest(BaseModel):
    """会话续轮请求。

    mode 必须与 AnalyzeRequest 一样收紧成白名单：pipeline 用
    `MockLLM() if mode == "mock" else OpenAILLM(...)` 选客户端，
    任何未校验的字符串都会落到真实（计费）分支上。
    """

    question: str = Field(min_length=1)
    mode: str = Field(default="mock", pattern="^(mock|real)$")


class JobOut(BaseModel):
    job_id: str
    status: str
    progress: int
    run_id: str | None = None
    error: str | None = None
    question: str
    # 跑的是哪个领域场景。历史页与审计要能一眼分开"普通分析"与"分诊"——
    # 只看问题文本分不出来，而分歧恰恰发生在"同一个问题、不同场景口径"的时候
    pack: str | None = None
    # 队列深度（workers/running/queued/stale_pending）：前端要能显示"排在第几位"，而不是让用户在
    # "点了没反应"与"卡死了"之间猜。100 并发下这条从体验问题变成必要的运维信息。
    # `stale_pending` 单列：那是"没人能认领也没人负责"的行，混进 queued 就是给运维一个不动的数。
    queue: dict[str, int] | None = None


class DatasetOut(BaseModel):
    id: int
    filename: str
    size: int
    row_count: int
    columns: list[str]


class SessionOut(BaseModel):
    """会话对客户端只暴露标识与标题；dataset_path 是服务器绝对路径，不外泄。"""

    session_id: str
    title: str | None
    turn_count: int


class MessageOut(BaseModel):
    turn: int
    question: str
    run_id: str | None
    answer_summary: str
    key_numbers: dict[str, float]


class EvaluationSummary(BaseModel):
    total: int
    status_count: dict[str, int]
    degraded_reasons: dict[str, int]
    chart: dict[str, int]
    critic: dict[str, int]
    avg_llm_calls: float
    avg_duration: float
    runs: list[dict[str, Any]] = []
