"""分析 Run 的 Celery 任务（阶段 08 的 Worker 落地）。

职责边界：本任务负责**从库里取出 Run、跑链路、写回状态**；
可靠性机制（状态机、重试、降级、心跳、取消）全部复用阶段 08 的 RunExecutor，
不在这里重新实现一遍。
"""

from __future__ import annotations

import logging
from typing import Any

from app.analysis.heartbeat import CancelledError
from app.analysis.pipeline import PipelineInput, analyze
from app.analysis.runner import RunExecutor
from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.analysis.execute_run", bind=True)
def execute_run_task(
    self: Any, *, run_id: int, project_id: int, start_tier: str = "L2"
) -> dict[str, Any]:
    """执行一次分析 Run。

    Windows 上 worker 需加 `-P solo`（见 AGENTS.md 技术栈）。
    """
    session = SessionLocal()
    try:
        from app.analysis.persistence import persist_insights
        from app.domains.registry import get_domain_registry
        from app.models.event import Event
        from app.repositories.agent_run import AgentRunRepository
        from app.repositories.evidence import EvidenceRepository
        from app.repositories.insight import InsightRepository

        runs = AgentRunRepository(session)
        run = runs.get(project_id, run_id)
        if run is None:
            logger.warning("Run %s 不存在（project %s），任务不做事", run_id, project_id)
            return {"run_id": run_id, "status": "missing"}

        domain = get_domain_registry().load("computer_monitoring")

        # 载入本次事件（受 project 边界约束）
        from sqlalchemy import select

        events = [
            {
                "event_id": int(e.id),
                "source_id": int(e.source_id),
                "timestamp": e.timestamp,
                "severity": e.severity,
                "event_type": e.event_type,
                "message": e.message,
                "payload": e.payload,
            }
            for e in session.execute(
                select(Event)
                .where(Event.project_id == project_id)
                .order_by(Event.timestamp)
                .limit(50_000)
            ).scalars()
        ]

        executor = RunExecutor(run_repository=runs)
        captured: dict[str, Any] = {}

        def attempt_tier(tier: str) -> Any:
            """每个等级尝试一次完整链路。"""
            executor.heartbeat(project_id, run_id)
            executor.check_cancel(project_id, run_id)

            from app.gateways.router import build_router

            settings = _settings()
            router = build_router(settings, run_repository=runs)
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
            return {"run_id": run_id, "status": "cancelled"}

        # 落库：Insight + Evidence（阶段 09 的写入闸门会再校验一次证据）
        result = captured.get("result")
        persisted = {"insight_count": 0, "evidence_count": 0}
        if result is not None and result.insights:
            from app.analysis.persistence import (
                EvidencePersistenceError,
                persist_insights,
            )

            try:
                written = persist_insights(
                    project_id=project_id,
                    run_id=run_id,
                    insights=result.insights,
                    valid_event_ids=set(result.valid_event_ids),
                    insight_repository=InsightRepository(session),
                    evidence_repository=EvidenceRepository(session),
                    source_id=int(run.source_id) if run.source_id else None,
                )
                persisted = written.as_dict()
            except EvidencePersistenceError as exc:
                # 落库侧证据闸门拦下：如实记进 Run，不让脏结论进库
                logger.error("Run %s 落库被证据闸门拒绝：%s", run_id, exc)
                run.error = f"证据校验失败：{exc}"

        # 回填成本与 token（计划第 650–652 行的 Post-check）
        if result is not None:
            run.tokens_input = sum(
                int(a.get("tokens_input") or 0) for a in result.model_attempts
            )
            run.tokens_output = sum(
                int(a.get("tokens_output") or 0) for a in result.model_attempts
            )
            run.cost_actual = sum(float(a.get("cost") or 0) for a in result.model_attempts)

        session.flush()
        return {
            "run_id": run_id,
            "status": outcome.status,
            "used_rules_only": outcome.used_rules_only,
            "insights": persisted,
        }
    finally:
        session.close()


def _settings() -> Any:
    from app.config import get_settings

    return get_settings()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
