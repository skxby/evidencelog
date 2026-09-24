"""分析 Run 的 Celery 任务（阶段 08 的 Worker 落地）。

职责边界：本任务负责**从库里取出 Run、跑链路、写回状态**；
可靠性机制（状态机、重试、降级、心跳、取消）全部复用阶段 08 的 RunExecutor，
不在这里重新实现一遍。
"""

from __future__ import annotations

from typing import Any

from app.analysis.heartbeat import CancelledError
from app.analysis.pipeline import PipelineInput, analyze
from app.analysis.runner import RunExecutor
from app.celery_app import celery_app
from app.db import SessionLocal
from app.utils.observability import bind_run_context, get_logger

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.analysis.execute_run", bind=True)
def execute_run_task(
    self: Any,
    *,
    run_id: int,
    project_id: int,
    start_tier: str = "L2",
    trace_id: str | None = None,
) -> dict[str, Any]:
    """执行一次分析 Run。

    Windows 上 worker 需加 `-P solo`（见 AGENTS.md 技术栈）。

    `trace_id` 由发起请求的那次 HTTP 调用传进来（见 `_dispatch`）：
    contextvars **不跨进程**，不显式传的话 worker 会另起一个 trace_id，
    "同一条 trace 串起请求与执行"就只剩前半截 —— 拿着 Run 详情里的
    trace_id 去 grep，worker 那半边一条都捞不到。传了就沿用同一个。
    """
    # 任务也在同一个 trace 下：这样"HTTP 请求 → 入队 → Worker 执行"
    # 的日志能被同一个 trace_id 串起来（计划第 928 行）
    with bind_run_context(trace_id=trace_id, run_id=run_id, project_id=project_id):
        return _execute(
            session_factory=SessionLocal,
            run_id=run_id,
            project_id=project_id,
            start_tier=start_tier,
        )


