"""组合根：在这里把具体领域注册进注册表。

**这是唯一允许 import 具体 domain 的地方。** 核心 Runtime 只经
`app.domains.registry` 访问领域（红线 8）。

加一个领域只需两步：写好目录包，然后在 `build_default_registry()` 里加一行。
"""

from __future__ import annotations

from app.domains.computer_monitoring import ComputerMonitoringDomain
from app.domains.registry import DomainRegistry


def build_default_registry() -> DomainRegistry:
    """装配内置领域。"""
    registry = DomainRegistry()
    registry.register(ComputerMonitoringDomain())
    return registry
