"""Policy 与 Cost Controller（计划第 625–675 行）。

    policy.py          RunPolicy（配置对象，可按 Project 覆盖）、stop_reason 常量
    cost_controller.py 成本估算 + 三个检查点（Pre / Mid / Post）
    store.py           项目级策略覆盖（V1 不建表）

价格与型号**都不写死**（计划第 671–672 行），全部从配置传入。
"""

from __future__ import annotations

from app.policy.cost_controller import (
    CONTEXT_BUDGET_BY_TIER,
    DEFAULT_PLANNED_CALLS,
    OUTPUT_RATIO,
    BudgetExceededError,
    CostController,
    CostEstimate,
    PreCheckResult,
    RunUsage,
    StopExecution,
    estimate_run_cost,
)
from app.policy.policy import (
    DEFAULT_POLICY,
    SAFETY_MARGIN,
    STOP_BUDGET_EXCEEDED,
    STOP_CALL_LIMIT,
    STOP_CANCELLED,
    STOP_MODEL_UNAVAILABLE,
    STOP_REASONS,
    STOP_RUNTIME_LIMIT,
    STOP_TOKEN_LIMIT,
    PolicyError,
    PolicyOverrides,
    RunPolicy,
)
from app.policy.store import (
    ProjectPolicyStore,
    get_policy_store,
    reset_policy_store,
)

__all__ = [
    "CONTEXT_BUDGET_BY_TIER",
    "DEFAULT_PLANNED_CALLS",
    "DEFAULT_POLICY",
    "OUTPUT_RATIO",
    "SAFETY_MARGIN",
    "STOP_BUDGET_EXCEEDED",
    "STOP_CALL_LIMIT",
    "STOP_CANCELLED",
    "STOP_MODEL_UNAVAILABLE",
    "STOP_REASONS",
    "STOP_RUNTIME_LIMIT",
    "STOP_TOKEN_LIMIT",
    "BudgetExceededError",
    "CostController",
    "CostEstimate",
    "PolicyError",
    "PolicyOverrides",
    "PreCheckResult",
    "ProjectPolicyStore",
    "RunPolicy",
    "RunUsage",
    "StopExecution",
    "estimate_run_cost",
    "get_policy_store",
    "reset_policy_store",
]
