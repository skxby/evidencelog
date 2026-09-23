"""Run 策略（计划第 627–637 行）。

策略是**配置对象**，可按 Project 覆盖（计划第 629 行）；不建表
（计划第 356 行：Policy 不建表，Run 创建时把当时策略快照存入 run_metadata）。

字段与计划第 631–637 行一一对应。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.gateways.base import MODEL_TIERS, TIERS

#: stop_reason 的合法取值（计划第 660 行）
STOP_BUDGET_EXCEEDED = "budget_exceeded"
STOP_TOKEN_LIMIT = "token_limit"
STOP_RUNTIME_LIMIT = "runtime_limit"
STOP_MODEL_UNAVAILABLE = "model_unavailable"
STOP_CALL_LIMIT = "call_limit"
STOP_CANCELLED = "cancelled"

STOP_REASONS: tuple[str, ...] = (
    STOP_BUDGET_EXCEEDED,
    STOP_TOKEN_LIMIT,
    STOP_RUNTIME_LIMIT,
    STOP_MODEL_UNAVAILABLE,
    STOP_CALL_LIMIT,
    STOP_CANCELLED,
)

#: 预检的安全边际（计划第 644 行：叠加 10% 安全边际）
SAFETY_MARGIN = 0.10


class PolicyError(ValueError):
    """策略配置不合法。"""


@dataclass(frozen=True)
class RunPolicy:
    """一次 Run 的策略。"""

    max_model_calls_per_run: int = 5
    max_tokens_per_run: int = 12_000
    max_runtime_seconds: int = 180
    allowed_tiers: tuple[str, ...] = MODEL_TIERS
    run_max_cost: float = 0.30
    #: 月度预算（元）；来自配置，用于 Pre-check 判断整体是否还有余量
    monthly_budget: float = 10.0

    def __post_init__(self) -> None:
        if self.max_model_calls_per_run <= 0:
            raise PolicyError(
                f"max_model_calls_per_run 必须为正，收到 {self.max_model_calls_per_run}；"
                "它是「禁止无限调用」的主要闸门，不允许为 0 或负数"
            )
        if self.max_tokens_per_run <= 0:
            raise PolicyError(f"max_tokens_per_run 必须为正，收到 {self.max_tokens_per_run}")
        if self.max_runtime_seconds <= 0:
            raise PolicyError(
                f"max_runtime_seconds 必须为正，收到 {self.max_runtime_seconds}"
            )
        if self.run_max_cost < 0:
            raise PolicyError(f"run_max_cost 不能为负，收到 {self.run_max_cost}")
        if not self.allowed_tiers:
            raise PolicyError("allowed_tiers 不能为空；至少允许一个会调用模型的等级")

        unknown = [t for t in self.allowed_tiers if t not in TIERS]
        if unknown:
            raise PolicyError(f"allowed_tiers 含未知等级 {unknown}，只接受 {TIERS}")
        if "L0" in self.allowed_tiers:
            # L0 不调用模型，把它列进"允许的模型等级"是概念混淆
            raise PolicyError(
                "allowed_tiers 不应包含 L0（L0 是代码/规则等级，不调用模型）"
            )

    def allows(self, tier: str) -> bool:
        return tier.upper() in {t.upper() for t in self.allowed_tiers}

    def snapshot(self) -> dict[str, Any]:
        """策略快照，写进 `run_metadata`（计划第 356 行）。

        必须快照的原因：策略可按 Project 覆盖且会变，不记下当时的取值，
        历史 Run 的中断原因就无法解释。
        """
        return {
            "max_model_calls_per_run": self.max_model_calls_per_run,
            "max_tokens_per_run": self.max_tokens_per_run,
            "max_runtime_seconds": self.max_runtime_seconds,
            "allowed_tiers": list(self.allowed_tiers),
            "run_max_cost": self.run_max_cost,
        }


#: 默认策略。刻意保守：宁可先拒绝，也不要跑出意外账单。
DEFAULT_POLICY = RunPolicy()


@dataclass
class PolicyOverrides:
    """按 Project 覆盖策略（计划第 629 行）。只存被显式设置的字段。"""

    values: dict[str, Any] = field(default_factory=dict)

    def apply(self, base: RunPolicy) -> RunPolicy:
        if not self.values:
            return base
        unknown = set(self.values) - set(base.__dataclass_fields__)
        if unknown:
            raise PolicyError(f"未知的策略字段 {sorted(unknown)}")
        merged = {**{f: getattr(base, f) for f in base.__dataclass_fields__}, **self.values}
        if isinstance(merged.get("allowed_tiers"), list):
            merged["allowed_tiers"] = tuple(merged["allowed_tiers"])
        return RunPolicy(**merged)
