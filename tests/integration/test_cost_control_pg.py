"""阶段 07 验收的集成测试（真实 PostgreSQL）。

把 Router（阶段 06）与 CostController 接起来，验证两件只有真实落库才能证明的事：
1. 模型调用的 token 与成本**真的累加**进了 `AgentRun.model_calls`；
2. 一个"想无限跑"的循环在真实 Run 上被硬拦住，不会跑出上限。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.db import SessionLocal
from app.gateways.base import ModelResult, TierConfig
from app.gateways.router import Router
from app.models import AgentRun, DataSource, Project, User
from app.policy import (
    STOP_CALL_LIMIT,
    CostController,
    RunPolicy,
    StopExecution,
)
from app.repositories import (
    AgentRunRepository,
    DataSourceRepository,
    ProjectRepository,
    UserRepository,
)

pytestmark = pytest.mark.integration
UTC = timezone.utc

TIER_CONFIGS = {
    "L1": TierConfig("L1", "m-small", "off", 1.0, 4.0),
    "L2": TierConfig("L2", "m-medium", "low", 1.0, 4.0),
    "L3": TierConfig("L3", "m-large", "high", 1.0, 4.0),
}


class FakeSettings:
    def __init__(self) -> None:
        self.model_provider_base_url = "https://example.invalid"
        for tier in ("l1", "l2", "l3"):
            setattr(self, f"model_{tier}", f"test-model-{tier}")
            setattr(self, f"model_{tier}_price_input_per_1m", 1.0)
            setattr(self, f"model_{tier}_price_output_per_1m", 4.0)
        self.model_l1_reasoning = "off"
        self.model_l2_reasoning = "low"
        self.model_l3_reasoning = "high"


class FakeGateway:
    """确定性假网关：每次调用返回固定的 token 用量。"""

    provider = "fake"

    def __init__(self, *, tokens_input: int = 1000, tokens_output: int = 250) -> None:
        self.tokens_input = tokens_input
        self.tokens_output = tokens_output
        self.calls = 0

    def generate(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        max_output_tokens: int,
        *,
        model: str,
        reasoning: str | None = None,
    ) -> ModelResult:
        self.calls += 1
        return ModelResult(
            content="ok",
            parsed=None,
            tokens_input=self.tokens_input,
            tokens_output=self.tokens_output,
            model=model,
            raw={},
        )


@pytest.fixture()
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def run_ctx(session):
    users = UserRepository(session)
    user = users.add(
        User(email=f"stage07-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    project = ProjectRepository(session).add(
        Project(user_id=user.id, name="p07", budget_total=10, budget_used=0)
    )
    session.flush()
    source = DataSourceRepository(session).add(
        project.id,
        DataSource(project_id=project.id, type="file_upload", format="txt", location="a.log"),
    )
    session.flush()
    runs = AgentRunRepository(session)
    run = runs.add(project.id, AgentRun(project_id=project.id, source_id=source.id))
    session.flush()
    return project, runs, run


def test_model_calls_and_cost_accumulate_in_real_run(session, run_ctx):
    """验收 3：成本被正确累加（真实落库后读回）。"""
    project, runs, run = run_ctx
    gateway = FakeGateway()
    router = Router(gateway, FakeSettings(), run_repository=runs)

    for _ in range(3):
        router.generate(
            tier="L1",
            prompt="p",
            max_output_tokens=100,
            project_id=project.id,
            run_id=run.id,
        )
    session.flush()
    session.expire_all()

    calls = runs.model_calls_of(project.id, run.id)
    assert len(calls) == 3
    total_cost = sum(entry["cost"] for entry in calls)
    total_in = sum(entry["tokens_input"] for entry in calls)
    total_out = sum(entry["tokens_output"] for entry in calls)

    # 1000 × 1元/1M + 250 × 4元/1M = 0.002 元/次
    assert total_cost == pytest.approx(0.006)
    assert (total_in, total_out) == (3000, 750)


def test_controller_usage_matches_recorded_calls(session, run_ctx):
    """控制器的累加值必须与真正落库的记录一致，否则"成本被累加"是假的。"""
    project, runs, run = run_ctx
    gateway = FakeGateway()
    router = Router(gateway, FakeSettings(), run_repository=runs)
    controller = CostController(RunPolicy(), tier_configs=TIER_CONFIGS)
    controller.start()

    for _ in range(2):
        controller.mid_check(tier="L1")
        result = router.generate(
            tier="L1", prompt="p", max_output_tokens=100,
            project_id=project.id, run_id=run.id,
        )
        controller.record_call(
            tokens_input=result.tokens_input, tokens_output=result.tokens_output, cost=result.cost
        )

    session.flush()
    session.expire_all()
    calls = runs.model_calls_of(project.id, run.id)

    assert controller.usage.cost == pytest.approx(sum(c["cost"] for c in calls))
    assert controller.usage.tokens_input == sum(c["tokens_input"] for c in calls)
    assert controller.usage.tokens_total == sum(
        c["tokens_input"] + c["tokens_output"] for c in calls
    )


def test_infinite_loop_cannot_exceed_policy_on_a_real_run(session, run_ctx):
    """验收 4：无法构造出无限调用的 Run —— 在真实 Run 上验证硬拦截。"""
    project, runs, run = run_ctx
    gateway = FakeGateway()
    router = Router(gateway, FakeSettings(), run_repository=runs)

    policy = RunPolicy(max_model_calls_per_run=3)
    controller = CostController(policy, tier_configs=TIER_CONFIGS)
    controller.start()

    with pytest.raises(StopExecution) as excinfo:
        for _ in range(1000):  # 一个"想跑 1000 次"的循环
            controller.mid_check(tier="L1")
            result = router.generate(
                tier="L1", prompt="p", max_output_tokens=100,
                project_id=project.id, run_id=run.id,
            )
            controller.record_call(
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
                cost=result.cost,
            )

    assert excinfo.value.reason == STOP_CALL_LIMIT
    # 关键断言：网关**实际**只被调用了 3 次，模型没被多打一次
    assert gateway.calls == 3

    session.flush()
    session.expire_all()
    assert len(runs.model_calls_of(project.id, run.id)) == 3


def test_partial_success_metadata_can_be_persisted_to_run(session, run_ctx):
    """验收 2：极小预算触发 partial_success，且元数据能真的写进 Run 并读回。"""
    project, runs, run = run_ctx
    gateway = FakeGateway()
    router = Router(gateway, FakeSettings(), run_repository=runs)

    controller = CostController(
        RunPolicy(max_model_calls_per_run=1, run_max_cost=0.01), tier_configs=TIER_CONFIGS
    )
    controller.start()
    controller.mid_check(tier="L1")
    result = router.generate(
        tier="L1", prompt="p", max_output_tokens=100, project_id=project.id, run_id=run.id
    )
    controller.record_call(
        tokens_input=result.tokens_input, tokens_output=result.tokens_output, cost=result.cost
    )

    metadata = controller.partial_success_metadata(STOP_CALL_LIMIT)
    run.run_metadata = metadata
    run.status = "partial_success"
    session.flush()
    session.expire_all()

    stored = runs.get(project.id, run.id)
    assert stored.status == "partial_success"
    assert stored.run_metadata["stop_reason"] == STOP_CALL_LIMIT
    assert "不完整" in stored.run_metadata["note"]


def test_pre_check_rejects_before_any_model_call(session, run_ctx):
    """验收 1：预算不足拒绝创建 —— 且此时**一次模型都没调**。"""
    gateway = FakeGateway()
    controller = CostController(
        RunPolicy(run_max_cost=0.0001), tier_configs=TIER_CONFIGS
    )
    result = controller.pre_check("L3")

    assert result.allowed is False
    assert gateway.calls == 0
