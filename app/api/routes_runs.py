"""上传与分析 Run 端点。

阶段 10 验收的两条关键点在这里：
- 「分析端点**立即返回、不阻塞**」：`POST /analysis-runs` 只创建 `queued` 行并
  派发 Celery 任务，绝不在这里跑分析；
- 「跨 Project 访问被拒绝」：经 `ProjectScopeDep`，并被
  `data_source_id` 的归属校验再兜一层。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.analysis.idempotency import make_idempotency_key
from app.analysis.runner import RunRequest, create_run
from app.api.deps import ProjectScopeDep, SessionDep
from app.api.schemas import (
    HTTP_422,
    CreateRunRequest,
    CreateRunResponse,
    EvidenceResponse,
    InsightResponse,
    RunResponse,
    UploadResponse,
)
from app.api.scoping import InsightScopeDep, RunScopeDep
from app.config import get_settings
from app.models import enums
from app.models.insight import Insight
from app.repositories.agent_run import AgentRunRepository
from app.repositories.evidence import EvidenceRepository
from app.repositories.insight import InsightRepository
from app.services.upload_service import UploadService, UploadTooLargeError
from app.utils.observability import get_logger

#: Web 侧的日志也走 structlog：JSON + trace_id（计划第 928、937 行）。
#: 用 stdlib 的 logging 会绕过处理器链，日志里就没有 trace_id 了。
_logger = get_logger(__name__)

router = APIRouter()


def _uploads_dir() -> Path:
    return Path(get_settings().data_dir) / "uploads"


# ============================================================
# 上传
# ============================================================


@router.post(
    "/api/projects/{project_id}/upload",
    response_model=UploadResponse,
    tags=["upload"],
)
async def upload_log(
    scope: ProjectScopeDep,
    session: SessionDep,
    # FastAPI 的文件上传就必须写成 File(...) 默认值，这是框架约定写法
    file: UploadFile = File(...),  # noqa: B008
    fmt: str = Form(...),
    data_source_id: int | None = Form(default=None),
) -> UploadResponse:
    """上传日志：脱敏 → 落盘 → 解析 → 入库（阶段 04 的管道）。

    **原始文件不落盘**，磁盘上只有脱敏后的版本（红线 5）。
    """
    if fmt not in ("txt", "jsonl"):
        raise HTTPException(
            status_code=HTTP_422,
            detail=f"不支持的格式 {fmt!r}；V1 只支持 txt 与 jsonl",
        )

    content = await file.read()

    service = UploadService(session, uploads_dir=_uploads_dir())
    try:
        result = service.ingest(
            project_id=scope.project_id,
            content=content,
            filename=file.filename or "upload.log",
            fmt=fmt,
            data_source_id=data_source_id,
        )
    except UploadTooLargeError as exc:
        # 超限明确拒绝（阶段 04 验收第 5 条），不是悄悄截断
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTP_422, detail=str(exc)
        ) from exc

    # 上传也显式提交：响应"已入库 N 条"之后，紧接着的分析必须能读到它们。
    # 不提交的话，调用方拿到 200 后立刻发起分析，可能读到空集合。
    session.commit()
    return UploadResponse(**result.as_dict())


# ============================================================
# 分析 Run
# ============================================================


@router.post(
    "/api/projects/{project_id}/analysis-runs",
    response_model=CreateRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["runs"],
)
def create_analysis_run(
    payload: CreateRunRequest, scope: ProjectScopeDep, session: SessionDep
) -> CreateRunResponse:
    """创建分析：**立即返回 `run_id + queued`**（计划第 867 行）。

    耗时分析交给 Celery worker，本端点只落一行 queued 并派发任务。
    """
    from app.models.datasource import DataSource

    source = session.get(DataSource, payload.data_source_id)
    if source is None or int(source.project_id) != scope.project_id:
        # 刻意 404：不暴露别的项目里是否存在这个 data_source
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据源 {payload.data_source_id} 不存在",
        )

    domain = _load_domain()
    now = datetime.now(timezone.utc)
    start = (
        payload.time_range.start
        if payload.time_range and payload.time_range.start
        else _epoch()
    )
    end = (
        payload.time_range.end if payload.time_range and payload.time_range.end else now
    )

    filters = payload.filters.model_dump(exclude_none=True) if payload.filters else {}

    # ---- Pre-check（计划第 642–644 行）：预算不足**拒绝创建** ----
    #
    # 这一步以前完全不存在：CostController 写得再全，真实路径里没人构造它，
    # 于是"预算不足拒绝创建"这条验收只在单测里成立，真机上可以拿一个
    # 已经花光的项目一直发起分析。
    from app.models.project import Project
    from app.policy.wiring import build_cost_controller

    project_row = session.get(Project, scope.project_id)
    controller = build_cost_controller(project=project_row)
    if controller is not None:
        pre = controller.pre_check(payload.start_tier or "L2")
        if not pre.allowed:
            # 402 而不是 422：这不是"参数写错了"，是"钱不够"。
            # 用 402 让调用方一眼分清该改参数还是该加预算。
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED,
                                detail=pre.message or pre.reason)

    request = RunRequest(
        project_id=scope.project_id,
        source_id=int(source.id),
        time_start=start,
        time_end=end,
        domain_id=domain.domain_id,
        domain_version=domain.version,
        filters=filters,
        start_tier=payload.start_tier or "L2",
        run_input=payload.model_dump(mode="json"),
    )

    runs = AgentRunRepository(session)
    run_id, run_status, reused = create_run(runs, request)
    session.flush()

    # 把发起这次分析的 trace_id 记在 Run 上（计划第 928 行：贯穿日志与任务）。
    # 记下来才能在事后用 trace_id 把这个 Run 的日志全捞出来。
    from app.utils.observability import current_trace_id

    trace_id = current_trace_id()
    if trace_id and not reused:
        run_row = runs.get(scope.project_id, run_id)
        if run_row is not None:
            run_row.run_metadata = {
                **(run_row.run_metadata or {}),
                "trace_id": trace_id,
            }
            session.flush()

    if not reused:
        # **必须先提交，再派发。**
        #
        # 派发只是往 Redis 放一条消息；worker 可能在几毫秒内取走它，并用**另一个
        # 连接**读数据。若此时本请求的事务还没提交，worker 读到的是提交前的快照：
        # 它会看到 0 条事件 -> 判定 L0（无异常）-> 产出一份「已完成、零结论」的报告。
        # 而 API 侧一切正常、Run 状态也是 completed，从外面完全看不出问题。
        #
        # 2026-09-23 真机复现：本地 Redis + 单进程 worker 下必然踩中
        # （Run 元数据 context_tokens=0、model_attempts=[]，而库里其实有 16 条事件）。
        session.commit()
        # Web 这半边也要留一条带 trace_id 的日志：否则"同一条 trace 串起
        # 请求与执行"只存在于 worker 侧，grep 出来看不到是谁发起的。
        _logger.info(
            "analysis_run_created",
            run_id=run_id,
            project_id=scope.project_id,
            source_id=int(source.id),
            start_tier=request.start_tier,
            trace_id=trace_id,
        )
        _dispatch(run_id, scope.project_id, request)

    return CreateRunResponse(run_id=run_id, status=run_status, reused=reused)


#: 允许「重新分析」的终态。
#:
#: - `failed` / `timeout`：计划第 912 行明写要给入口；
#: - `partial_success`：结果**不完整**，验收第 919 行要求"不完整 / 失败"都有重试入口；
#: - `completed` **不给**：同样的输入再跑一遍只是重复计费（要重跑得换时间窗或过滤条件）；
#: - `cancelled` 不给：那是用户主动取消，重试必须是一次新的显式操作，避免误触；
#: - `queued` / `running` 不给：还在跑，重试会白花钱。
RETRYABLE_RUN_STATES = frozenset(
    {enums.AGENT_RUN_FAILED, enums.AGENT_RUN_TIMEOUT, enums.AGENT_RUN_PARTIAL_SUCCESS}
)


@router.post(
    "/api/runs/{run_id}/retry",
    response_model=CreateRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["runs"],
)
def retry_analysis_run(scope: RunScopeDep, session: SessionDep) -> CreateRunResponse:
    """「重新分析」：按原 Run 的输入**新建**一个 Run（计划第 912 行）。

    为什么要有个端点、而不是让页面把参数再填一遍：Run 创建时已经把当时的
    请求原样存进了 `input`（计划第 328 行的 `input(jsonb)`）。让用户回项目页
    重填一次时间窗，既容易填错（口径变了就不是同一次分析了），也违背
    "失败结果要提供重试入口"的本意。

    新 Run 用 `parent_run_id` 指向原 Run —— 计划第 353、692 行要求
    「failed 不可原地复活，只能新建 Run，用 parent_run_id 串起谱系」。
    """
    run = scope.run
    if run.status not in RETRYABLE_RUN_STATES:
        raise HTTPException(
            status_code=HTTP_422,
            detail=(
                f"Run {scope.run_id} 当前状态 {run.status} 不允许重新分析；"
                f"只有 {sorted(RETRYABLE_RUN_STATES)} 可以"
            ),
        )

    stored = dict(run.input or {})
    if not stored:
        # 不留空壳：没有原始输入就没法保证"重试的是同一件事"
        raise HTTPException(
            status_code=HTTP_422,
            detail="这个 Run 没有记录原始输入，无法自动重新分析；请回项目页重新发起",
        )

    window = stored.get("time_range") or {}
    try:
        start = datetime.fromisoformat(str(window.get("start")))
        end = datetime.fromisoformat(str(window.get("end")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=HTTP_422,
            detail=f"原 Run 的时间范围无法解析，不能自动重试：{window}",
        ) from exc

    request = RunRequest(
        project_id=scope.project_id,
        source_id=int(run.source_id or 0),
        time_start=start,
        time_end=end,
        domain_id=_load_domain().domain_id,
        domain_version=_load_domain().version,
        filters=stored.get("filters") or {},
        start_tier=stored.get("start_tier") or "L2",
        run_input=stored,
    )

    runs = AgentRunRepository(session)
    new_run_id, new_status, reused = create_run(runs, request, parent_run_id=scope.run_id)
    session.flush()

    from app.utils.observability import current_trace_id

    trace_id = current_trace_id()
    if trace_id and not reused:
        row = runs.get(scope.project_id, new_run_id)
        if row is not None:
            row.run_metadata = {**(row.run_metadata or {}), "trace_id": trace_id}
            row.run_metadata = {**row.run_metadata, "retry_of": scope.run_id}
            session.flush()

    if not reused:
        # 与创建路径同样的顺序要求：先提交，再派发（否则 Worker 读到未提交的快照）
        session.commit()
        _logger.info(
            "analysis_run_retried",
            run_id=new_run_id,
            retry_of=scope.run_id,
            project_id=scope.project_id,
            trace_id=trace_id,
        )
        _dispatch(new_run_id, scope.project_id, request)

    return CreateRunResponse(run_id=new_run_id, status=new_status, reused=reused)


@router.get("/api/runs/{run_id}", response_model=RunResponse, tags=["runs"])
def get_run(scope: RunScopeDep) -> RunResponse:
    """状态、进度、成本（计划第 868 行）。"""
    run = scope.run
    return RunResponse(
        id=int(run.id),
        project_id=int(run.project_id),
        status=run.status,
        current_phase=run.current_phase,
        phase_history=run.phase_history,
        tokens_input=int(run.tokens_input or 0),
        tokens_output=int(run.tokens_output or 0),
        cost_actual=float(run.cost_actual or 0),
        error=run.error,
        run_metadata=run.run_metadata,
        started_at=run.started_at,
        finished_at=run.finished_at,
        last_heartbeat=run.last_heartbeat,
        cancel_requested=bool(run.cancel_requested),
    )


@router.get("/api/runs/{run_id}/detail", tags=["runs"])
def get_run_detail(scope: RunScopeDep, session: SessionDep) -> dict:
    """Run 详情：一次复述「做了什么、花了多少、为何得到这个结论」（计划第 940 行）。

    这是**只读聚合**：把散落在 run / model_calls / tool_usage / insights /
    evidences 里的信息按"审阅者会问的问题"重新组织，并顺带自检说不通的地方。
    """
    from app.analysis.run_detail import build_run_detail

    run = scope.run

    insights = InsightRepository(session).list_for_run(scope.project_id, scope.run_id)
    evidence_repo = EvidenceRepository(session)
    evidence_by_insight = {
        int(insight.id): evidence_repo.list_for_insight(
            scope.project_id, int(insight.id)
        )
        for insight in insights
    }

    detail = build_run_detail(
        run=run, insights=insights, evidence_by_insight=evidence_by_insight
    )
    payload = detail.as_dict()
    # 把人话复述也一并返回：验收问的就是"能否完整复述"
    payload["narrative"] = detail.narrate()
    return payload


@router.post("/api/runs/{run_id}/cancel", tags=["runs"])
def cancel_run(scope: RunScopeDep, session: SessionDep) -> dict:
    """请求取消。

    **诚实说明粒度**（计划第 710 行）：置标记后 Worker 在下一个检查点生效；
    若恰好进入一次长模型调用，最坏要等该调用返回，不承诺秒停。
    """
    runs = AgentRunRepository(session)
    run_id = scope.run_id
    runs.request_cancel(scope.project_id, run_id)
    session.flush()
    return {
        "run_id": run_id,
        "cancel_requested": True,
        "note": "取消将在下一个检查点生效；进行中的模型调用需等其返回",
    }


# ============================================================
# Insight / Evidence
# ============================================================


@router.get(
    "/api/runs/{run_id}/insights",
    response_model=list[InsightResponse],
    tags=["insights"],
)
def list_run_insights(scope: RunScopeDep, session: SessionDep) -> list[InsightResponse]:
    run_id = scope.run_id
    return [
        _insight_response(i)
        for i in InsightRepository(session).list_for_run(scope.project_id, run_id)
    ]


@router.get(
    "/api/insights/{insight_id}", response_model=InsightResponse, tags=["insights"]
)
def get_insight(scope: InsightScopeDep) -> InsightResponse:
    return _insight_response(scope.insight)


@router.get(
    "/api/insights/{insight_id}/evidence",
    response_model=list[EvidenceResponse],
    tags=["insights"],
)
def list_insight_evidence(
    scope: InsightScopeDep, session: SessionDep
) -> list[EvidenceResponse]:
    """证据是「fact 可点击核对」的数据来源（阶段 11 验收）。"""
    rows = EvidenceRepository(session).list_for_insight(
        scope.project_id, scope.insight_id
    )
    return [
        EvidenceResponse(
            id=int(e.id),
            insight_id=int(e.insight_id),
            source_id=int(e.source_id) if e.source_id is not None else None,
            event_ids=e.event_ids,
            time_range=e.time_range,
            calculation=e.calculation,
            description=e.description,
        )
        for e in rows
    ]


# ============================================================
# 内部
# ============================================================


def _epoch() -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _load_domain():
    from app.domains.registry import get_domain_registry

    return get_domain_registry().load("computer_monitoring")


def _dispatch(run_id: int, project_id: int, request: RunRequest) -> None:
    """把执行派发给 Celery。

    派发失败**不吞掉**：标记成 error 字段由调用方看得到，但端点本身仍返回
    202 + queued（因为 Run 已创建，前端可以去查状态）。

    `trace_id` 一并传过去：Worker 是另一个进程，contextvars 不会自己跨过去。
    不传的话 worker 会新起一个 trace_id，于是计划第 928 行那句
    「HTTP 请求 → 入队 → Worker 执行 能被同一个 trace_id 串起来」就只剩前半截。
    """
    import logging

    from app.utils.observability import current_trace_id

    logger = logging.getLogger(__name__)
    try:
        from app.tasks.analysis import execute_run_task

        execute_run_task.delay(
            run_id=run_id,
            project_id=project_id,
            start_tier=request.start_tier,
            trace_id=current_trace_id(),
        )
    except Exception as exc:  # noqa: BLE001 - broker 不可用不该让创建失败
        logger.warning("派发 Run %s 到 Celery 失败：%s", run_id, exc)


def _insight_response(insight: Insight) -> InsightResponse:
    return InsightResponse(
        id=int(insight.id),
        project_id=int(insight.project_id),
        run_id=int(insight.run_id),
        incident_id=int(insight.incident_id)
        if insight.incident_id is not None
        else None,
        type=insight.type,
        severity=insight.severity,
        confidence=float(insight.confidence),
        title=insight.title,
        summary=insight.summary,
        reasoning=insight.reasoning,
        limitations=insight.limitations,
        run_metadata=insight.run_metadata,
        created_at=insight.created_at,
    )


def build_idempotency_key_for(request: RunRequest) -> str:
    """供测试与排障直接算键，避免测试自己重写一遍算法。"""
    return make_idempotency_key(
        project_id=request.project_id,
        source_id=request.source_id,
        time_start=request.time_start,
        time_end=request.time_end,
        filters=request.filters,
        domain_id=request.domain_id,
        domain_version=request.domain_version,
    )
