"""分析任务：提交、状态查询、SSE 进度流（全部按用户归属收口）。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import access, config, eventlog, llm_gate, queueing, quota
from app.runner import Dispatcher
from app.db import query_one
from app.deps import get_current_user
from app.jobs import JobManager
from app.routers.bundles import load_bundle_for_analysis
from app.schemas import AnalyzeRequest, JobOut
from agentflow.core.mcp import load_mcp_config, partition_approvals
from agentflow.core.pack import (
    available_columns,
    load_pack,
    missing_required,
    pack_names,
)
from agentflow.pipeline import as_bundle

router = APIRouter(prefix="/api", tags=["jobs"])
manager = JobManager(sink=eventlog.append_event)
# 建对象与启动分开：import 时起线程会让测试与 `--help` 都偷偷开跑线程。
# 启动点在 app/main.py 的 lifespan——进程活着才当认领者，退出时收。
dispatcher = Dispatcher(manager)
# worker 数的判据住在 `queueing.default_worker_concurrency()`：默认仍是 2，因为 P0 实测
# 在**同一个进程里**把它调到 8 会把登录 p95 从 8977.9ms 推到 14378.3ms——受理层和执行层
# 抢同一份 CPU。分成独立进程（scripts/worker.py）之后这个数才是"每个 worker 各自的数"，
# 那时往上加才不伤登录。真正的上游并发由 P4 的全局闸门管，这里不把"线程多"伪装成"上游扛得住"。

# 阶段→进度的映射住在 app/runner.py（执行那侧），路由不再自己算一份
_JOB_FIELDS = "job_id, user_id, status, progress, run_id, error, question, pack"


def _visible_job(job_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """归属过滤写进 SQL 本身，不靠调用方先查再比——将来谁删了比对，这条语句仍然拦得住。

    不区分"不存在"与"别人的"，一律 404，避免 job_id 枚举。
    判据在 `app/access.py`：作业与报告是企业内的共享资产，同企业成员看得见彼此的进度；
    `org_id = 0`（未归属）的行只对造它的人可见。
    """
    sql, params = access.scope(user)
    job = query_one(
        f"SELECT {_JOB_FIELDS} FROM jobs WHERE job_id = ?{sql}",
        (job_id, *params),
    )
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    job["queue"] = queueing.stats()
    # 上游闸门的读数与队列深度**分两个字段**：queue 是库里的全局事实，gate 是本进程的累计量。
    # 合成一个字典就会让人以为 inflight/limit 也是全局数——那是"没测过"说成"测过了"的变种。
    job["llm_gate"] = llm_gate.snapshot()
    return job


def _available_pack_names() -> list[str]:
    """可运行的场景包名。名单只在引擎里算一次（`pack_names()`），Web 层不另立一份。"""
    return sorted(pack_names())


def validate_pack(payload: AnalyzeRequest) -> Any:
    """包名校验放在派发之前。

    进了后台线程才失败，用户拿到的是一个 `failed` 的 job；422 与 failed job 的差别
    就是"当场知道该改什么"与"去历史页猜"。
    """
    if payload.pack is None:
        return None
    # 空串不算"没选包"：调用方写了 `pack: ""` 是想跑包的，静默降级成普通分析
    # 就是"未知键被忽略"的同一类事，只是换了个入口
    if not payload.pack.strip() or payload.pack not in set(_available_pack_names()):
        raise HTTPException(
            status_code=422,
            detail=f"场景包 {payload.pack} 不存在或装不起来（可用的有：{_available_pack_names()}）",
        )
    try:
        return load_pack(payload.pack)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def validate_approvals(payload: AnalyzeRequest) -> dict[str, bool]:
    """请求侧批准：只有 config 的 `grantable_approvals` 点过名的工具才算数。

    这里提前拒是给调用方看的——"你批的这个工具运维没放行"要当场知道。真正的过滤在引擎闸门
    （`core/mcp.py: partition_approvals`），两道共用同一个函数：将来多加一条调用通道，
    少一道拦就是安全事故，而"两道各写一遍"迟早算出两个结论。
    """
    if not payload.mcp_approvals:
        return {}
    try:
        config = load_mcp_config()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"外部工具配置有问题：{error}") from error
    if config is None:
        raise HTTPException(
            status_code=422,
            detail="本次没接任何外部（MCP）server，请求里的 mcp_approvals 无处生效",
        )
    _accepted, rejected = partition_approvals(config, payload.mcp_approvals)
    if rejected:
        raise HTTPException(
            status_code=422,
            detail=(
                f"这些外部工具不在可批准名单里：{sorted(rejected)}。要放行需运维在 config/mcp.yaml"
                f" 的 grantable_approvals 里点名（当前名单：{sorted(config.grantable)}）"
            ),
        )
    return dict(payload.mcp_approvals)


def preflight_pack_data(pack_obj: Any, sources: Any, org_id: int) -> None:
    """包与数据对不对得上，派发前就说清缺哪几列、去哪看约定。

    预检看的是**归一化之后**的列，不是 `datasets.columns` 里存的原始表头：归一化会派生出
    `time` 这类规范列，用原始表头比就会误拒——`login_audit` 配 `login_auth.csv` 报了
    "缺 time 列"，而引擎实际跑得通。这条是测试跑出来的真错。
    所以这里直接复用运行时要走的 `as_bundle`：同一份归一化、同一个判据，
    两处不可能算出两个"缺列"结论。

    `org_id` 决定这份归一化缓存落在哪棵树上。必须是**待会儿那次运行用的同一棵**：
    预检在 `outputs/bundles/` 建一份、运行在 `outputs/org/<id>/bundles/` 再建一份的话，
    "同一份快照"这句就变成假的（同一批数据被解析两遍，磁盘两份，而报告里的数字各自指回
    一份 — 那正是 #23 加锁与原子 rename 想避免的形状）。
    """
    if pack_obj is None:
        return
    if not hasattr(sources, "tables"):
        # 单文件也归一化成 Bundle 再比列：与运行时要走的 `as_bundle` 同一条路径、同一个指纹，
        # 所以这里建的缓存就是待会儿那次运行要用的那份，不多写一份数据
        sources = as_bundle(sources, config.org_outputs_root(org_id))
    missing = missing_required(pack_obj, available_columns(pack_obj, sources))
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"数据缺少场景包 {pack_obj.name} 必需列：{'、'.join(missing)}"
                f"（需要：{pack_obj.required_columns}，见 packs/{pack_obj.name}/data_convention.md）"
            ),
        )


def submit_analysis(
    question: str,
    sources: Any,
    mode: str,
    session_id: str | None,
    user: dict[str, Any],
    *,
    org_id: int,
    pack: str | None = None,
    mcp_approvals: dict[str, bool] | None = None,
    run_origin: dict[str, Any] | None = None,
    # 数据源的**引用**（bundle:<id> / dataset:<id）。绝对路径不进 jobs 表：
    # 那既是可外泄的位置信息（#15），也会在换机/换进程时变成一句跑不通的谎。
    source_ref: str = "",
) -> str:
    """sources 可以是文件路径，也可以是 Bundle——pipeline 里 `as_bundle` 会归一化。

    `pack` 落进 jobs 表：一次跑的是哪个领域场景，属于"这次结果是怎么来的"的一部分。
    只记问题文本的话，历史页上"登录审计"和"多源分诊"长得一模一样，出了分歧无从回溯。

    `org_id` 是**必填的关键字参数**，而且由调用方算（`access.primary_org(user)`）：
    这一个数同时决定"这条作业同企业谁能看见"与"它的产物落在哪棵树下"，两者必须是同一个
    答案。做成可选参数（内部自己算）的话，将来加一个入口就有人忘了传，表现不是报错而是
    全都落到 `org/0`（未归属 ⇒ 共享读默认拒绝 ⇒ 同事永远看不见彼此跑过什么）。
    """
    job_id = f"job_{uuid.uuid4().hex[:12]}"

    # 提交 = 入队，而且是一行写完就已是可认领状态（参数以**引用**形式进 spec：
    # bundle:<id> / dataset:<id>，绝对路径不进 jobs 表——那既可外泄位置（#15），
    # 也会在换机/换进程时变成一句跑不通的谎）。
    # 执行体不再是闭包：只有这样才能被另一个进程认领（P0 读数：杀进程后 28 个 job
    # 永远停在非终态，因为"要跑什么"只活在提交它的那个进程的内存里）。
    spec = {
        "question": question,
        "mode": mode,
        "session_id": session_id,
        "pack": pack,
        "mcp_approvals": mcp_approvals or {},
        "run_origin": run_origin or {},
        "source_ref": source_ref,
    }
    queueing.accept(
        job_id=job_id,
        user_id=user["id"],
        # 企业归属在受理这一刻盖（由调用方算好传进来）：作业行没归属（org_id=0）的话，
        # 共享读对它默认拒绝，同企业的人就永远看不见彼此跑过什么——那等于接了线但没人能共享。
        # 同一列还决定这次运行的产物落在哪棵树下，所以这里不给默认值。
        org_id=org_id,
        question=question,
        mode=mode,
        session_id=session_id,
        pack=pack,
        spec=spec,
    )
    # Web 进程自己也是认领者（默认形态）。独立 worker 进程起来后这只是多一个消费者，
    # 不是第二条执行路径——认领是原子的，一个 job 只会被一个认领者拿到。
    dispatcher.start()
    return job_id


@router.post("/analyze", response_model=JobOut)
def analyze(payload: AnalyzeRequest, user: dict = Depends(get_current_user)) -> dict:
    # 顺序是有意的：最便宜的校验先做（读目录、读一份小配置），资源解析放后面。
    # 把"包名写错"排在解析 Bundle 之后，等于让一个必然失败的请求先去读盘。
    pack_obj = validate_pack(payload)
    approvals = validate_approvals(payload)
    # 一次请求只算一遍企业归属：预检建缓存的那棵树、与运行要落的那棵树必须是同一棵，
    # 而"谁属于哪家企业"这件事的权威在 access（取最小的那条成员关系，没成员关系 = 0）。
    org_id = access.primary_org(user)
    # 配额判在**受理这一刻**，判据住在 `app/quota.py`：worker 认领之后已经没有"拒绝"这个出口，
    # 那时才发现超配额只能把作业跑成 failed——"配额"就变成"失败计数"，而 P4 要的恰恰是失败计数不涨。
    # 放在解析数据源之前：一条必然被拒的请求不该先去读盘（与上面"最便宜的校验先做"同一个顺序）。
    decision = quota.decide(org_id)
    if not decision["allowed"]:
        quota.note_refusal(decision, org_id, user)
        raise HTTPException(
            status_code=429,
            detail=decision["reason"],
            headers={"Retry-After": str(decision["retry_after"])},
        )
    if payload.bundle_id:
        # 归属、状态、目录包含、快照可读——四步都在 bundles 模块里做一次（同一个 BUNDLES_DIR）
        sources = load_bundle_for_analysis(payload.bundle_id, user)
        source_ref = f"bundle:{payload.bundle_id}"
    else:
        # 数据集可以是同企业别人的：判据走 access，与 runner 读取时那条是同一条
        dataset = access.dataset_row(user, payload.dataset_id)
        if not dataset:
            raise HTTPException(status_code=404, detail="数据集不存在")
        sources = dataset["path"]
        source_ref = f"dataset:{payload.dataset_id}"
    preflight_pack_data(pack_obj, sources, org_id)
    # 会话是**个人**上下文：同企业也不共享，所以这里用 OWNER
    sql, params = access.scope(user, access.OWNER)
    if payload.session_id and not query_one(
        f"SELECT * FROM sessions WHERE session_id = ?{sql}", (payload.session_id, *params)
    ):
        raise HTTPException(status_code=404, detail="会话不存在")
    job_id = submit_analysis(
        payload.question,
        sources,
        payload.mode,
        payload.session_id,
        user,
        org_id=org_id,
        pack=payload.pack,
        mcp_approvals=approvals,
        # 谁发起的、从哪发起的、请求批了什么：批准链有了第二个入口，
        # 经手人就必须查得回来，否则"这次写能力是谁批的"没人能答
        run_origin={
            "source": "web",
            "actor_user_id": user["id"],
            "actor_username": user.get("username"),
            "pack": payload.pack,
            "approvals_requested": dict(payload.mcp_approvals),
        },
        source_ref=source_ref,
    )
    return _visible_job(job_id, user)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _visible_job(job_id, user)


TERMINAL_STATUSES = set(queueing.TERMINAL)  # 名单只有一份（queueing.TERMINAL），这里不另立
# 终态判定要的是库里的状态，不是"我这个进程知不知道"——worker 拆出去之后，写状态的人
# 与推流的人不是同一个进程。


def _sse(event: dict, seq: int | None = None) -> str:
    """一帧 SSE。集中成一个函数是因为 `id:` 这一行的有无就是续读协议本身。"""
    body = "id: " + str(seq) + "\n" if seq is not None else ""
    return body + "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _last_event_id(request: Request) -> int:
    """浏览器重连会自动带回 `Last-Event-ID`。解析不了退回 0（从头读），不猜。"""
    raw = (request.headers.get("last-event-id") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) >= 0 else 0


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, user: dict = Depends(get_current_user)) -> StreamingResponse:
    _visible_job(job_id, user)
    start = _last_event_id(request)

    async def event_stream():
        # 判据取在**用的地方**而不是外层函数的闭包里：这条流活多久，判据就要在它自己那次
        # 查询里说清楚——外层变量被重构掉时，读代码的人不会拿到一条"看不见但查得到"的语句。
        sql, params = access.scope(user)
        # 第一帧报队列深度：用户在"点了没反应"与"排在第几"之间看到的必须是后者。
        # 数从库里读（queueing.stats）而不是读本进程簿记——job 可能被另一个进程的 worker 认领。
        # 首帧同时给两份深度：库里的队列深度（全局）与本进程的闸门读数（局部）。
        # 只给前者，"上游 16 路全在飞、还有 9 个作业在等槽位"这件事在读数上就是隐形的，
        # 用户看到的仍是"点了没反应"——那正是 P4 要消掉的那格。
        yield _sse({"type": "queue", **queueing.stats(), "gate": llm_gate.snapshot()})
        cursor = start
        while True:
            rows = eventlog.read_after(job_id, cursor)
            if not rows and not eventlog.has_events(job_id):
                # 事件没落库（sink 写失败过）⇒ 退化成过程内缓冲，与改造前等价。
                # 降级可以，静默不行：明着发一条 events_not_persisted。
                yield _sse({"type": "events_not_persisted", "job_id": job_id})
                events, _total, expired = manager.snapshot(job_id, 0)
                for event in events:
                    yield _sse(event)
                if expired or manager.is_done(job_id):
                    break
                await asyncio.sleep(0.5)
                continue
            for seq, event in rows:
                cursor = seq
                yield _sse(event, seq)
            row = query_one(
                f"SELECT status FROM jobs WHERE job_id = ?{sql}", (job_id, *params)
            )
            state = (row or {}).get("status")
            if state in TERMINAL_STATUSES:
                yield _sse({"type": "job_status", "status": state})
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
