"""Router：等级 → 型号的解析与调用编排（计划第 594–617 行）。

**业务层只调 `generate(tier=...)`，只认 L0–L3；型号只在这里从配置解析**
（红线 2）。本模块代码里不出现任何型号字面量 —— 有单测钉死这一点。

L0 是「代码 / 规则，不调用模型」（计划第 599 行），故 Router 对 L0 直接
拒绝而不是回退到 L1：静默回退会让"本应零成本"的路径产生真实费用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.gateways.base import (
    MODEL_TIERS,
    TIER_L0,
    TIERS,
    VALID_REASONING_EFFORTS,
    ConfigurationError,
    L0DoesNotCallModelError,
    ModelGateway,
    ModelResult,
    TierConfig,
)


def _tier_field(obj: Any, name: str) -> Any:
    """从 Settings 对象或 dict 里取字段，便于测试注入。"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def read_tier_configs(settings: Any) -> dict[str, TierConfig]:
    """从配置读出 L1/L2/L3 的型号、档位与单价。

    缺型号即报错：这是**启动期**就该暴露的配置问题，不该等第一次调用
    （那时错误会伪装成"模型不可用"，让人往网络方向排查）。
    """
    configs: dict[str, TierConfig] = {}
    # 缓存命中价与峰谷倍数都是**全局**一项（.env 里只有
    # MODEL_CACHE_HIT_INPUT_PRICE_PER_1M 与 MODEL_PEAK_PRICE_MULTIPLIER，
    # 没有分等级的版本），故三个等级共用。
    cache_hit_price = float(_tier_field(settings, "model_cache_hit_input_price_per_1m") or 0.0)
    peak_multiplier = float(_tier_field(settings, "model_peak_price_multiplier") or 1.0)
    for tier in MODEL_TIERS:
        lowered = tier.lower()
        model = _tier_field(settings, f"model_{lowered}") or ""
        reasoning = (_tier_field(settings, f"model_{lowered}_reasoning") or "").strip()
        price_in = float(_tier_field(settings, f"model_{lowered}_price_input_per_1m") or 0.0)
        price_out = float(_tier_field(settings, f"model_{lowered}_price_output_per_1m") or 0.0)

        if not model:
            raise ConfigurationError(
                f"{tier} 未配置型号：请在 .env 里设置 MODEL_{tier}=<具体型号>"
            )
        if reasoning and reasoning not in VALID_REASONING_EFFORTS:
            raise ConfigurationError(
                f"MODEL_{tier}_REASONING={reasoning!r} 不是合法档位；"
                f"只接受 {VALID_REASONING_EFFORTS}（注意：medium 是非法值）"
            )

        configs[tier] = TierConfig(
            tier=tier,
            model=str(model),
            reasoning=reasoning,
            price_input_per_1m=price_in,
            price_output_per_1m=price_out,
            price_cache_hit_input_per_1m=cache_hit_price,
            peak_price_multiplier=peak_multiplier,
        )
    return configs


@dataclass
class ResolvedModel:
    """一次调用被解析成的具体目标。仅供记录与排障，业务层不该依赖它。"""

    tier: str
    model: str
    reasoning: str


