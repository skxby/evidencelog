"""Model Gateway 抽象（计划第 605–619 行）。

业务层只调用 `generate()`，**不知道供应商**；供应商差异在各自实现里消化。

三个必须守住的边界：
1. **代码中不出现具体型号**（计划第 616 行）——型号只从配置读入；
2. 各供应商对「结构化输出 / function calling」的支持方式不同，差异在本层消化，
   对上只暴露统一接口（计划第 619 行）；
3. 每次调用返回的**实际** token 用量要能被记录（计划第 617 行）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

#: 模型等级。L0 是「代码 / 规则，不调用模型」（计划第 599 行）。
TIER_L0 = "L0"
TIER_L1 = "L1"
TIER_L2 = "L2"
TIER_L3 = "L3"

TIERS: tuple[str, ...] = (TIER_L0, TIER_L1, TIER_L2, TIER_L3)
#: 真正会调用模型的等级
MODEL_TIERS: tuple[str, ...] = (TIER_L1, TIER_L2, TIER_L3)

#: 推理档位的合法取值。**只有这四个**——DeepSeek 官方 API 与 DSH 适配器
#: 都只接受 off / low / high / max。`medium` 不是合法值，历史文档里的
#: medium 映射已作废（见 .env 注释与《模型与Key配置方案》）。
VALID_REASONING_EFFORTS: tuple[str, ...] = ("off", "low", "high", "max")


class GatewayError(RuntimeError):
    """Gateway 层错误的基类。"""


class ModelUnavailableError(GatewayError):
    """模型不可用（网络 / 限流 / 服务端错误）。

    调用方（阶段 08 的重试与降级链）需要把它与「输入错误」区分开：
    前者可重试，后者重试无意义。
    """


class StructuredOutputError(GatewayError):
    """要求结构化输出但拿不到合法 JSON。

    **不静默降级成纯文本**：那会让下游拿到一个"看起来有结构"的字符串。
    """


class L0DoesNotCallModelError(GatewayError):
    """L0 是代码/规则等级，不该发起模型调用（计划第 599 行）。"""


class ConfigurationError(GatewayError):
    """配置缺失或不合法。启动时就该暴露，而不是等第一次调用。"""


@dataclass(frozen=True)
class TierConfig:
    """一个等级的配置（型号 + 档位 + 单价）。单价单位：元 / 每百万 token。"""

    tier: str
    model: str
    reasoning: str
    price_input_per_1m: float
    price_output_per_1m: float

    def estimate_cost(self, tokens_input: int, tokens_output: int) -> float:
        """按 token 用量估算成本（元）。

        这是**估算**：不含缓存命中折扣与峰谷差异。阶段 07 的成本控制会在此基础上
        区分命中/未命中与峰谷；这里只保证「有数可记」。
        """
        return (
            tokens_input / 1_000_000 * self.price_input_per_1m
            + tokens_output / 1_000_000 * self.price_output_per_1m
        )


@dataclass
class ModelResult:
    """一次模型调用的结果。

    字段与计划第 612 行的注释一致：`content, parsed, tokens_input, tokens_output,
    model, raw`。额外带 `tier` 与 `cost`，便于**即时记录**到 `AgentRun.model_calls`。
    """

    content: str
    parsed: dict[str, Any] | None
    tokens_input: int
    tokens_output: int
    model: str
    raw: dict[str, Any] = field(default_factory=dict)
    tier: str = ""
    #: 本次估算成本（元）
    cost: float = 0.0
    #: 供应商返回的思考 token 数（DeepSeek 会带）
    reasoning_tokens: int = 0
    #: 缓存命中的输入 token 数（成本差异可达 50 倍，阶段 07 要用）
    cached_tokens: int = 0

    def as_model_call(self, *, call_id: str, status: str = "ok") -> dict[str, Any]:
        """转成写进 `AgentRun.model_calls` 的一条记录。

        计划第 352 行约定的字段：`call_id, tier, model, timestamp, tokens_input,
        tokens_output, cost, status`。
        """
        from datetime import datetime, timezone

        return {
            "call_id": call_id,
            "tier": self.tier,
            "model": self.model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "cost": self.cost,
            "status": status,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


class ModelGateway(ABC):
    """供应商网关契约（计划第 608–612 行照抄）。"""

    #: 供应商标识，仅用于日志与记录，不参与业务判断
    provider: str

    @abstractmethod
    def generate(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        max_output_tokens: int,
        *,
        model: str,
        reasoning: str | None = None,
    ) -> ModelResult:
        """调用模型。

        `schema` 非空时要求结构化输出（JSON）；拿不到合法 JSON 必须抛
        `StructuredOutputError`，**不得退化成纯文本**。
        """


def requests_structured_output(schema: dict[str, Any] | None) -> bool:
    return schema is not None
