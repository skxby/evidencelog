"""按 Project 覆盖策略的读取（计划第 629 行：策略可按 Project 覆盖）。

V1 不建策略表（计划第 356 行）。覆盖值放在 `Project.status` 之外的地方没有
合适列，故 V1 用**进程内注册表**：默认策略 + 可选的项目级覆盖。
持久化留到需要时（V1.5），届时再评估是否建表。

**不把覆盖值塞进 Project 的某个 JSON 列**：那会让"策略"这种横切关注点
散进业务表，且改策略要动数据。计划明确说 Policy 不建表、只把快照写进 Run。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.policy.policy import DEFAULT_POLICY, PolicyOverrides, RunPolicy


def policy_from_settings() -> RunPolicy:
    """默认策略从 `.env` 读，而不是只认 dataclass 里的硬编码值。

    `DEFAULT_RUN_MAX_COST` 一直是"声明了但没人读"：用户在 .env 里把单次上限
    调成 0.05，代码仍按硬编码的 0.30 放行。它是一道**花钱的闸门**，
    静默失效的方向恰好是"多花钱"。

    `MONTHLY_BUDGET` 也是同一形态，而且更难发现：上一轮把它"接上了"
    （`pre_check` 里加了月度判定、`cost_sum_since` 也写好了），
    但**这个函数只把单次上限从配置搬进策略**，月度预算仍取 dataclass 的默认
    10.0 —— 于是 .env 里把 MONTHLY_BUDGET 改成 0.02 完全不起作用。
    真机核验（2026-09-24）就是这么发现的：把月度预算调到 ¥0.02 后，
    真实花费 ¥0.0123 仍能继续发起分析。
    `settings_usage_audit` 漏掉它是因为 `.monthly_budget` 在别处出现过
    （读的是**策略对象**的属性，不是配置），工具的判据分不清这两者 ——
    这也是为什么"配置项是摆设"必须靠**行为测试**钉住，不能只靠文本审计。

    取值**负数**会在首次使用时由 `RunPolicy.__post_init__` 直接报错 ——
    配错了就该立刻响，而不是等跑出账单。
    `0` 是合法值，语义为"这个 Run 一分钱都不许花"（pre-check 会直接拒绝创建），
    这是有意保留的开关，不是漏校验。

    读配置用 `getattr` 兜底：测试与部分调用方会注入"只带所需字段"的
    settings 替身，缺这一项时退回策略自己的默认值，而不是抛 AttributeError
    （那会让人以为策略写错了，其实是替身不完整）。
    """
    from app.config import get_settings

    settings = get_settings()
    raw_cost = getattr(settings, "default_run_max_cost", DEFAULT_POLICY.run_max_cost)
    raw_month = getattr(settings, "monthly_budget", DEFAULT_POLICY.monthly_budget)
    return replace(
        DEFAULT_POLICY,
        run_max_cost=float(raw_cost),
        monthly_budget=float(raw_month),
    )


class ProjectPolicyStore:
    """进程内的项目级策略覆盖表。"""

    def __init__(self, default: RunPolicy = DEFAULT_POLICY) -> None:
        self._default = default
        self._overrides: dict[int, PolicyOverrides] = {}

    @property
    def default(self) -> RunPolicy:
        return self._default

    def set_override(self, project_id: int, **values: Any) -> RunPolicy:
        """设置项目级覆盖并返回生效后的策略。字段名非法会立刻报错。"""
        overrides = PolicyOverrides(values=dict(values))
        resolved = overrides.apply(self._default)  # 先验证再存
        self._overrides[project_id] = overrides
        return resolved

    def clear_override(self, project_id: int) -> None:
        self._overrides.pop(project_id, None)

    def policy_for(self, project_id: int) -> RunPolicy:
        """取该项目生效的策略；没有覆盖则返回默认策略。"""
        overrides = self._overrides.get(project_id)
        if overrides is None:
            return self._default
        return overrides.apply(self._default)


_store: ProjectPolicyStore | None = None


def get_policy_store() -> ProjectPolicyStore:
    """进程内单例。默认策略从配置构建（见 `policy_from_settings`）。"""
    global _store
    if _store is None:
        _store = ProjectPolicyStore(default=policy_from_settings())
    return _store


def reset_policy_store() -> None:
    """仅供测试：清空单例与所有覆盖。"""
    global _store
    _store = None
