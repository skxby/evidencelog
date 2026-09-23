"""阶段 08 验收的集成测试（真实 PostgreSQL）。

对应计划第 731 行验收：
  创建 Run 立即返回 run_id + queued；
  手动 kill Worker 后僵尸 Run 被回收；
  重复提交不产生重复 Run；
  取消能在下一个检查点生效；
  模型不可用时能降级到 L0 报告；
  各类错误分类正确。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.heartbeat import CancelledError
from app.analysis.idempotency import ErrorKind, InputFormatError
from app.analysis.retry import RetryPolicy
from app.analysis.runner import RunExecutor, RunRequest, create_run
from app.db import SessionLocal
from app.gateways.base import ModelUnavailableError
from app.models import DataSource, Project, User, enums
from app.repositories import (
    AgentRunRepository,
    DataSourceRepository,
    ProjectRepository,
    UserRepository,
)

pytestmark = pytest.mark.integration
UTC = timezone.utc
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def ctx(session):
    users = UserRepository(session)
    user = users.add(
        User(email=f"stage08-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    project = ProjectRepository(session).add(
        Project(user_id=user.id, name="p08", budget_total=10, budget_used=0)
    )
    session.flush()
    source = DataSourceRepository(session).add(
        project.id,
        DataSource(project_id=project.id, type="file_upload", format="txt", location="a.log"),
    )
    session.flush()
    runs = AgentRunRepository(session)
    return project, source, runs


def _request(project, source, **overrides) -> RunRequest:
    base = {
        "project_id": project.id,
        "source_id": source.id,
        "time_start": T0,
        "time_end": T0 + timedelta(hours=1),
        "domain_id": "computer_monitoring",
        "domain_version": "1.0.0",
        "filters": {"severity": ["high"]},
        "start_tier": "L2",
    }
    base.update(overrides)
    return RunRequest(**base)


def _executor(runs, **kwargs) -> RunExecutor:
    defaults = {"retry_policy": RetryPolicy(max_retries=0)}
    defaults.update(kwargs)
    return RunExecutor(run_repository=runs, **defaults)


# ============================================================
# 验收：创建 Run 立即返回 run_id + queued
# ============================================================


def test_create_run_returns_run_id_and_queued_immediately(session, ctx):
    project, source, runs = ctx
    run_id, status, reused = create_run(runs, _request(project, source))
    session.flush()

    assert isinstance(run_id, int) and run_id > 0
    assert status == enums.AGENT_RUN_QUEUED
    assert reused is False

    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_QUEUED
    # 创建时不该有任何执行痕迹
    assert stored.started_at is None
    assert stored.model_calls is None


def test_create_run_persists_the_idempotency_key(session, ctx):
    project, source, runs = ctx
    request = _request(project, source)
    run_id, _, _ = create_run(runs, request)
    session.flush()

    assert runs.get(project.id, run_id).idempotency_key == request.idempotency_key()


# ============================================================
# 验收：重复提交不产生重复 Run
# ============================================================


def test_same_request_is_reused_without_creating_a_second_run(session, ctx):
    """计划第 703 行：相同键且已有成功 Run → 直接复用，不重复执行/计费。"""
    project, source, runs = ctx
    request = _request(project, source)

    first_id, _, _ = create_run(runs, request)
    session.flush()
    # 把它标记为已完成（模拟上次跑成功）
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_COMPLETED)
    session.flush()

    second_id, status, reused = create_run(runs, _request(project, source))
    session.flush()

    assert reused is True
    assert second_id == first_id, "应复用同一个 Run"
    assert status == enums.AGENT_RUN_COMPLETED
    assert runs.count(project.id) == 1, "不应产生第二个 Run"


def test_different_filters_produce_a_different_run(session, ctx):
    """幂等键包含 filters：条件不同就是不同的分析。"""
    project, source, runs = ctx
    first_id, _, _ = create_run(runs, _request(project, source, filters={"severity": ["high"]}))
    session.flush()
    second_id, _, reused = create_run(
        runs, _request(project, source, filters={"severity": ["low"]})
    )
    session.flush()

    assert reused is False
    assert second_id != first_id
    assert runs.count(project.id) == 2


def test_partial_success_is_not_reused_as_a_completed_result(session, ctx):
    """partial_success 本身不完整，拿它当"已有结果"会把不完整当完整交付。"""
    project, source, runs = ctx
    request = _request(project, source)
    first_id, _, _ = create_run(runs, request)
    session.flush()
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_PARTIAL_SUCCESS)
    session.flush()

    second_id, status, reused = create_run(runs, _request(project, source))
    session.flush()

    assert reused is False
    assert second_id != first_id
    assert status == enums.AGENT_RUN_QUEUED


# ============================================================
# 验收：手动 kill Worker 后僵尸 Run 被回收
# ============================================================


def test_zombie_run_is_reclaimed_as_timeout(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    # 模拟 Worker 被杀：心跳停在很久以前
    stale = datetime.now(UTC) - timedelta(seconds=1000)
    runs.touch_heartbeat(project.id, run_id, at=stale)
    session.flush()

    reclaimed = runs.reclaim_zombies(timeout_seconds=300)
    session.flush()
    session.expire_all()

    assert run_id in reclaimed
    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_TIMEOUT
    assert stored.finished_at is not None
    assert stored.run_metadata["stop_reason"] == "timeout"
    assert "心跳丢失" in stored.run_metadata["note"]


def test_fresh_run_is_not_reclaimed(session, ctx):
    """阈值必须大于单次最慢调用，否则正在正常工作的 Run 会被误杀。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    runs.touch_heartbeat(project.id, run_id, at=datetime.now(UTC))
    session.flush()

    assert runs.reclaim_zombies(timeout_seconds=300) == []
    session.expire_all()
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_RUNNING


