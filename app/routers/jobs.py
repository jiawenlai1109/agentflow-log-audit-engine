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

from app.config import OUTPUTS_ROOT
from app import eventlog
from app.db import execute, query_one
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
from agentflow.pipeline import as_bundle, run_analysis

router = APIRouter(prefix="/api", tags=["jobs"])
manager = JobManager(sink=eventlog.append_event)
# worker 数以前写死 2：P0 实测 100 个 job 进来峰值有 93 个非终态（约 91 个在排队）。
# 分析的时间绝大多数花在等 LLM 上游，2 个线程是自己掐自己的吞吐。现在默认 8、
# 由 `WORKER_CONCURRENCY` 说话；真正的上游并发（8 × 每 run 内 3 路 = 24 路）由 P4 的
# 全局闸门管——这里不把"线程多"伪装成"上游扛得住"。

PHASE_PROGRESS = {"explore": 10, "plan": 20, "execute": 60, "report": 85, "review": 95}
_JOB_FIELDS = "job_id, user_id, status, progress, run_id, error, question, pack"


def _owned_job(job_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """归属过滤写进 SQL 本身，不靠调用方先查再比——将来谁删了比对，这条语句仍然拦得住。

    不区分"不存在"与"别人的"，一律 404，避免 job_id 枚举。
    """
    job = query_one(
        f"SELECT {_JOB_FIELDS} FROM jobs WHERE job_id = ? AND user_id = ?",
        (job_id, user["id"]),
    )
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    job["queue"] = manager.depth()
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


def preflight_pack_data(pack_obj: Any, sources: Any) -> None:
    """包与数据对不对得上，派发前就说清缺哪几列、去哪看约定。

    预检看的是**归一化之后**的列，不是 `datasets.columns` 里存的原始表头：归一化会派生出
    `time` 这类规范列，用原始表头比就会误拒——`login_audit` 配 `login_auth.csv` 报了
    "缺 time 列"，而引擎实际跑得通。这条是测试跑出来的真错。
    所以这里直接复用运行时要走的 `as_bundle`：同一份归一化、同一个判据，
    两处不可能算出两个"缺列"结论。
    """
    if pack_obj is None:
        return
    if not hasattr(sources, "tables"):
        # 单文件也归一化成 Bundle 再比列：与运行时要走的 `as_bundle` 同一条路径、同一个指纹，
        # 所以这里建的缓存就是待会儿那次运行要用的那份，不多写一份数据
        sources = as_bundle(sources, OUTPUTS_ROOT)
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
    user_id: int,
    pack: str | None = None,
    mcp_approvals: dict[str, bool] | None = None,
    run_origin: dict[str, Any] | None = None,
) -> str:
    """sources 可以是文件路径，也可以是 Bundle——pipeline 里 `as_bundle` 会归一化。

    `pack` 落进 jobs 表：一次跑的是哪个领域场景，属于"这次结果是怎么来的"的一部分。
    只记问题文本的话，历史页上"登录审计"和"多源分诊"长得一模一样，出了分歧无从回溯。
    """
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    execute(
        "INSERT INTO jobs (job_id, user_id, question, mode, session_id, pack, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
        (job_id, user_id, question, mode, session_id, pack),
    )

    def worker() -> None:
        def on_event(event: dict[str, Any]) -> None:
            manager.publish(job_id, event)
            if event.get("type") == "phase":
                progress = PHASE_PROGRESS.get(event.get("phase"), 50)
                execute(
                    "UPDATE jobs SET status='running', progress=? WHERE job_id=? AND user_id=?",
                    (progress, job_id, user_id),
                )
            if event.get("type") == "done":
                execute(
                    "UPDATE jobs SET status=?, run_id=?, finished_at=?, progress=100 "
                    "WHERE job_id=? AND user_id=?",
                    (
                        event.get("status"),
                        event.get("run_id"),
                        datetime.now().isoformat(),
                        job_id,
                        user_id,
                    ),
                )

        try:
            result = run_analysis(
                question=question,
                sources=sources,
                mode=mode,
                outputs_root=OUTPUTS_ROOT,
                session_id=session_id,
                on_event=on_event,
                pack=pack,
                mcp_approvals=mcp_approvals,
                run_origin=run_origin,
            )
            if result.get("status") != "success":
                execute(
                    "UPDATE jobs SET status=?, run_id=?, finished_at=?, progress=100 "
                    "WHERE job_id=? AND user_id=?",
                    (
                        result.get("status"),
                        result.get("run_id"),
                        datetime.now().isoformat(),
                        job_id,
                        user_id,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? "
                "WHERE job_id=? AND user_id=?",
                (str(exc)[:500], datetime.now().isoformat(), job_id, user_id),
            )
            manager.publish(job_id, {"type": "error", "error": str(exc)[:500]})

    manager.submit(job_id, worker)
    return job_id


@router.post("/analyze", response_model=JobOut)
def analyze(payload: AnalyzeRequest, user: dict = Depends(get_current_user)) -> dict:
    # 顺序是有意的：最便宜的校验先做（读目录、读一份小配置），资源解析放后面。
    # 把"包名写错"排在解析 Bundle 之后，等于让一个必然失败的请求先去读盘。
    pack_obj = validate_pack(payload)
    approvals = validate_approvals(payload)
    dataset_columns: list[str] | None = None
    dataset_columns: list[str] | None = None
    if payload.bundle_id:
        # 归属、状态、目录包含、快照可读——四步都在 bundles 模块里做一次（同一个 BUNDLES_DIR）
        sources = load_bundle_for_analysis(payload.bundle_id, user)
    else:
        dataset = query_one(
            "SELECT * FROM datasets WHERE id = ? AND user_id = ?",
            (payload.dataset_id, user["id"]),
        )
        if not dataset:
            raise HTTPException(status_code=404, detail="数据集不存在")
        sources = dataset["path"]
    preflight_pack_data(pack_obj, sources)
    if payload.session_id and not query_one(
        "SELECT * FROM sessions WHERE session_id = ? AND user_id = ?",
        (payload.session_id, user["id"]),
    ):
        raise HTTPException(status_code=404, detail="会话不存在")
    job_id = submit_analysis(
        payload.question,
        sources,
        payload.mode,
        payload.session_id,
        user["id"],
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
    )
    return _owned_job(job_id, user)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _owned_job(job_id, user)


TERMINAL_STATUSES = {"success", "partial", "degraded", "failed", "error", "cancelled"}
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
    _owned_job(job_id, user)
    start = _last_event_id(request)

    async def event_stream():
        # 第一帧报队列深度：用户在"点了没反应"与"排在第几"之间看到的必须是后者
        yield _sse({"type": "queue", **manager.depth()})
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
                "SELECT status FROM jobs WHERE job_id = ? AND user_id = ?",
                (job_id, user["id"]),
            )
            state = (row or {}).get("status")
            if state in TERMINAL_STATUSES:
                yield _sse({"type": "job_status", "status": state})
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
