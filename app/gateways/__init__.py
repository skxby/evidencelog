"""Model Gateway 层：业务层只调 `generate()`，不知道供应商。

分工：
    base.py        抽象契约、等级常量、ModelResult/TierConfig
    deepseek.py    DeepSeek（OpenAI 兼容端点）实现
    credentials.py 凭据解析（环境变量 > DSH 凭据文件）
    router.py      等级 → 型号的解析与调用编排（型号只在这里出现）
"""

from __future__ import annotations

from app.gateways.base import (
    MODEL_TIERS,
    TIERS,
    VALID_REASONING_EFFORTS,
    ConfigurationError,
    GatewayError,
    L0DoesNotCallModelError,
    ModelGateway,
    ModelResult,
    ModelUnavailableError,
    StructuredOutputError,
    TierConfig,
)
from app.gateways.deepseek import DeepSeekGateway, parse_json_content
from app.gateways.router import Router, build_router, read_tier_configs

__all__ = [
    "MODEL_TIERS",
    "TIERS",
    "VALID_REASONING_EFFORTS",
    "ConfigurationError",
    "DeepSeekGateway",
    "GatewayError",
    "L0DoesNotCallModelError",
    "ModelGateway",
    "ModelResult",
    "ModelUnavailableError",
    "Router",
    "StructuredOutputError",
    "TierConfig",
    "build_router",
    "parse_json_content",
    "read_tier_configs",
]