def test_queued_run_is_not_reclaimed_by_heartbeat(session, ctx):
    """queued 还没被 Worker 接手，没有心跳是正常的。"""
    project, source, runs = ctx
    _run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    assert runs.reclaim_zombies(timeout_seconds=300) == []


def test_terminal_run_is_never_reclaimed(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_COMPLETED)
    session.flush()

    assert runs.reclaim_zombies(timeout_seconds=1) == []
    session.expire_all()
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_COMPLETED


# ============================================================
# 验收：取消能在下一个检查点生效
# ============================================================


def test_cancel_flag_persists_and_is_visible(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    assert runs.is_cancel_requested(project.id, run_id) is False
    assert runs.request_cancel(project.id, run_id) is True
    session.flush()
    assert runs.is_cancel_requested(project.id, run_id) is True

    # 幂等：重复置位无副作用
    assert runs.request_cancel(project.id, run_id) is True
    session.flush()
    assert runs.is_cancel_requested(project.id, run_id) is True


def test_cancel_takes_effect_at_the_next_checkpoint(session, ctx):
    """取消打在检查点前，执行器应在下一个检查点抛出并置 cancelled。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.request_cancel(project.id, run_id)
    session.flush()

    executor = _executor(runs)
    called: list[str] = []

    outcome = executor.execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=lambda tier: called.append(tier) or "should-not-happen",
        start_tier="L2",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_CANCELLED
    assert called == [], "取消后不该再发起模型调用"
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_CANCELLED


def test_check_cancel_raises_cancelled_error(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.request_cancel(project.id, run_id)
    session.flush()

    with pytest.raises(CancelledError):
        _executor(runs).check_cancel(project.id, run_id)


# ============================================================
# 验收：模型不可用时能降级到 L0 报告
# ============================================================


def test_model_unavailable_falls_back_to_rules_only_partial_success(session, ctx):
    """计划第 724–725 行：全等级失败 → 纯规则 L0 报告 + partial_success
    + stop_reason=model_unavailable。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    def always_unavailable(tier: str) -> str:
        raise ModelUnavailableError("503 服务不可用")

    executor = RunExecutor(run_repository=runs, retry_policy=RetryPolicy(max_retries=0))
    outcome = executor.execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=always_unavailable,
        start_tier="L3",
        rules_only_fallback=lambda: "analyzer 的确定性结果",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_PARTIAL_SUCCESS
    assert outcome.used_rules_only is True
    assert outcome.stop_reason == "model_unavailable"
    assert outcome.value == "analyzer 的确定性结果"

    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_PARTIAL_SUCCESS
    assert stored.run_metadata["stop_reason"] == "model_unavailable"

    # 降级链确实走过 L3 → L2 → L1
    tiers_tried = [a["tier"] for a in outcome.attempts]
    assert tiers_tried == ["L3", "L2", "L1"]


def test_successful_run_marks_completed(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    outcome = _executor(runs).execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=lambda tier: f"result-{tier}",
        start_tier="L2",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_COMPLETED
    assert outcome.used_rules_only is False
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_COMPLETED


# ============================================================
# 验收：各类错误分类正确（且失败不静默）
# ============================================================


def test_input_error_fails_the_run_with_reason(session, ctx):
    """计划第 728 行：失败不静默 —— 置 failed 并写明阶段与原因。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    def bad_input(tier: str) -> str:
        raise InputFormatError("日志格式不认识")

    outcome = _executor(runs).execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=bad_input,
        start_tier="L1",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_FAILED
    assert outcome.stop_reason == ErrorKind.INPUT
    assert "InputFormatError" in (outcome.error or "")

    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_FAILED
    assert "InputFormatError" in (stored.error or "")


def test_unexpected_exception_still_terminates_the_run(session, ctx):
    """任何未预期异常也必须以明确状态收尾，不留 running 悬挂。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    def boom(tier: str) -> str:
        raise ZeroDivisionError("unexpected")

    outcome = _executor(runs).execute(
        project_id=project.id, run_id=run_id, attempt_tier=boom, start_tier="L1"
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_FAILED
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_FAILED


def test_executor_never_leaves_a_run_in_running(session, ctx):
    """两种结局各跑一遍，都不该留下 running。"""
    project, source, runs = ctx

    scenarios = (
        (lambda tier: "ok", enums.AGENT_RUN_COMPLETED),
        (
            lambda tier: (_ for _ in ()).throw(InputFormatError("bad")),
            enums.AGENT_RUN_FAILED,
        ),
    )

    for index, (attempt, expected) in enumerate(scenarios):
        # 用 index 作为 filters 的一部分，保证两个场景的幂等键不同
        run_id, _, _ = create_run(
            runs, _request(project, source, filters={"scenario": index})
        )
        session.flush()
        _executor(runs).execute(
            project_id=project.id, run_id=run_id, attempt_tier=attempt, start_tier="L1"
        )
        session.flush()
        session.expire_all()
        assert runs.get(project.id, run_id).status == expected


def test_heartbeat_is_updated_during_execution(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    seen: list[datetime | None] = []

    def probe(tier: str) -> str:
        seen.append(runs.get(project.id, run_id).last_heartbeat)
        return "ok"

    _executor(runs).execute(
        project_id=project.id, run_id=run_id, attempt_tier=probe, start_tier="L1"
    )
    session.flush()
    session.expire_all()

    stored = runs.get(project.id, run_id)
    assert stored.last_heartbeat is not None, "执行期必须写心跳，否则僵尸回收无从判断"
