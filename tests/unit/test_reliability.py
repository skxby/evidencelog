"""阶段 08 可靠性机制单测：状态机 / 幂等 / 重试 / 心跳与取消。

对应计划第 731 行验收里可离线验证的部分；真实落库的部分见
`tests/integration/test_run_lifecycle_pg.py`。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.heartbeat import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    CancelledError,
    HeartbeatRecord,
    check_cancelled,
    heartbeat_deadline,
    is_zombie,
    should_abort,
    validate_heartbeat_timeout,
)
from app.analysis.idempotency import (
    RETRYABLE_KINDS,
    AuthenticationError,
    BudgetExhaustedError,
    ErrorKind,
    InputFormatError,
    StorageFailureError,
    ValidationFailureError,
    canonical_json,
    classify_error,
    compute_idempotency_key,
    is_retryable,
    make_idempotency_key,
    normalize_time_range,
)
from app.analysis.retry import (
    MAX_RETRIES,
    RetryPolicy,
    call_with_fallback,
    call_with_retry,
    downgrade_ladder,
)
from app.analysis.state_machine import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    StateMachine,
    allowed_targets,
    can_transition,
    is_terminal,
)
from app.gateways.base import ModelUnavailableError, StructuredOutputError
from app.models.enums import (
    AGENT_RUN_CANCELLED,
    AGENT_RUN_COMPLETED,
    AGENT_RUN_FAILED,
    AGENT_RUN_PARTIAL_SUCCESS,
    AGENT_RUN_QUEUED,
    AGENT_RUN_RUNNING,
    AGENT_RUN_STATES,
    AGENT_RUN_TIMEOUT,
)
from app.policy import StopExecution

UTC = timezone.utc
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


# ============================================================
# 状态机
# ============================================================


def test_transition_table_covers_all_seven_states():
    assert set(ALLOWED_TRANSITIONS) == set(AGENT_RUN_STATES)
    assert len(ALLOWED_TRANSITIONS) == 7


def test_queued_goes_to_running_or_cancelled():
    assert can_transition(AGENT_RUN_QUEUED, AGENT_RUN_RUNNING)
    assert can_transition(AGENT_RUN_QUEUED, AGENT_RUN_CANCELLED)
    # 不允许 queued 直接到 completed（跳过 running 就没有执行记录）
    assert not can_transition(AGENT_RUN_QUEUED, AGENT_RUN_COMPLETED)
    assert not can_transition(AGENT_RUN_QUEUED, AGENT_RUN_FAILED)


@pytest.mark.parametrize(
    "target",
    [
        AGENT_RUN_COMPLETED,
        AGENT_RUN_PARTIAL_SUCCESS,
        AGENT_RUN_FAILED,
        AGENT_RUN_TIMEOUT,
        AGENT_RUN_CANCELLED,
    ],
)
def test_running_reaches_every_documented_outcome(target: str):
    assert can_transition(AGENT_RUN_RUNNING, target)


@pytest.mark.parametrize(
    "terminal",
    [
        AGENT_RUN_COMPLETED,
        AGENT_RUN_PARTIAL_SUCCESS,
        AGENT_RUN_FAILED,
        AGENT_RUN_TIMEOUT,
        AGENT_RUN_CANCELLED,
    ],
)
def test_terminal_states_cannot_change(terminal: str):
    """计划第 691 行：终态不可再改。"""
    assert is_terminal(terminal)
    assert allowed_targets(terminal) == frozenset()
    for target in AGENT_RUN_STATES:
        assert not can_transition(terminal, target)


def test_failed_cannot_be_revived_in_place():
    """计划第 692 行：failed 不可原地复活，只能新建 Run。"""
    with pytest.raises(IllegalTransitionError, match="终态"):
        StateMachine(status=AGENT_RUN_FAILED).transition(AGENT_RUN_RUNNING)


def test_illegal_transition_reports_allowed_targets():
    machine = StateMachine(status=AGENT_RUN_QUEUED)
    with pytest.raises(IllegalTransitionError, match="允许的目标"):
        machine.transition(AGENT_RUN_FAILED)


def test_unknown_status_is_refused():
    with pytest.raises(IllegalTransitionError, match="未知状态"):
        StateMachine(status="bogus")
    with pytest.raises(IllegalTransitionError, match="未知状态"):
        can_transition(AGENT_RUN_QUEUED, "bogus")


def test_start_moves_to_running():
    machine = StateMachine()
    assert machine.start(phase="parse") == AGENT_RUN_RUNNING
    assert machine.current_phase == "parse"


def test_phase_progress_is_recorded_and_orthogonal_to_status():
    """status 管生命周期，current_phase/phase_history 管跑到哪一步（计划第 354 行）。"""
    machine = StateMachine()
    machine.start()
    machine.enter_phase("parse")
    machine.enter_phase("group")
    machine.skip_phase("model", reason="budget")

    assert machine.status == AGENT_RUN_RUNNING, "推进阶段不该改变 status"
    assert machine.current_phase == "model" or machine.current_phase == "group"
    assert "parse" in machine.completed_phases()
    assert "model" in machine.skipped_phases()


def test_phase_progress_refused_after_terminal():
    machine = StateMachine(status=AGENT_RUN_COMPLETED)
    with pytest.raises(IllegalTransitionError, match="终态"):
        machine.enter_phase("parse")


def test_phase_history_entries_carry_timestamp_and_status():
    machine = StateMachine()
    machine.start()
    machine.enter_phase("parse")
    entry = machine.phase_history[-1]
    assert entry["phase"] == "parse"
    assert entry["status"] == "done"
    assert entry["at"]


def test_full_happy_path():
    machine = StateMachine()
    machine.start()
    machine.enter_phase("parse")
    machine.enter_phase("analyze")
    machine.transition(AGENT_RUN_COMPLETED)
    assert machine.status == AGENT_RUN_COMPLETED
    assert machine.is_terminal


# ============================================================
# 幂等键
# ============================================================


def _key(**overrides):
    base = {
        "project_id": 1,
        "source_id": 2,
        "time_start": T0,
        "time_end": T0 + timedelta(hours=1),
        "filters": {"severity": ["high"]},
        "domain_id": "computer_monitoring",
        "domain_version": "1.0.0",
    }
    base.update(overrides)
    return make_idempotency_key(**base)


def test_identical_inputs_produce_identical_key():
    assert _key() == _key()


def test_key_is_sha256_hex():
    key = _key()
    assert len(key) == 64
    int(key, 16)  # 合法十六进制


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", 99),
        ("source_id", 99),
        ("domain_id", "other"),
        ("domain_version", "2.0.0"),
        ("filters", {"severity": ["low"]}),
    ],
)
def test_any_component_change_changes_the_key(field: str, value: object):
    assert _key(**{field: value}) != _key()


def test_different_time_range_changes_the_key():
    assert _key(time_end=T0 + timedelta(hours=2)) != _key()


def test_filter_key_order_does_not_change_the_key():
    """字典顺序不同必须得到同一个键 —— 否则幂等会静默失效。"""
    a = _key(filters={"severity": ["high"], "event_type": ["log"]})
    b = _key(filters={"event_type": ["log"], "severity": ["high"]})
    assert a == b


def test_key_includes_pipeline_version():
    """流水线版本变了，同一份输入应视为不同的分析（历史可复现靠它）。"""
    from app.analysis.idempotency import IdempotencyInputs

    base = IdempotencyInputs(
        project_id=1,
        source_id=2,
        time_range_start=T0.isoformat(),
        time_range_end=(T0 + timedelta(hours=1)).isoformat(),
        filters={},
        domain_id="d",
        domain_version="1.0.0",
        pipeline_version="1.0.0",
    )
    other = IdempotencyInputs(
        project_id=1,
        source_id=2,
        time_range_start=T0.isoformat(),
        time_range_end=(T0 + timedelta(hours=1)).isoformat(),
        filters={},
        domain_id="d",
        domain_version="1.0.0",
        pipeline_version="1.1.0",
    )
    assert compute_idempotency_key(base) != compute_idempotency_key(other)


def test_naive_time_is_refused_for_the_key():
    """含糊的时间会让"同一时间范围"算出不同键。"""
    # 刻意构造 naive datetime 以验证它被拒绝（DTZ001 在此是测试意图）
    naive = datetime(2026, 9, 23, 12, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="naive"):
        _key(time_start=naive)


def test_time_string_without_timezone_is_refused():
    with pytest.raises(ValueError, match="不含时区"):
        normalize_time_range("2026-09-23T12:00:00", "2026-09-23T13:00:00")


def test_time_is_normalized_to_utc():
    """带偏移的时间要换算成 UTC 再入键，否则同一时刻不同写法会得到不同键。"""
    a = normalize_time_range("2026-09-23T20:00:00+08:00", T0)
    b = normalize_time_range("2026-09-23T12:00:00+00:00", T0)
    assert a[0] == b[0]


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


# ============================================================
# 错误分类（计划第 712–717 行）
# ============================================================


def test_network_and_rate_limit_errors_are_retryable():
    assert classify_error(ModelUnavailableError("503")) == ErrorKind.RETRYABLE
    assert is_retryable(ModelUnavailableError("429 限流"))


@pytest.mark.parametrize(
    "exc,expected",
    [
        (InputFormatError("bad"), ErrorKind.INPUT),
        (AuthenticationError("401"), ErrorKind.AUTH),
        (BudgetExhaustedError("no money"), ErrorKind.BUDGET),
        (ValidationFailureError("bad schema"), ErrorKind.VALIDATION),
    ],
)
def test_non_retryable_categories(exc: BaseException, expected: str):
    """计划第 716 行：输入格式错误、认证失败、预算耗尽、校验失败都不可重试。"""
    assert classify_error(exc) == expected
    assert not is_retryable(exc)


def test_budget_stop_is_not_retryable():
    assert classify_error(StopExecution("call_limit", "到顶")) == ErrorKind.BUDGET
    assert not is_retryable(StopExecution("call_limit", "到顶"))


def test_structured_output_failure_is_retryable_so_the_ladder_can_run():
    """结构化输出失败必须**可重试**，否则降级链根本没机会生效。

    真机故障（2026-09-23）：L3 输出被 token 上限截断 → JSON 不完整。
    早先把它归为"校验失败不可重试"，于是 L3 一失败整条链路就直接退到
    纯规则报告（零结论），L3→L2→L1 的降级链完全没被用上。
    换等级/换预算是能解决截断的，所以它属于可重试。
    """
    assert classify_error(StructuredOutputError("no json")) == ErrorKind.RETRYABLE
    assert is_retryable(StructuredOutputError("no json"))


def test_input_validation_failure_remains_non_retryable():
    """真正不可重试的是**输入**类校验失败 —— 重试同一份坏输入毫无意义。"""
    assert not is_retryable(ValidationFailureError("bad input"))
    assert not is_retryable(InputFormatError("bad format"))


def test_unknown_error_is_not_treated_as_retryable():
    """未知错误盲目重试可能放大故障、也可能白花钱。"""
    assert classify_error(RuntimeError("???")) == ErrorKind.UNKNOWN
    assert not is_retryable(RuntimeError("???"))


# ============================================================
# 重试
# ============================================================


def test_retry_succeeds_after_transient_failures():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ModelUnavailableError("503")
        return "ok"

    outcome = call_with_retry(flaky, policy=RetryPolicy(max_retries=3))
    assert outcome.ok is True
    assert outcome.value == "ok"
    assert outcome.attempts == 3


def test_retry_stops_at_max_retries():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise ModelUnavailableError("503")

    outcome = call_with_retry(always_fail, policy=RetryPolicy(max_retries=3))
    assert outcome.ok is False
    # 首次 + 3 次重试 = 4 次
    assert calls["n"] == MAX_RETRIES + 1
    assert outcome.attempts == MAX_RETRIES + 1


def test_non_retryable_error_is_not_retried():
    calls = {"n": 0}

    def fail_fast():
        calls["n"] += 1
        raise InputFormatError("bad input")

    outcome = call_with_retry(fail_fast, policy=RetryPolicy(max_retries=3))
    assert outcome.ok is False
    assert calls["n"] == 1, "不可重试的错误只应尝试一次"
    assert outcome.history[0].error_kind == ErrorKind.INPUT


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(
        base_delay_seconds=1.0, max_delay_seconds=8.0, jitter_ratio=0.0
    )
    assert [policy.delay_for(i) for i in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_backoff_has_jitter_so_retries_do_not_synchronize():
    """没有抖动，同时失败的调用会在同一刻一起重试，把限流的服务再打一次。"""
    policy = RetryPolicy(base_delay_seconds=4.0, jitter_ratio=0.25)
    delays = {
        round(policy.delay_for(1, rng=random.Random(seed)), 4) for seed in range(20)
    }
    assert len(delays) > 1, "延迟没有抖动"
    assert all(3.0 <= d <= 5.0 for d in delays)


def test_retry_records_history_for_explaining_what_happened():
    def flaky():
        raise ModelUnavailableError("503")

    outcome = call_with_retry(flaky, policy=RetryPolicy(max_retries=2))
    assert len(outcome.history) == 3
    assert all(r.error_kind == ErrorKind.RETRYABLE for r in outcome.history)
    assert outcome.as_dict()["last_error"].startswith("ModelUnavailableError")


def test_retry_policy_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        RetryPolicy(max_retries=-1)


# ============================================================
# 降级链（计划第 719–726 行）
# ============================================================


@pytest.mark.parametrize(
    "start,expected",
    [("L3", ["L3", "L2", "L1"]), ("L2", ["L2", "L1"]), ("L1", ["L1"])],
)
def test_downgrade_ladder(start: str, expected: list[str]):
    assert downgrade_ladder(start) == expected


def test_downgrade_ladder_excludes_l0():
    """L0 不是"再试一次模型"，而是纯规则报告，语义不同。"""
    assert "L0" not in downgrade_ladder("L3")
    with pytest.raises(ValueError, match="不是可调用的模型等级"):
        downgrade_ladder("L0")


def test_fallback_uses_first_working_tier():
    tried: list[str] = []

    def attempt(tier: str) -> str:
        tried.append(tier)
        if tier == "L3":
            raise ModelUnavailableError("503")
        return f"ok-{tier}"

    outcome = call_with_fallback(
        attempt, start_tier="L3", retry_policy=RetryPolicy(max_retries=0)
    )
    assert outcome.ok is True
    assert outcome.tier_used == "L2"
    assert tried == ["L3", "L2"]
    assert outcome.used_rules_only is False


def test_all_tiers_failing_produces_rules_only_report():
    """计划第 724–725 行：仍失败 → 纯规则 L0 报告 + partial_success + model_unavailable。"""

    def rules_only() -> str:
        return "确定性结果"

    outcome = call_with_fallback(
        lambda tier: (_ for _ in ()).throw(ModelUnavailableError("503")),
        start_tier="L3",
        rules_only_fallback=rules_only,
        retry_policy=RetryPolicy(max_retries=0),
    )
    assert outcome.ok is True
    assert outcome.used_rules_only is True
    assert outcome.tier_used == "L0"
    assert outcome.stop_reason == "model_unavailable"
    assert outcome.value == "确定性结果"


def test_all_tiers_failing_without_rules_fallback_is_not_ok():
    outcome = call_with_fallback(
        lambda tier: (_ for _ in ()).throw(ModelUnavailableError("503")),
        start_tier="L1",
        retry_policy=RetryPolicy(max_retries=0),
    )
    assert outcome.ok is False
    assert outcome.used_rules_only is True


def test_non_retryable_error_aborts_the_ladder():
    """预算/认证类错误降级重试没意义，只是多花钱。"""
    tried: list[str] = []

    def attempt(tier: str) -> str:
        tried.append(tier)
        raise BudgetExhaustedError("预算耗尽")

    outcome = call_with_fallback(
        attempt, start_tier="L3", retry_policy=RetryPolicy(max_retries=2)
    )
    assert outcome.ok is False
    assert tried == ["L3"], f"不该继续降级，实际尝试了 {tried}"


def test_storage_failure_is_non_retryable_and_carries_its_own_kind():
    """落库失败必须单独成类，且**不许**继续降级。

    真机故障：结论因 CHECK 约束写不进去。若把它当"未知错误"，降级链会
    继续往 L2→L1 各再调一次模型 —— 每次产出的都是同一份写不进去的结论，
    三次模型钱全白花，而真正的原因（写库失败）被埋在最里层。
    它发生在"钱已经花完"之后，所以只能立刻收尾并如实报出来。
    """
    exc = StorageFailureError("结论落库失败：IntegrityError")
    assert classify_error(exc) == ErrorKind.STORAGE
    assert ErrorKind.STORAGE not in RETRYABLE_KINDS
    assert not is_retryable(exc)

    tried: list[str] = []

    def attempt(tier: str) -> str:
        tried.append(tier)
        raise StorageFailureError("写不进去")

    outcome = call_with_fallback(
        attempt, start_tier="L3", retry_policy=RetryPolicy(max_retries=2)
    )
    assert outcome.ok is False
    assert tried == ["L3"], f"落库失败不该再降级烧钱，实际尝试了 {tried}"
    # stop_reason 要是 storage 本身，而不是笼统的 model_unavailable ——
    # 否则排障方向会被带偏到"模型不可用"上去。
    assert outcome.stop_reason == ErrorKind.STORAGE


def test_fallback_outcome_is_serializable():
    import json

    outcome = call_with_fallback(
        lambda tier: (_ for _ in ()).throw(ModelUnavailableError("503")),
        start_tier="L2",
        retry_policy=RetryPolicy(max_retries=0),
    )
    json.dumps(outcome.as_dict())


# ============================================================
# 心跳、僵尸回收与取消
# ============================================================


def test_heartbeat_timeout_must_exceed_slowest_call():
    """计划第 706 行括号里那句是硬约束：阈值小于单次最慢调用会误杀正常 Run。"""
    with pytest.raises(ValueError, match="不大于单次最慢调用"):
        validate_heartbeat_timeout(60)
    validate_heartbeat_timeout(300)  # 不抛异常


def test_heartbeat_timeout_must_be_positive():
    with pytest.raises(ValueError, match="必须为正"):
        validate_heartbeat_timeout(0)


def _record(**overrides) -> HeartbeatRecord:
    base = {
        "run_id": 1,
        "project_id": 1,
        "status": AGENT_RUN_RUNNING,
        "last_heartbeat": T0,
        "started_at": T0,
        "cancel_requested": False,
    }
    base.update(overrides)
    return HeartbeatRecord(**base)


def test_stale_heartbeat_is_a_zombie():
    assert is_zombie(_record(), now=T0 + timedelta(seconds=301), timeout_seconds=300)


def test_fresh_heartbeat_is_not_a_zombie():
    assert not is_zombie(_record(), now=T0 + timedelta(seconds=10), timeout_seconds=300)


def test_exactly_at_timeout_is_not_yet_a_zombie():
    assert not is_zombie(
        _record(), now=T0 + timedelta(seconds=300), timeout_seconds=300
    )


def test_queued_run_without_heartbeat_is_not_a_zombie():
    """queued 还没被 Worker 接手，没有心跳是正常的；它由「排队超时」处理。"""
    assert not is_zombie(
        _record(status=AGENT_RUN_QUEUED, last_heartbeat=None, started_at=None),
        now=T0 + timedelta(days=1),
        timeout_seconds=300,
    )


def test_zombie_falls_back_to_started_at_when_no_heartbeat():
    assert is_zombie(
        _record(last_heartbeat=None),
        now=T0 + timedelta(seconds=400),
        timeout_seconds=300,
    )


def test_running_without_any_timestamp_is_treated_as_zombie():
    """数据异常时宁可回收成 timeout，也不要让它永远占着「运行中」。"""
    assert is_zombie(
        _record(last_heartbeat=None, started_at=None), now=T0, timeout_seconds=300
    )


def test_naive_heartbeat_is_handled():
    assert is_zombie(
        _record(last_heartbeat=datetime(2026, 9, 23, 12, 0)),  # noqa: DTZ001 - 刻意 naive，验证被容错处理
        now=T0 + timedelta(seconds=400),
        timeout_seconds=300,
    )


def test_heartbeat_deadline_is_reported():
    assert heartbeat_deadline(started_at=T0, timeout_seconds=300) == T0 + timedelta(
        seconds=300
    )


def test_cancel_check_raises_when_requested():
    with pytest.raises(CancelledError):
        check_cancelled(cancel_requested=True)
    check_cancelled(cancel_requested=False)  # 不抛


def test_should_abort_mirrors_cancel_flag():
    assert should_abort(cancel_requested=True) is True
    assert should_abort(cancel_requested=False) is False


def test_cancelled_error_is_not_classified_as_retryable():
    """取消不是错误，不该被重试逻辑当成失败来重试。"""
    assert classify_error(CancelledError()) == ErrorKind.UNKNOWN
    assert not is_retryable(CancelledError())


# ============================================================
# 回归（2026-09-24 真机核验）：僵尸回收必须挂在 beat 上
# ============================================================


def test_beat_schedule_reaps_zombie_runs():
    """beat 必须真的挂着回收任务，间隔取自配置。

    这条 schedule 此前**不存在**：`reclaim_zombies` 只有测试在调，
    beat 容器空转，于是"kill Worker 后僵尸 Run 被回收"在真机上从未发生过。
    """
    from app.celery_app import celery_app
    from app.config import get_settings

    entry = celery_app.conf.beat_schedule.get("reap-zombie-runs")
    assert entry is not None, "beat_schedule 里没有僵尸回收任务"
    assert entry["task"] == "app.tasks.maintenance.reap_zombie_runs"
    assert float(entry["schedule"]) == float(get_settings().zombie_reap_interval_seconds)
    assert "app.tasks.maintenance" in celery_app.conf.include, "任务模块必须被 worker 导入"
    # 扫描必须比心跳超时勤，否则"回收"最坏要等两倍超时
    assert float(entry["schedule"]) < DEFAULT_HEARTBEAT_TIMEOUT_SECONDS


def test_zombie_reaper_runs_on_its_own_queue():
    """回收任务必须走独立队列。

    第一版把它发到默认队列（与分析任务同一个），真机复验当场打脸：
    分析 Worker 被冻结后，回收任务跟着排队没人消费 —— 而"分析 Worker 死了"
    正是它唯一要处理的场景。救火队不能住在消防站里。
    """
    from app.celery_app import celery_app

    routes = celery_app.conf.task_routes or {}
    matched = [
        route
        for pattern, route in routes.items()
        if pattern.startswith("app.tasks.maintenance")
    ]
    assert matched, "维护任务没有单独路由，会和分折任务抢同一个被冻结的 worker"
    assert matched[0]["queue"] == "maintenance"
    assert celery_app.conf.task_default_queue != "maintenance", "分析任务不该挤进维护队列"
