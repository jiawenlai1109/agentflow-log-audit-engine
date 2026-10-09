"""分析任务：提交、状态查询、SSE 进度流（全部按用户归属收口）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import access, config, eventlog, llm_gate, paths, queueing, quota
from app.runner import Dispatcher, dispatch_form, web_dispatch_enabled
from app.db import query, query_one
from app.deps import get_current_user
from app.jobs import JobManager
from app.routers.bundles import load_bundle_for_analysis
from app.schemas import AnalyzeRequest, JobOut
from agentflow.core import trace
from agentflow.core.mcp import load_mcp_config, partition_approvals
from agentflow.core.pack import (
    available_columns,
    load_pack,
    missing_required,
    pack_names,
)
from agentflow.pipeline import as_bundle

router = APIRouter(prefix="/api", tags=["jobs"])
# 受理层的日志名与 quota/ratelimit 共用一个（`agentflow.accept`）：运维查"这次提交发生了什么"
# 时不该在三个 logger 名之间找。可见性由 app.main 的 ensure_operator_visible_logging 负责。
logger = logging.getLogger("agentflow.accept")
manager = JobManager(sink=eventlog.append_event)
# 建对象与启动分开：import 时起线程会让测试与 `--help` 都偷偷开跑线程。
# 启动点在 app/main.py 的 lifespan——进程活着才当认领者，退出时收。
dispatcher = Dispatcher(manager)
# worker 数的判据住在 `queueing.default_worker_concurrency()`：默认仍是 2，因为 P0 实测
# 在**同一个进程里**把它调到 8 会把登录 p95 从 8977.9ms 推到 14378.3ms——受理层和执行层
# 抢同一份 CPU。分成独立进程（scripts/worker.py）之后这个数才是"每个 worker 各自的数"，
# 那时往上加才不伤登录。真正的上游并发由 P4 的全局闸门管，这里不把"线程多"伪装成"上游扛得住"。

# 阶段→进度的映射住在 app/runner.py（执行那侧），路由不再自己算一份
_JOB_FIELDS = "job_id, user_id, status, progress, run_id, error, question, pack, trace_id"


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
    # 终态与否由服务端算（名单只有 `queueing.TERMINAL` 那一份），前端只读一个布尔。
    # 让前端自己抄一份"哪些状态算完了"的下场是两份名单分叉：多出来的那个终态会被
    # 当成"还在跑"，客户端就一直重连一条早就结束的流。
    job["terminal"] = str(job.get("status")) in TERMINAL_STATUSES
    # 执行形态与闸门读数同口径：**本进程**的局部量，单独一个字段。
    # 分进程部署下没有这一格，运维面对的就是"作业全在排队、每个接口都返回 200"，
    # 而唯一的线索（这个进程不认领作业）只写在启动日志里。
    job["dispatch"] = dispatch_form()
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


# 幂等键的形状：可见 ASCII，最长 200 字符（与列宽一致），**只在受理这一处判形状**。
_IDEMPOTENCY_SHAPE = re.compile(r"^[\x21-\x7e]{1,200}$")


def parse_idempotency_key(raw: str | None) -> str | None:
    """`Idempotency-Key` 请求头的形状判据。三种命运，各自有理由：

    - **没带这个头** → None。这次提交没有护栏，两次相同提问照样建两行（正常语义）。
    - **带了但是空的 / 形状不对** → 422，不当成"没带"。客户端写了 `Idempotency-Key: `
      或者塞了空格与控制字符，通常是它自己出 bug；静默放行等于**让它以为有护栏而实际没有**，
      那比直接报错更坏（与"一个看着能配、实际会搞丢东西的旋钮比没有更坏"同族）。
    - **形状对** → 原样用，不 strip、不归一化。键是不透明串；把两个不同的键洗成同一个，
      表现是"我明明提交了两次，第二次没有任何反应"。
    """
    if raw is None:
        return None
    if not _IDEMPOTENCY_SHAPE.match(raw):
        raise HTTPException(
            status_code=422,
            detail=(
                "Idempotency-Key 形状不对：要 1-200 个可见字符、不含空格与控制字符。"
                "带了空值或坏值不当成「没带」——那样你会以为这次提交有幂等护栏，而实际没有。"
            ),
        )
    return raw


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
    # 幂等键（P5-1）：同一个人带同一个键，只有第一次真的建出作业行。判据在库层，
    # 这里只负责把它原样递下去——路由不自己判"是不是重放"，那会有第二份答案。
    idempotency_key: str | None = None,
    # 入站的链路标识（P6-1，`X-Trace-Id`）。两个创建作业的入口都汇到这里，所以生成点只有一处。
    inbound_trace: str | None = None,
) -> dict[str, Any]:
    """sources 可以是文件路径，也可以是 Bundle——pipeline 里 `as_bundle` 会归一化。

    `pack` 落进 jobs 表：一次跑的是哪个领域场景，属于"这次结果是怎么来的"的一部分。
    只记问题文本的话，历史页上"登录审计"和"多源分诊"长得一模一样，出了分歧无从回溯。

    `org_id` 是**必填的关键字参数**，而且由调用方算（`access.primary_org(user)`）：
    这一个数同时决定"这条作业同企业谁能看见"与"它的产物落在哪棵树下"，两者必须是同一个
    答案。做成可选参数（内部自己算）的话，将来加一个入口就有人忘了传，表现不是报错而是
    全都落到 `org/0`（未归属 ⇒ 共享读默认拒绝 ⇒ 同事永远看不见彼此跑过什么）。

    返回值是 `{"job_id": …, "replayed": 是否只是重放}`，不是裸的 job_id：
    "我这次提交排上了一个活"与"我这次提交撞回了自己上一次那条"对用户是两种结果，
    界面与压测读数都要能分开这两件事。

    `trace_id`（P6-1）在这里生成、在这里落库，**两个创建作业的入口都汇到这一个函数**：
    `/api/analyze` 与会话续轮如果各写一条生成逻辑，"凭一个 id 查回整条链路"就只对一半的请求成立
    ——这和幂等键当初必须装在 `queueing.accept` 而不是装在路由里是同一个理由。
    重放时库里那条作业行没被碰过（`ON CONFLICT DO NOTHING`），所以行上留的仍是第一次的 trace；
    第二次提交自己的 trace 只出现在那一条 warning/日志里，不会冒充行上的链路。
    """
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    # 链路标识生在**受理这一跳**（P6-1），不是生在 HTTP 中间件里：一次 HTTP 请求与一次分析
    # 不是一一对应——带幂等键的重试是第二个请求、撞回同一个作业。把 trace 定在"提交"上，
    # 才有"凭一个 id 查回这次分析排了多久、谁认领、重试几次"这句话的落点。
    # 入站带了 `X-Trace-Id` 且形状对就沿用（网关已经起过一条链路时不该在受理层断掉），
    # 形状不对就另起一条并把这件事**说出来**：静默改写客户端的链路标识，等于让它以为自己那条贯穿到底。
    trace_id, reused = trace.adopt(inbound_trace)
    if inbound_trace and not reused:
        logger.warning(
            "入站 %s 形状不对（要 1-%d 位字母数字/-/_，不收空格与控制字符），本次提交另起一条链路：job_id=%s",
            trace.HEADER,
            trace.MAX_LEN,
            job_id,
        )

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
        # 执行侧读的那一份（认领之后由 app/runner 绑回引擎）。作业行上还有同一列，
        # 那份是给运维 `WHERE trace_id = ?` 反查用的——两个读者、一个值，不是两个事实源。
        "trace_id": trace_id,
    }
    accepted = queueing.accept(
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
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    # Web 进程自己也是认领者（默认形态）。独立 worker 进程起来后这只是多一个消费者，
    # 不是第二条执行路径——认领是原子的，一个 job 只会被一个认领者拿到。
    # 这一句要过形态判据：`WEB_DISPATCH=off` 时提交路径**照样**开循环的话，
    # "拆进程"就变成一个只在 lifespan 生效的开关——第一次有人点提交就把它绕过去了，
    # 而绕过去的表现不是报错，是 Web 进程偷偷开始吃 CPU（正是 P0 那笔登录 p95 的账）。
    if web_dispatch_enabled():
        dispatcher.start()
    return accepted


@router.post("/analyze", response_model=JobOut)
def analyze(
    payload: AnalyzeRequest,
    user: dict = Depends(get_current_user),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    inbound_trace: str | None = Header(None, alias=trace.HEADER),
) -> dict:
    # 顺序是有意的：最便宜的校验先做（读目录、读一份小配置），资源解析放后面。
    # 把"包名写错"排在解析 Bundle 之后，等于让一个必然失败的请求先去读盘。
    pack_obj = validate_pack(payload)
    approvals = validate_approvals(payload)
    # 幂等键的形状排在配额之前：一条形状不对的请求连"该不该拦"都还没资格问，
    # 而且它 422 之后什么都不会发生（既不占配额，也不留作业行）。
    key = parse_idempotency_key(idempotency_key)
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
    job = submit_analysis(
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
        idempotency_key=key,
        inbound_trace=inbound_trace,
    )
    visible = _visible_job(job["job_id"], user)
    # "这次真的排上了一个活"与"这次只是撞回我自己上一次那条"，客户端有权知道：
    # 压测的"重试不产生第二个作业"这条判据也读这一格，不靠数日志。
    visible["idempotency_replayed"] = job["replayed"]
    return visible


@router.get("/queue")
def queue_state(user: dict = Depends(get_current_user)) -> dict:
    """平台读数的独立入口：队列深度（库里的全局事实）+ 闸门与形态（本进程的局部量）。

    为什么单独一条：这三份数原来只挂在**作业详情**与**进度流首帧**上，于是"我还没提交任何
    作业"的时候界面无处可问——而那时恰好是分进程形态下最该看的一眼（有没有人在认领）。
    数据一条都不新算：三个函数就是作业详情与首帧用的那三份，权威还是各自那一处。

    刻意**不含入口限流的额度**。P4-3 定过"429 的响应里不写限额与已试次数"（那等于替试探者
    标定天花板），一个匿名可猜的 GET 把整份吐出去，就是从侧门把那条决定撤掉。
    """
    return {"queue": queueing.stats(), "gate": llm_gate.snapshot(), "dispatch": dispatch_form()}


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _visible_job(job_id, user)


def _seconds_between(start: Any, end: Any) -> float | None:
    """两个本地时间串之间的秒数。**解不开就返回 None，不许当 0**。

    这条口径是 P5 收尾那一轮立下的："0"有两种读法——"量到的是 0"与"没量到"。
    排队时长这一格尤其贵：把"这条作业一条事件都还没有"报成 `queue_wait_seconds: 0`，
    运维读到的是"排得真快"，而真相是"没人认领"。
    """
    if not start or not end:
        return None
    try:
        first = datetime.strptime(str(start), "%Y-%m-%d %H:%M:%S")
        second = datetime.strptime(str(end), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return round((second - first).total_seconds(), 3)


def _upstream_shape(row: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """"上游返回了什么形状"这一格：闸门等待从库里的事件算，产物侧从 `evaluation.json` 取。

    三种缺席分开写，不许都写成 0 或不写：没有 run_id（这条作业还没落过产物）、
    产物文件不在（被清理或换机）、文件在但读不开。最后一种必须报"读不开"，
    因为它意味着有别的写法在写那个文件——那是缺陷不是空值。
    """
    waits = eventlog.payloads_of_kind(str(row["job_id"]), "llm_gate_wait")
    durations = [event.get("waited_ms") for event in waits if isinstance(event.get("waited_ms"), (int, float))]
    last = waits[-1] if waits else {}
    out: dict[str, Any] = {
        "mode": row.get("mode"),  # mock 那两格描述的是本地假客户端，不是真上游——别让读数冒充
        "gate_waits": len(waits),
        "max_gate_wait_ms": max(durations) if durations else None,
        "gate_limit": last.get("limit"),
        "gate_limit_source": last.get("limit_source"),
    }
    run_id = row.get("run_id")
    if not run_id:
        out["artifact"] = {"status": "no_run_id", "reason": "这条作业还没跑出 run_id，产物侧没有可对的东西"}
        return out
    path = paths.run_dir_for_row(row, str(run_id)) / "evaluation.json"
    if not path.exists():
        out["artifact"] = {"status": "absent", "run_id": run_id, "reason": "产物目录里没有 evaluation.json"}
        return out
    try:
        evaluation = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        out["artifact"] = {
            "status": "unreadable",
            "run_id": run_id,
            "reason": f"{type(exc).__name__}：文件在但读不出来，说明有别的写法在写它",
        }
        return out
    # 只取形状与计数，不取正文：产物里那些 results/report 正文不是这个读数该搬的东西
    out["artifact"] = {
        "status": "present",
        "run_id": run_id,
        "run_status": evaluation.get("status"),
        "llm_calls": evaluation.get("llm_calls"),
        "duration_seconds": evaluation.get("duration_seconds"),
        "models_used": evaluation.get("models_used"),
        "empty_content_shapes": len(evaluation.get("llm_empty_content") or []),
        "degraded_reason": evaluation.get("degraded_reason"),
    }
    return out


def _trace_job(row: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """一条作业行 → "凭一个 id 查回"的那五格。

    作业详情那一侧**复用 `_visible_job`**，不在这里再拼一份：状态、队列深度、闸门读数、
    终态与否、执行形态都已经有唯一的一份算法，抄第二份的下场是两边迟早给两个答案
    （P5-3 那次就是为这句话加的守卫）。这里只补作业详情**没有**的那几格。
    """
    spec = queueing.get_spec(row)
    origin = spec.get("run_origin") or {}
    job_id = str(row["job_id"])
    first_event_at = eventlog.first_event_at(job_id)
    claimed_by = row.get("claimed_by")
    is_admin = user.get("role") == "admin"
    return {
        "detail": _visible_job(job_id, user),
        # 谁提交的：行上的 user_id + 受理那一刻记下的经手人。别人的 user_id 不外泄（P3 的口径），
        # 所以给一个 is_mine 让读者知道自己是不是那个"谁"。
        "submission": {
            "user_id": row.get("user_id"),
            "is_mine": int(row.get("user_id") or 0) == int(user["id"]),
            "actor_username": origin.get("actor_username"),
            "org_id": row.get("org_id"),
            "pack": row.get("pack"),
            "question": row.get("question"),
            "source_ref": spec.get("source_ref"),
        },
        # 排了多久：算法写在读数里，不让读者猜这个数是按哪两列减出来的
        "timing": {
            "created_at": row.get("created_at"),
            "first_event_at": first_event_at,
            "finished_at": row.get("finished_at"),
            "queue_wait_seconds": _seconds_between(row.get("created_at"), first_event_at),
            # 算法与**分辨率**都写进读数：这两列都是秒级（SQLite 的 `datetime('now','localtime')`
            # 不带小数），所以 `0.0` 的意思是"不到一秒"，不是"没有排队"。这一格不给分辨率，
            # 下一个读它的人就会把 0.0 抄成"排队时间为零"——P5 收尾那轮立下的"0 有两种读法"，
            # 这一次是我自己的新读数里差点犯的那一种。
            "queue_wait_basis": "jobs.created_at → 这条作业第一条 job_events.created_at（两列都是秒级，故本数是秒级量化）",
            "total_seconds": _seconds_between(row.get("created_at"), row.get("finished_at")),
        },
        # 谁认领的：`claimed_by` 是本机 主机名:进程号:尾，属内部标识（P5-3 定的口径），
        # 因此**只给 admin**；普通读者拿到的是布尔 + 一句这一格为什么是 null。
        "execution": {
            "attempts": int(row.get("attempts") or 0),
            "claimed": bool(claimed_by),
            "claimed_by": claimed_by if is_admin else None,
            "claimed_by_scope": "admin_only" if claimed_by else "none",
            "run_id": row.get("run_id"),
        },
        "upstream": _upstream_shape(row, spec),
    }


@router.get("/trace/{trace_id}")
def trace_readout(trace_id: str, user: dict = Depends(get_current_user)) -> dict:
    """P6 的退出判据：任一次线上请求，凭一个 id 查回"谁提交的、排了多久、谁认领的、
    几次重试、上游返回了什么形状"。

    三条口径是这一格能不能被信任的关键：

    1. **`trace_id` 不是权限凭据**。查询照拼 `access.scope(user)` 那条谓词（判据只有一份，
       住在 `app/access.py`）：带别人那条串查回来的是 404，与"这条链路不存在"**同一个形状**——
       区分二者就等于给了一个枚举入口。
    2. **一条链路可以挂着多条作业行**。入站那个串是客户端自带的，同一个人复用同一个 id 连发
       两次是合法形状（没带幂等键就是两行）。所以这里交回**列表**而不是"那一条"，
       把"一次提交 = 一行"这个假设藏在返回形状里。
    3. **没量到的那格不许写成 0**：排队时长在"一条事件都还没有"时是 None，
       上游产物分"没有 run_id / 文件不在 / 文件读不开"三种缺席。
    """
    if not trace.is_usable(trace_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"trace_id 形状不对：要 1-{trace.MAX_LEN} 位字母、数字、- 或 _，不收空格与控制字符。"
                "这一格是从作业行上查回来的，不是自由文本——形状摆对了，「查得着」与「查不着」才是两种确定的读数。"
            ),
        )
    sql, params = access.scope(user)
    rows = query(
        "SELECT job_id, user_id, org_id, status, mode, pack, question, run_id, attempts, claimed_by, "
        "created_at, finished_at, spec FROM jobs WHERE trace_id = ?" + sql + " ORDER BY id ASC",
        (trace_id, *params),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="没有这条链路的可见记录（不存在，或不在你能看的范围里）")
    return {"trace_id": trace_id, "count": len(rows), "jobs": [_trace_job(row, user) for row in rows]}


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
        # 首帧给三份读数：库里的队列深度（全局）、本进程的闸门量（局部）、本进程的执行形态（局部）。
        # 只给前者，"上游 16 路全在飞、还有 9 个作业在等槽位"这件事在读数上就是隐形的，
        # 用户看到的仍是"点了没反应"——那正是 P4 要消掉的那格。
        # `dispatch` 补的是同一族的另一格：分进程形态下 `WEB_DISPATCH=off` 而 worker 没起时，
        # queued 会一直涨、running 一直是 0，接口全绿——"没人认领"这件事只有这一帧会说。
        yield _sse({"type": "queue", **queueing.stats(), "gate": llm_gate.snapshot(), "dispatch": dispatch_form()})
        cursor = start
        buffered = 0  # 过程内缓冲那条路的"已发条数"，与库里的 seq 不是一个数，不共用变量
        while True:
            rows = eventlog.read_after(job_id, cursor)
            finished_in_buffer = False
            if not rows and not eventlog.has_events(job_id):
                # 事件没落库（sink 写失败过）⇒ 退化成过程内缓冲，与改造前等价。
                # 降级可以，静默不行：明着发一条 events_not_persisted。
                yield _sse({"type": "events_not_persisted", "job_id": job_id})
                events, _total, expired = manager.snapshot(job_id, buffered)
                # 游标要往前走：原来每次传 0，于是同一段缓冲每轮**重发一遍**——
                # 那条降级路自己就是把"补齐而不是重放"这句话弄反了。
                buffered += len(events)
                for event in events:
                    yield _sse(event)
                finished_in_buffer = bool(expired or manager.is_done(job_id))
            else:
                for seq, event in rows:
                    cursor = seq
                    yield _sse(event, seq)
            # 两条路都要问一次库里的状态。原来只有"落库那条"问：一条"事件从没落库、而行已被
            # 改成终态"的作业会把这条流**永远挂住**——P5-3 那条 SSE 用例实测撞到了，而它同时
            # 是那三次变异位点 TIMEOUT 420s 的真因（挂住的是被测端点，不是量具）。
            # 这条缺陷在 P5-2 之后变贵了：前端现在会自动重连，一条永不结束的流就是一条无限重连。
            row = query_one(
                f"SELECT status FROM jobs WHERE job_id = ?{sql}", (job_id, *params)
            )
            state = (row or {}).get("status")
            if state in TERMINAL_STATUSES:
                yield _sse({"type": "job_status", "status": state})
                break
            if finished_in_buffer:
                # 缓冲那条路自己判的"结束"（回收过 / 本进程跑完了），而库里还不是终态：
                # 可以收流，但**不许编一条 job_status**——那等于把"还没落到终态"说成结束了。
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