class Router:
    """按等级路由到具体型号。"""

    def __init__(
        self,
        gateway: ModelGateway,
        settings: Any,
        *,
        run_repository: Any = None,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.run_repository = run_repository
        self.tiers = read_tier_configs(settings)

    # ---------- 解析 ----------

    def resolve(self, tier: str) -> ResolvedModel:
        """把等级翻译成具体型号 + 档位。"""
        normalized = tier.upper()
        if normalized == TIER_L0:
            raise L0DoesNotCallModelError(
                "L0 是代码 / 规则等级，不调用模型（计划第 599 行）；"
                "请用确定性代码路径处理，不要回退到 L1"
            )
        if normalized not in TIERS:
            raise ValueError(f"未知模型等级 {tier!r}，只接受 {TIERS}")
        config = self.tiers[normalized]
        return ResolvedModel(
            tier=normalized, model=config.model, reasoning=config.reasoning
        )

    def tier_config(self, tier: str) -> TierConfig:
        return self.tiers[tier.upper()]

    def fingerprint(self) -> dict[str, str]:
        """当前等级→型号映射的快照。

        写进 Run 记录，便于事后确认"那次分析用的是哪个型号"——
        型号可从环境变量切换，不记下来就无法复现历史结果。
        """
        return {tier: cfg.model for tier, cfg in sorted(self.tiers.items())}

    def estimate_cost(
        self,
        tier: str,
        tokens_input: int,
        tokens_output: int,
        *,
        cached_tokens: int = 0,
        peak: bool | None = None,
    ) -> float:
        """按等级单价估算成本（元）。阶段 07 的 Pre-check 会用它。

        `peak=None` 时按**当前时刻**判断峰谷；Pre-check 的估算是"现在要花多少"，
        所以默认取当下是对的。显式传值用于测试与回放。
        """
        return self.tier_config(tier).estimate_cost(
            tokens_input,
            tokens_output,
            cached_tokens=cached_tokens,
            peak=self._is_peak_now() if peak is None else peak,
        )

    def _is_peak_now(self) -> bool:
        """现在是否处于供应商的高峰时段（窗口见 `is_peak_time`）。"""
        from app.utils.timestamps import is_peak_time

        return is_peak_time(
            datetime.now(timezone.utc),
            default_timezone=getattr(self.settings, "default_timezone", "Asia/Shanghai"),
        )

    # ---------- 调用 ----------

    def generate(
        self,
        *,
        tier: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_output_tokens: int,
        project_id: int | None = None,
        run_id: int | None = None,
        call_id: str | None = None,
        record: bool = True,
    ) -> ModelResult:
        """调用模型；成功时把用量与成本即时记进 `AgentRun.model_calls`。

        `record=True` 且给了 `project_id`/`run_id` 时才会落库；缺任何一个
        就只返回结果并在 `raw` 里留一条说明——**不静默假装记过了**。
        """
        target = self.resolve(tier)

        result = self.gateway.generate(
            prompt,
            schema,
            max_output_tokens,
            model=target.model,
            reasoning=target.reasoning,
        )
        result.tier = target.tier

        config = self.tiers[target.tier]
        # 命中缓存的输入 token 单独计价；高峰时段乘峰值倍数。
        # 两个都不传的话账单会偏（缓存那侧偏高、峰谷那侧偏低），
        # 而偏差会直接喂给预算闸门。
        result.cost = config.estimate_cost(
            result.tokens_input,
            result.tokens_output,
            cached_tokens=getattr(result, "cached_tokens", 0) or 0,
            peak=self._is_peak_now(),
        )

        if record:
            self._record(result, project_id=project_id, run_id=run_id, call_id=call_id)

        return result

    def _record(
        self,
        result: ModelResult,
        *,
        project_id: int | None,
        run_id: int | None,
        call_id: str | None,
    ) -> None:
        if self.run_repository is None or project_id is None or run_id is None:
            result.raw.setdefault(
                "_recording_skipped",
                "缺少 run_repository / project_id / run_id，本次调用未落库",
            )
            return

        import uuid

        entry = result.as_model_call(call_id=call_id or f"c_{uuid.uuid4().hex[:12]}")
        self.run_repository.append_model_call(project_id, run_id, entry)


def build_router(settings: Any, *, run_repository: Any = None, gateway: ModelGateway | None = None) -> Router:
    """组合根：按配置装配 Gateway 与 Router。

    API Key 走 `credentials.resolve_api_key()`（环境变量优先，其次 DSH 凭据文件），
    **不硬编码、不进代码库**。
    """
    from app.gateways.credentials import resolve_api_key
    from app.gateways.deepseek import DeepSeekGateway

    if gateway is None:
        base_url = _tier_field(settings, "model_provider_base_url") or ""
        if not base_url:
            raise ConfigurationError(
                "MODEL_PROVIDER_BASE_URL 未配置：请在 .env 里设置供应商端点"
            )
        gateway = DeepSeekGateway(base_url=str(base_url), api_key=resolve_api_key())

    return Router(gateway, settings, run_repository=run_repository)
