"""DomainRegistry：注册与按 id + version 加载（计划第 428 行）。

**本模块刻意不 import 任何具体 domain。** 一旦在这里出现
`from app.domains.computer_monitoring import ...`，核心 Runtime 就与具体领域耦合了，
「加领域不改内核」也就不再成立（红线 8）。

具体领域的注册发生在组合根 `app/domains/wiring.py`。
"""

from __future__ import annotations

from app.domains.protocol import DomainBase


class DomainNotFoundError(LookupError):
    """请求的领域未注册。"""


class DomainVersionMismatchError(LookupError):
    """领域已注册，但版本不匹配。

    显式报错而不是"随便给一个版本"：领域版本会写进 Run 幂等键
    （计划第 700 行），静默用错版本会让历史结果不可复现。
    """


class DomainRegistry:
    """领域注册表。同一个 domain 可注册多个版本，按 id + version 精确加载。"""

    def __init__(self) -> None:
        self._domains: dict[tuple[str, str], DomainBase] = {}

    def register(self, domain: DomainBase) -> None:
        """注册一个领域实例。重复注册同一 (id, version) 直接报错，避免静默覆盖。"""
        key = (domain.domain_id, domain.version)
        if key in self._domains:
            raise ValueError(f"领域 {domain.domain_id}@{domain.version} 已注册，拒绝覆盖")
        self._domains[key] = domain

    def load(self, domain_id: str, version: str | None = None) -> DomainBase:
        """按 id（可选 version）加载领域。

        `version=None` 时，若该 id 只有一个版本则返回它；有多个版本则报错要求明确指定，
        不猜"最新的"——猜版本会让幂等键失去意义。
        """
        if version is not None:
            try:
                return self._domains[(domain_id, version)]
            except KeyError:
                raise DomainVersionMismatchError(
                    f"领域 {domain_id}@{version} 未注册；已注册版本：{self.versions_of(domain_id)}"
                ) from None

        candidates = [
            (v, d) for (i, v), d in self._domains.items() if i == domain_id
        ]
        if not candidates:
            raise DomainNotFoundError(
                f"领域 {domain_id} 未注册；已注册：{sorted(self.ids())}"
            )
        if len(candidates) > 1:
            raise DomainVersionMismatchError(
                f"领域 {domain_id} 存在多个版本 {sorted(v for v, _ in candidates)}，"
                f"必须显式指定 version（版本会进幂等键，不能靠猜）"
            )
        return candidates[0][1]

    def ids(self) -> list[str]:
        return sorted({i for i, _ in self._domains})

    def versions_of(self, domain_id: str) -> list[str]:
        return sorted(v for i, v in self._domains if i == domain_id)

    def all_domains(self) -> list[DomainBase]:
        return [self._domains[k] for k in sorted(self._domains)]

    def __len__(self) -> int:
        return len(self._domains)

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._domains


#: 进程内默认注册表。业务代码应通过 `get_domain_registry()` 拿它，不要自己 new。
_registry: DomainRegistry | None = None


def get_domain_registry() -> DomainRegistry:
    """返回已装好内置领域的注册表（首次调用时装配）。"""
    global _registry
    if _registry is None:
        from app.domains.wiring import build_default_registry

        _registry = build_default_registry()
    return _registry


def reset_domain_registry() -> None:
    """仅供测试：清空单例，让下一次调用重新装配。"""
    global _registry
    _registry = None
