"""成本核算与三个检查点的判定逻辑（计划第 639–673 行）。

```text
单次成本 = 输入token × 输入单价 + 输出token × 输出单价
```

**价格不写死**（计划第 671–672 行：模型迭代快，不要把价格或型号写死在计划里），
全部从配置传入。

三个检查点的职责边界：
    Pre-check   创建 Run 时：**估算**（不是按原始事件数线性估）
    Mid-check   每次模型调用前：对照**实际**已用量判断是否该停
    Post-check  Run 结束时：汇总实际用量并累加回 Project

`Pre-check` 按「蒸馏后 Context 预算」估算而不是事件条数——因为事件数 10 倍
不等于 token 10 倍（采样与压缩会削平），按条数估会严重高估并误拒。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.gateways.base import MODEL_TIERS, TierConfig
from app.policy.policy import (
    SAFETY_MARGIN,
    STOP_BUDGET_EXCEEDED,
    STOP_CALL_LIMIT,
    STOP_RUNTIME_LIMIT,
    STOP_TOKEN_LIMIT,
    RunPolicy,
)

#: 各等级的 Context token 预算（计划第 795–799 行表：总预算）
#: L1=2000 / L2=4000 / L3=8000。**不要按事件条数估**。
CONTEXT_BUDGET_BY_TIER: dict[str, int] = {
    "L1": 2_000,
    "L2": 4_000,
    "L3": 8_000,
}

#: 输出 token 相对输入的经验比例（计划第 111 行的成本预估用 25%）
OUTPUT_RATIO = 0.25

#: 多错误归纳 / 升级场景下可能叠加多次调用，Pre-check 按这个次数预留
DEFAULT_PLANNED_CALLS = 1


class BudgetExceededError(RuntimeError):
    """预算/额度不足 —— 用于**拒绝创建** Run（Pre-check）。

    与 `StopExecution` 的区别：这个发生在开始之前（拒绝），
    那个发生在执行之中（中断并返回部分结果）。
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class StopExecution(RuntimeError):
    """执行中到达上限 —— 调用方应把 Run 置为 `partial_success` 并返回已完成部分。

    计划第 648 行：停止昂贵步骤，状态置 partial_success，返回已完成部分。
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class CostEstimate:
    """Pre-check 的估算结果。"""

    tier: str
    planned_calls: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost: float
    #: 含安全边际后的成本
    estimated_cost_with_margin: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "planned_calls": self.planned_calls,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "estimated_cost": round(self.estimated_cost, 6),
            "estimated_cost_with_margin": round(self.estimated_cost_with_margin, 6),
        }


def estimate_run_cost(
    tier: str,
    tier_config: TierConfig,
    *,
    planned_calls: int = DEFAULT_PLANNED_CALLS,
    peak: bool = False,
) -> CostEstimate:
    """按**蒸馏后 Context 预算**估算一次 Run 的成本。

    刻意不接收事件条数：按条数线性估会严重高估（采样与压缩会削平），
    从而把本来能跑的 Run 误拒。

    `peak` 必须与**实际计费侧**同一口径（`Router.generate` 用的是
    `is_peak_time(now)`）：漏传的话，高峰时段估值按闲时价算，
    比真实账单低一倍 —— 预算是"钱够不够"的闸门，低估的方向恰好是该拦不拦。
    真机核验（2026-09-24）：高峰时估算 ¥0.0176 vs 同 token 实际计费 ¥0.032，低估 1.82 倍。
    """
    normalized = tier.upper()
    if normalized not in CONTEXT_BUDGET_BY_TIER:
        raise ValueError(f"未知或不可调用的等级 {tier!r}，只接受 {MODEL_TIERS}")
    if planned_calls < 1:
        raise ValueError(f"planned_calls 必须 ≥ 1，收到 {planned_calls}")

    per_call_input = CONTEXT_BUDGET_BY_TIER[normalized]
    per_call_output = int(per_call_input * OUTPUT_RATIO)

    tokens_input = per_call_input * planned_calls
    tokens_output = per_call_output * planned_calls
    cost = tier_config.estimate_cost(tokens_input, tokens_output, peak=peak)

    return CostEstimate(
        tier=normalized,
        planned_calls=planned_calls,
        estimated_input_tokens=tokens_input,
        estimated_output_tokens=tokens_output,
        estimated_cost=cost,
        estimated_cost_with_margin=cost * (1 + SAFETY_MARGIN),
    )


@dataclass
class RunUsage:
    """一次 Run 的**实际**用量累加器。"""

    calls: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost: float = 0.0
    elapsed_seconds: float = 0.0
    #: 最近一次被记录的中断原因
    stop_reason: str | None = None
    completed_phases: list[str] = field(default_factory=list)
    skipped_phases: list[str] = field(default_factory=list)

    @property
    def tokens_total(self) -> int:
        return self.tokens_input + self.tokens_output

    def record_call(self, *, tokens_input: int, tokens_output: int, cost: float) -> None:
        self.calls += 1
        self.tokens_input += tokens_input
        self.tokens_output += tokens_output
        self.cost += cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tokens_total": self.tokens_total,
            "cost": round(self.cost, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stop_reason": self.stop_reason,
            "completed_phases": list(self.completed_phases),
            "skipped_phases": list(self.skipped_phases),
        }


@dataclass
class PreCheckResult:
    """Pre-check 的判定。`allowed=False` 时 `reason` 说明为什么拒绝。"""

    allowed: bool
    reason: str | None = None
    message: str = ""
    estimate: CostEstimate | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "allowed": self.allowed,
            "estimate": self.estimate.as_dict() if self.estimate else None,
        }
        if not self.allowed:
            payload["reason"] = self.reason
            payload["message"] = self.message
        return payload


class CostController:
    """把策略与实际用量对起来，产出三个检查点的判定。"""

    def __init__(
        self,
        policy: RunPolicy,
        *,
        tier_configs: dict[str, TierConfig],
        project_budget_total: float | None = None,
        project_budget_used: float = 0.0,
        month_spent: float = 0.0,
        peak_now: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy
        self.tier_configs = tier_configs
        self.project_budget_total = project_budget_total
        self.project_budget_used = project_budget_used
        self.month_spent = month_spent
        #: 此刻是否处于供应商高峰时段。Pre-check 的估算是"现在要花多少"，
        #: 所以必须和计费侧（`Router._is_peak_now`）用同一个判断 ——
        #: 由组合根（`build_cost_controller`）从配置时区算好传进来。
        self.peak_now = bool(peak_now)
        self.usage = RunUsage()
        self._clock = clock or time.monotonic
        #: Run 的开始时刻；None 表示尚未开始计时
        self._started_at: float | None = None

    # ---------- 计时 ----------

    def start(self) -> None:
        """标记 Run 开始计时。

        没有这一步，`elapsed_seconds` 永远是 0，运行时长上限形同虚设 ——
        那正是「无限调用」能溜过去的一种方式。
        """
        self._started_at = self._clock()

    def elapsed_seconds(self) -> float:
        """自 `start()` 起的秒数；未开始时为 0。"""
        if self._started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._started_at)

    def _sync_elapsed(self) -> float:
        elapsed = self.elapsed_seconds()
        self.usage.elapsed_seconds = elapsed
        return elapsed

    # ---------- Pre-check（创建 Run 时）----------

    def pre_check(self, tier: str, *, planned_calls: int = DEFAULT_PLANNED_CALLS) -> PreCheckResult:
        """创建前估算。不足则**拒绝创建**（计划第 644 行）。"""
        normalized = tier.upper()

        if not self.policy.allows(normalized):
            return PreCheckResult(
                allowed=False,
                reason=STOP_BUDGET_EXCEEDED,
                message=(
                    f"等级 {normalized} 不在 allowed_tiers="
                    f"{list(self.policy.allowed_tiers)} 内"
                ),
            )

        config = self.tier_configs.get(normalized)
        if config is None:
            return PreCheckResult(
                allowed=False,
                reason=STOP_BUDGET_EXCEEDED,
                message=f"等级 {normalized} 没有配置型号与单价",
            )

        if planned_calls > self.policy.max_model_calls_per_run:
            return PreCheckResult(
                allowed=False,
                reason=STOP_CALL_LIMIT,
                message=(
                    f"预计调用 {planned_calls} 次超过上限 "
                    f"{self.policy.max_model_calls_per_run}"
                ),
            )

        estimate = estimate_run_cost(
            normalized, config, planned_calls=planned_calls, peak=self.peak_now
        )
        cost_with_margin = estimate.estimated_cost_with_margin

        if cost_with_margin > self.policy.run_max_cost:
            return PreCheckResult(
                allowed=False,
                reason=STOP_BUDGET_EXCEEDED,
                message=(
                    f"预计成本 ¥{cost_with_margin:.4f}（含 "
                    f"{int(SAFETY_MARGIN * 100)}% 安全边际）超过单次上限 "
                    f"¥{self.policy.run_max_cost:.4f}"
                ),
                estimate=estimate,
            )

        if self.project_budget_total is not None:
            remaining = self.project_budget_total - self.project_budget_used
            if cost_with_margin > remaining:
                return PreCheckResult(
                    allowed=False,
                    reason=STOP_BUDGET_EXCEEDED,
                    message=(
                        f"项目剩余预算 ¥{remaining:.4f} 不足以覆盖预计成本 "
                        f"¥{cost_with_margin:.4f}"
                    ),
                    estimate=estimate,
                )

        # 月度预算（计划第 644 行「月度预算或 Run 预算不足 → 拒绝创建」）。
        # 这一条此前完全没有实现：`monthly_budget` 在 Settings 与 RunPolicy 里
        # 都声明了、README 也把它写成"预算上限"，但没有任何地方比过它。
        if self.policy.monthly_budget > 0:
            month_remaining = self.policy.monthly_budget - self.month_spent
            if cost_with_margin > month_remaining:
                return PreCheckResult(
                    allowed=False,
                    reason=STOP_BUDGET_EXCEEDED,
                    message=(
                        f"本月已花 ¥{self.month_spent:.4f}，月度预算 "
                        f"¥{self.policy.monthly_budget:.4f} 剩余 ¥{month_remaining:.4f} "
                        f"不足以覆盖预计成本 ¥{cost_with_margin:.4f}"
                    ),
                    estimate=estimate,
                )

        if estimate.estimated_input_tokens + estimate.estimated_output_tokens > (
            self.policy.max_tokens_per_run
        ):
            return PreCheckResult(
                allowed=False,
                reason=STOP_TOKEN_LIMIT,
                message=(
                    f"预计 token {estimate.estimated_input_tokens + estimate.estimated_output_tokens} "
                    f"超过上限 {self.policy.max_tokens_per_run}"
                ),
                estimate=estimate,
            )

        return PreCheckResult(allowed=True, estimate=estimate)

    # ---------- Mid-check（每次模型调用前）----------

    def mid_check(self, *, tier: str | None = None) -> None:
        """调用前检查；到顶则抛 `StopExecution`（计划第 646–648 行）。

        注意检查的是**已用量**，不是估算量 —— 到顶立刻停，不再往下跑。
        """
        policy = self.policy
        usage = self.usage
        elapsed = self._sync_elapsed()

        if usage.calls >= policy.max_model_calls_per_run:
            raise StopExecution(
                STOP_CALL_LIMIT,
                f"已达到单次 Run 的调用上限 {policy.max_model_calls_per_run} 次",
            )

        if usage.tokens_total >= policy.max_tokens_per_run:
            raise StopExecution(
                STOP_TOKEN_LIMIT,
                f"已用 token {usage.tokens_total} 达到上限 {policy.max_tokens_per_run}",
            )

        if elapsed >= policy.max_runtime_seconds:
            raise StopExecution(
                STOP_RUNTIME_LIMIT,
                f"已运行 {elapsed:.1f}s 达到上限 {policy.max_runtime_seconds}s",
            )

        if usage.cost >= policy.run_max_cost:
            raise StopExecution(
                STOP_BUDGET_EXCEEDED,
                f"已花成本 ¥{usage.cost:.4f} 达到单次上限 ¥{policy.run_max_cost:.4f}",
            )

        if tier is not None and not policy.allows(tier):
            raise StopExecution(
                STOP_BUDGET_EXCEEDED,
                f"等级 {tier.upper()} 不在 allowed_tiers={list(policy.allowed_tiers)} 内",
            )

    def check_timeout(self) -> None:
        """按当前时钟判定是否超时；超时抛 `StopExecution`。"""
        elapsed = self._sync_elapsed()
        if elapsed >= self.policy.max_runtime_seconds:
            raise StopExecution(
                STOP_RUNTIME_LIMIT,
                f"已运行 {elapsed:.1f}s 达到上限 {self.policy.max_runtime_seconds}s",
            )

    # ---------- 记录与 Post-check ----------

    def record_call(self, *, tokens_input: int, tokens_output: int, cost: float) -> None:
        """记录一次**实际**调用用量。"""
        self.usage.record_call(tokens_input=tokens_input, tokens_output=tokens_output, cost=cost)

    def post_check(self) -> dict[str, Any]:
        """Run 结束时汇总（计划第 650–652 行）。

        返回汇总结果；调用方据它把 token/成本/调用次数累加回 `Project.budget_used`
        并写入模型调用记录。本方法不自己写库——写库要有 project 边界，
        那是 repository 的职责。
        """
        return self.usage.as_dict()

    def partial_success_metadata(self, reason: str, *, note: str = "") -> dict[str, Any]:
        """构造中断时的 `run_metadata`（计划第 658–664 行）。

        措辞上必须**明确标注不完整**（验收要求页面能标注「不完整」）。

        **接线状态（2026-09-24 真机核验后补记）**：目前**没有生产调用方** ——
        中断时的 `run_metadata` 由执行器的 `_metadata()` 从降级结果构造
        （它还要并进 `attempts` 这类执行器才有的事实）。
        保留这个方法而不是删掉，是因为它把"中断元数据该长什么样"写成了可执行的
        契约（`completed_phases` / `skipped_phases` / `note` 三项），
        单测也钉着它；两条路径的字段目前一致。
        真要把执行器那条也切过来，得先把 `attempts` 并进去 —— 那是下一次的事，
        不为了"看起来接上了"而制造第二个真源。

        注意组装顺序：`usage.as_dict()` 里也有一个 `stop_reason`（初始为 None），
        若把它展开在后会**覆盖掉调用方传入的 reason**，让中断原因变成 null ——
        那正是"结果看起来完整、其实没有原因"的假正常。故显式 reason 必须放最后。
        """
        default_note = "分析因预算/时限/模型不可用中断，结果可能不完整"
        metadata = {
            **self.usage.as_dict(),
            "completed_phases": list(self.usage.completed_phases),
            "skipped_phases": list(self.usage.skipped_phases),
            "note": note or default_note,
        }
        # 显式 reason 覆盖 usage 里的值，并在 usage 上也同步，保持一致
        self.usage.stop_reason = reason
        metadata["stop_reason"] = reason
        return metadata
