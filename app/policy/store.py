"""按 Project 覆盖策略的读取（计划第 629 行：策略可按 Project 覆盖）。

V1 不建策略表（计划第 356 行）。覆盖值放在 `Project.status` 之外的地方没有
合适列，故 V1 用**进程内注册表**：默认策略 + 可选的项目级覆盖。
持久化留到需要时（V1.5），届时再评估是否建表。

**不把覆盖值塞进 Project 的某个 JSON 列**：那会让"策略"这种横切关注点
散进业务表，且改策略要动数据。计划明确说 Policy 不建表、只把快照写进 Run。
"""

from __future__ import annotations

from typing import Any

from app.policy.policy import DEFAULT_POLICY, PolicyOverrides, RunPolicy


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
    """进程内单例。"""
    global _store
    if _store is None:
        _store = ProjectPolicyStore()
    return _store


def reset_policy_store() -> None:
    """仅供测试：清空单例与所有覆盖。"""
    global _store
    _store = None
