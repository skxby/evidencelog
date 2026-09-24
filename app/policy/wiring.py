"""把 Policy / Cost Controller 接进真实路径（组合根）。

**为什么单独一个模块**：`app/policy/` 里都是纯逻辑（估算、三道检查点、策略对象），
它们本身测得很全 —— 问题是**没有任何真实路径构造过 `CostController`**：
`grep -r "CostController(" app/` 只匹配到定义与导出，`get_policy_store()` 也没有
调用方。于是阶段 07 的三道闸门在真机上一次都没生效：
项目预算花不完（`budget_used` 永远是 0）、单次上限改 .env 不生效、
每次模型调用前也没有 Mid-check。

这和 `app/domains/registry.py` + `wiring.py` 是同一套分工：
纯逻辑不碰装配，装配集中在组合根，**一眼能看出"到底接上了没有"**。
"""

from __future__ import annotations

from typing import Any

from app.gateways.base import ConfigurationError
from app.policy.cost_controller import CostController
from app.policy.policy import RunPolicy


def build_cost_controller(
    *,
    project: Any = None,
    settings: Any = None,
    policy_store: Any = None,
    clock: Any = None,
    session: Any = None,
) -> CostController | None:
    """给某个 Project 建一个成本控制器；读不到型号配置时返回 `None`。

    返回 `None` 而不是抛错，是为了**不破坏降级路径**：没配型号时本来就调不了
    模型（链路会退到纯规则 L0 报告），此时既估不出成本、也不会产生花费，
    拦下创建反而是错的 —— 用户会看到"建不了分析"，而不是"模型没配好"。

    `Project.budget_total <= 0` 视为**未设预算**（不拦）。理由是 `budget_total`
    在 schema 里的默认值就是 0，若把 0 当"预算为零"，那么所有没显式填预算的
    项目都会连一次分析都发不起来 —— 那是把默认值当成了策略。真要卡预算就填个正数。
    """
    from app.config import get_settings
    from app.gateways.router import read_tier_configs
    from app.policy.store import get_policy_store

    settings = settings or get_settings()
    store = policy_store or get_policy_store()

    policy: RunPolicy = (
        store.policy_for(int(project.id)) if project is not None else store.default
    )

    try:
        tier_configs = read_tier_configs(settings)
    except ConfigurationError:
        # 型号没配齐：估不出成本，也不会花钱，交给既有的降级链处理
        return None

    total: float | None = None
    used = 0.0
    month_spent = 0.0
    if project is not None:
        raw_total = float(project.budget_total or 0.0)
        total = raw_total if raw_total > 0 else None
        used = float(project.budget_used or 0.0)

        # 本月已花：月度预算的 Pre-check 要用（计划第 644 行）。
        # 需要 session 才能查；拿不到就按 0 处理（**不假装超预算**：
        # 宁可少一道闸门，也不能因为查不到数据就把创建全拒了）。
        if session is not None:
            month_spent = _month_spent(session, int(project.id), settings=settings)

    return CostController(
        policy,
        tier_configs=tier_configs,
        project_budget_total=total,
        project_budget_used=used,
        month_spent=month_spent,
        # 峰谷价必须同时进**估算**与**计费**两侧：估值按闲时价算的话，
        # 高峰时段的预检会低估一倍，该拦的 Run 就放过去了（真机核验 2026-09-24）。
        peak_now=_is_peak_now(settings),
        clock=clock,
    )


def _is_peak_now(settings: Any) -> bool:
    """此刻是否处于供应商高峰时段（与 `Router._is_peak_now` 同一判断）。"""
    from datetime import datetime, timezone

    from app.utils.timestamps import is_peak_time

    try:
        return bool(
            is_peak_time(
                datetime.now(timezone.utc),
                default_timezone=getattr(settings, "default_timezone", "Asia/Shanghai")
                or "Asia/Shanghai",
            )
        )
    except Exception:  # noqa: BLE001 - 时区配置不可读时按闲时价估（与旧行为一致）
        return False


def month_start(*, settings: Any = None, now: Any = None) -> Any:
    """本月起点（按配置时区的自然月，返回 UTC datetime）。

    为什么按本地时区切月：预算是人按"这个月花了多少"理解的，
    用 UTC 切会让月初/月末各错 8 小时。
    """
    from datetime import datetime, timezone

    from app.utils.timestamps import get_zone

    settings = settings or _settings_or_none()
    tz_name = getattr(settings, "default_timezone", "Asia/Shanghai") or "Asia/Shanghai"
    local_now = (now or datetime.now(timezone.utc)).astimezone(get_zone(tz_name))
    return local_now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)


def _settings_or_none() -> Any:
    try:
        from app.config import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001 - 配置不可读时退回默认时区
        return None


def _month_spent(session: Any, project_id: int, *, settings: Any = None) -> float:
    from app.repositories.agent_run import AgentRunRepository

    return AgentRunRepository(session).cost_sum_since(
        project_id, month_start(settings=settings)
    )


class PolicyGuardedRouter:
    """在**每次模型调用前**做 Mid-check、调用后记账（计划第 646–648 行）。

    为什么包在 router 上而不是改链路：`Router.generate` 就是"一次模型调用"的
    唯一入口，包住它等于每次调用都被检查，且链路本身不必知道策略的存在
    （策略属于组合根，和 Registry/wiring 的分工一致）。

    `mid_check` 到顶会抛 `StopExecution`；调用方（链路）要**接住它并保留
    已经拿到的结论**，状态置 `partial_success` —— 计划第 648 行要的是
    "停止昂贵步骤，返回已完成部分"，不是把已经跑出来的东西一起丢掉。
    """

    def __init__(self, inner: Any, controller: CostController) -> None:
        self._inner = inner
        self._controller = controller

    def generate(self, **kwargs: Any) -> Any:
        self._controller.mid_check(tier=kwargs.get("tier"))
        result = self._inner.generate(**kwargs)
        self._controller.record_call(
            tokens_input=int(getattr(result, "tokens_input", 0) or 0),
            tokens_output=int(getattr(result, "tokens_output", 0) or 0),
            cost=float(getattr(result, "cost", 0.0) or 0.0),
        )
        return result

    def __getattr__(self, name: str) -> Any:
        # 其余属性（fingerprint、tier_config 等）原样透传，
        # 免得包一层之后别处读不到。
        return getattr(self._inner, name)