def _execute(
    *, session_factory, run_id: int, project_id: int, start_tier: str
) -> dict[str, Any]:
    session = session_factory()
    try:
        from app.domains.registry import get_domain_registry
        from app.repositories.agent_run import AgentRunRepository
        from app.repositories.event import EventRepository

        runs = AgentRunRepository(session)
        run = runs.get(project_id, run_id)
        if run is None:
            logger.warning(
                "Run %s 不存在（project %s），任务不做事", run_id, project_id
            )
            return {"run_id": run_id, "status": "missing"}

        domain = get_domain_registry().load("computer_monitoring")

        # 载入本次事件（受 project 边界约束）。
        # 必须经 EventRepository.pipeline_events —— 那里是 ORM→链路字段映射的
        # 唯一真源。早先这里手写了一份"看起来等价"的映射，唯独漏了 metadata
        # （进程名在其中），于是 analyzer 全部判定"无异常"、复杂度落到 L0、
        # 模型一次都没被调用，Run 却显示 completed。
        events = EventRepository(session).pipeline_events(project_id)
        # 事件条数是排障的第一现场：0 条事件会让整条链路"成功地产出零结论"，
        # 而这个数字是唯一能一眼看出来的证据
        logger.info("run_events_loaded", event_count=len(events))

        executor = RunExecutor(run_repository=runs)
        captured: dict[str, Any] = {}

        def attempt_tier(tier: str) -> Any:
            """每个等级尝试一次完整链路。"""
            try:
                return _attempt_tier_inner(tier)
            except Exception as exc:
                import traceback

                logger.error(
                    "attempt_tier_failed",
                    tier=tier,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc()[-2000:],
                )
                raise

        def _attempt_tier_inner(tier: str) -> Any:
            executor.heartbeat(project_id, run_id)
            executor.check_cancel(project_id, run_id)

            from app.gateways.router import build_router

            settings = _settings()
            try:
                router = build_router(settings, run_repository=runs)
            except Exception as exc:
                # 装配 router 失败（配置缺失、凭据找不到…）必须留痕。
                # 否则它会被降级链当成"这个等级失败"吞掉，
                # 最终以"分析完成、零结论"收场 —— 真正的原因一个字都不剩。
                logger.error(
                    "build_router_failed",
                    tier=tier,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            result = analyze(
                PipelineInput(
                    project_id=project_id,
                    source_id=int(run.source_id or 0),
                    domain_id=domain.domain_id,
                    domain_version=domain.version,
                    time_start=run.started_at or _utcnow(),
                    time_end=_utcnow(),
                    events=events,
                ),
                analyzers=domain.analyzers(),
                domain=domain,
                router=router,
                record=True,
                project_id_for_recording=project_id,
                run_id=run_id,
            )
            captured["result"] = result
            # 关键节点留痕：**这才是 trace_id 能派上用场的地方**。
            # 计划第 928 行要求"同一条 trace 串起请求与执行"，可早先整条成功路径
            # 一条日志都不打 —— 于是拿 trace_id 去 grep 什么都捞不到，
            # "有 trace" 只是形式上的。等级判定、候选异常、模型调用与花费
            # 是排障时最先要看的三件事，全部记上。
            logger.info(
                "pipeline_done",
                tier=result.tier,
                anomaly_count=len(result.anomalies),
                anomaly_types=sorted({str(a.get("type")) for a in result.anomalies}),
                context_tokens=result.context_tokens,
                model_attempts=len(result.model_attempts),
            )
            # 落库必须在**这里**、也就是"状态机写终态之前"完成。
            #
            # 放在 execute() 之后是不行的：那时 Run 已经是 completed/failed 这样的
            # 终态，而状态机明令终态不可再改。于是落库一旦失败，就只能眼睁睁看着
            # 一个"已完成但 0 条结论"的 Run 留在库里 —— 恰好是红线 4 要禁止的含糊。
            # 放进 attempt_tier 里，落库失败就变成一次**等级尝试失败**，
            # 由降级链与状态机如实收尾成 failed，并把真实原因写进 error。
            captured["persisted"] = _persist_result(
                session, project_id=project_id, run_id=run_id, run=run, result=result
            )
            logger.info("insights_persisted", **captured["persisted"])
            return result

        def rules_only() -> Any:
            """模型全不可用时的纯规则兜底（计划第 724 行）。"""
            result = analyze(
                PipelineInput(
                    project_id=project_id,
                    source_id=int(run.source_id or 0),
                    domain_id=domain.domain_id,
                    domain_version=domain.version,
                    time_start=run.started_at or _utcnow(),
                    time_end=_utcnow(),
                    events=events,
                ),
                analyzers=domain.analyzers(),
                domain=domain,
                router=None,  # 不调模型
            )
            captured["result"] = result
            return result

        try:
            outcome = executor.execute(
                project_id=project_id,
                run_id=run_id,
                attempt_tier=attempt_tier,
                start_tier=start_tier,
                rules_only_fallback=rules_only,
            )
        except CancelledError:
            logger.info("run_cancelled", run_id=run_id)
            return {"run_id": run_id, "status": "cancelled"}

        # 落库已在 attempt_tier 内完成（见那里的说明）。这里只取结果：
        # 失败时 captured 里没有它，Run 已被降级链收尾成 failed。
        result = captured.get("result")
        persisted = captured.get("persisted") or {
            "insight_count": 0,
            "evidence_count": 0,
        }

        # 回填成本与 token（计划第 650–652 行的 Post-check）
        if result is not None:
            run.tokens_input = sum(
                int(a.get("tokens_input") or 0) for a in result.model_attempts
            )
            run.tokens_output = sum(
                int(a.get("tokens_output") or 0) for a in result.model_attempts
            )
            run.cost_actual = sum(
                float(a.get("cost") or 0) for a in result.model_attempts
            )

        # 把产出条数与降级事实回填进 Run 元数据 —— 否则 Run 详情只会显示
        # "0 条结论、0 次模型调用"，而**真实发生的事**（结构化输出失败、
        # 降级到纯规则）全都看不见，读者会以为分析正常但没发现异常。
        metadata_extra: dict[str, Any] = {
            "insight_count": int(persisted.get("insight_count", 0)),
            "evidence_count": int(persisted.get("evidence_count", 0)),
        }
        # 降级链的尝试明细：哪个等级失败、失败原因是什么。
        # 不记的话，Run 详情只能显示"完成"，看不出中间降过级、为什么降。
        metadata_extra["attempts"] = list(outcome.attempts or [])
        if result is not None:
            metadata_extra["used_rules_only"] = bool(result.used_rules_only)
            metadata_extra["context_tokens"] = int(result.context_tokens)
            metadata_extra["model_attempts"] = result.model_attempts
        run.run_metadata = {**(run.run_metadata or {}), **metadata_extra}

        # 终态与花费：这次分析到底干了什么的一句话总结。放在成本回填之后 ——
        # 放前面的话 tokens/cost 还是 0，日志会替库里"作证"说这次没花钱。
        # 与 run_events_loaded / pipeline_done 同属一条 trace，一条 grep 全出来。
        logger.info(
            "run_finished",
            status=outcome.status,
            used_rules_only=outcome.used_rules_only,
            stop_reason=outcome.stop_reason,
            insight_count=int(persisted.get("insight_count", 0)),
            context_tokens=int(result.context_tokens) if result is not None else 0,
            tokens_input=int(run.tokens_input or 0),
            tokens_output=int(run.tokens_output or 0),
            cost=float(run.cost_actual or 0),
        )

        # 提交！这一步绝不能只 flush。
        #
        # 这个任务**自己管会话**（不像 HTTP 路由那样由 get_db 依赖负责提交），
        # 只 flush 的话 `session.close()` 会把整段执行结果回滚掉：状态机算出的
        # completed/partial_success/failed、回填的 token 与成本、落库的
        # Insight 与 Evidence，**全部消失**，Run 永远停在 queued。
        # 更阴险的是返回值仍然是对的 —— "任务报告成功"与"库里没变"能同时成立。
        session.commit()
        return {
            "run_id": run_id,
            "status": outcome.status,
            "used_rules_only": outcome.used_rules_only,
            "insights": persisted,
        }
    except Exception as exc:
        # 这一层兜底是"真起全栈跑一遍"逼出来的：
        # 落库时的约束冲突（CHECK 不认模型给的 type）从任务里逃逸出去，
        # `session.close()` 把整个事务回滚 —— Run 永远停在 queued，
        # 页面上一切正常，只有 Worker 日志里才有真相。
        # 任何逃逸异常都必须先在 Run 上留下痕迹，再抛出（红线 4）。
        _record_crash(session, project_id=project_id, run_id=run_id, exc=exc)
        raise
    finally:
        session.close()


def _persist_result(
    session: Any, *, project_id: int, run_id: int, run: Any, result: Any
) -> dict[str, int]:
    """把结论与证据落库；失败必须变成一次**明确的**失败，不许静默。

    - 证据闸门拒绝（结论本身不合格）→ `ValidationFailureError`（不可重试）
    - 数据库写不进去（约束冲突、连接中断…）→ `StorageFailureError`（不可重试）

    两者都不可重试的理由一样：模型已经调完、钱已经花了，重试只会产出
    同一份写不进去的结论，再花一次钱。降级链会因此立即收尾成 `failed`
    并把真实原因写进 Run.error，而不是继续往下白白烧钱。
    """
    if result is None or not result.insights:
        return {"insight_count": 0, "evidence_count": 0}

    from sqlalchemy.exc import SQLAlchemyError

    from app.analysis.idempotency import StorageFailureError, ValidationFailureError
    from app.analysis.persistence import EvidencePersistenceError, persist_insights
    from app.repositories.evidence import EvidenceRepository
    from app.repositories.insight import InsightRepository

    written = None
    try:
        # SAVEPOINT：落库失败只回滚这一小段，外层事务仍然可用 ——
        # 否则 PG 会把整个事务置为 aborted，连"把失败写进 Run"都做不到，
        # 于是又变成"任务崩了、Run 停在 queued"。
        with session.begin_nested():
            written = persist_insights(
                project_id=project_id,
                run_id=run_id,
                insights=result.insights,
                valid_event_ids=set(result.valid_event_ids),
                insight_repository=InsightRepository(session),
                evidence_repository=EvidenceRepository(session),
                source_id=int(run.source_id) if run.source_id else None,
            )
    except EvidencePersistenceError as exc:
        logger.error(
            "insights_rejected_by_evidence_gate", run_id=run_id, error=str(exc)
        )
        raise ValidationFailureError(f"证据校验失败：{exc}") from exc
    except SQLAlchemyError as exc:
        logger.error(
            "insights_persist_failed",
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise StorageFailureError(f"结论落库失败：{type(exc).__name__}: {exc}") from exc

    return written.as_dict()


def _record_crash(
    session: Any, *, project_id: int, run_id: int, exc: BaseException
) -> None:
    """把逃逸异常如实写进 Run（尽量），失败只记日志。

    只在 Run 处于 `running` 时改状态：`queued → failed` 不是计划里允许的转移，
    硬改就是绕过状态机。停在 queued 的 Run 由心跳超时回收判定
    （见 state_machine 里对 `queued → timeout` 的说明）。
    """
    from app.analysis.idempotency import classify_error
    from app.models import enums
    from app.repositories.agent_run import AgentRunRepository

    message = f"{type(exc).__name__}: {exc}"
    try:
        # PG 在事务出错后会拒绝后续语句，先回滚再写
        session.rollback()
        runs = AgentRunRepository(session)
        run = runs.get(project_id, run_id)
        if run is None:
            return
        current = str(run.status)
        if current in enums.AGENT_RUN_TERMINAL_STATES:
            logger.error(
                "execute_run_crashed_after_terminal",
                run_id=run_id,
                status=current,
                error=message,
            )
            return
        if current != enums.AGENT_RUN_RUNNING:
            logger.error(
                "execute_run_crashed_before_running",
                run_id=run_id,
                status=current,
                error=message,
            )
            return
        runs.apply_status(
            project_id,
            run_id,
            target=enums.AGENT_RUN_FAILED,
            reason=classify_error(exc),
            error=f"Worker 异常终止：{message}",
        )
        session.commit()
        logger.error("execute_run_crashed", run_id=run_id, error=message)
    except Exception:
        session.rollback()
        logger.exception("record_crash_failed", run_id=run_id, error=message)


def _settings() -> Any:
    from app.config import get_settings

    return get_settings()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
