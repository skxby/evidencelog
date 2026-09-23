"""Tool Registry：所有工具统一注册（计划第 572–588 行）。

**tool 与 analyzer 的边界**（计划第 396–402、586 行）：

    tool     通用数据算子：过滤 / 统计 / 时间窗口，跨领域复用，**不含领域判断**
    analyzer 领域检测：含阈值与判断逻辑，是 domain 私有资产

所以本层**不得出现任何阈值判断**（"CPU > 90% 算异常"那类逻辑属于 analyzer）。

工具是**纯确定性函数**：同样输入必然同样输出，不调用模型（红线 1）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: 事件在本层的统一形状。刻意用 dict 而不是 ORM 或领域对象：
#: 工具要跨领域复用，就不该依赖任何一方的类型。
#: 约定键：event_id / timestamp / severity / event_type / message / payload
EventDict = Mapping[str, Any]

#: 工具默认超时（秒）。计划第 576 行要求元数据里带 timeout。
DEFAULT_TIMEOUT_SECONDS = 30.0


class ToolNotFoundError(LookupError):
    """请求的工具未注册。"""


class ToolInputError(ValueError):
    """工具输入不合法。

    **不静默纠正非法输入**：悄悄把坏参数当成默认值，会产出看起来正常的错结果。
    """


class ToolExecutionError(RuntimeError):
    """工具执行失败。包装原始异常，保留类型与消息便于定位。"""


@dataclass(frozen=True)
class ToolSpec:
    """工具元数据 + 可调用体（计划第 576 行：`name, version, domain, input_schema,
    output_schema, timeout`）。"""

    name: str
    version: str
    domain: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    timeout: float
    func: Callable[..., Any]
    description: str = ""
    #: 产出是否与输入顺序一致（大多数聚合工具不保证）
    deterministic: bool = True

    def as_dict(self) -> dict[str, Any]:
        """元数据视图（不含 func，便于序列化进 Run 记录）。"""
        return {
            "name": self.name,
            "version": self.version,
            "domain": self.domain,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "timeout": self.timeout,
            "description": self.description,
            "deterministic": self.deterministic,
        }


@dataclass
class ToolResult:
    """执行结果，可直接记入 Run（验收：执行结果可记录到 Run）。"""

    tool: str
    version: str
    ok: bool
    output: Any = None
    elapsed_ms: float = 0.0
    error: str | None = None
    #: 本次处理的事件条数，便于成本/用量统计
    items_in: int = 0

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool,
            "version": self.version,
            "ok": self.ok,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "items_in": self.items_in,
        }
        if self.ok:
            payload["output"] = self.output
        else:
            # 失败必须带原因，绝不产出「看着正常」的空结果（红线 4）
            payload["error"] = self.error
        return payload


class ToolRegistry:
    """工具注册表。按名取用，元数据可枚举。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name!r} 已注册，拒绝覆盖")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(
                f"工具 {name!r} 未注册；已注册：{sorted(self._tools)}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def domain_of(self, name: str) -> str:
        return self.get(name).domain

    def by_domain(self, domain: str) -> list[ToolSpec]:
        return [self._tools[n] for n in sorted(self._tools) if self._tools[n].domain == domain]

    def specs(self) -> list[ToolSpec]:
        return [self._tools[n] for n in sorted(self._tools)]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def call(self, name: str, /, **kwargs: Any) -> ToolResult:
        """按名执行工具，返回可记录的 `ToolResult`。

        执行失败**不抛异常**，而是返回 `ok=False` 且带 `error` 的结果：
        调用方（Agent 循环）需要把失败也记进 Run，而不是让整个流程崩掉。
        但错误信息一定带上，绝不吞掉原因。
        """
        spec = self.get(name)
        items_in = _count_items(kwargs)

        started = time.perf_counter()
        try:
            output = spec.func(**kwargs)
        except (ToolInputError, TypeError, ValueError) as exc:
            return ToolResult(
                tool=spec.name,
                version=spec.version,
                ok=False,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
                items_in=items_in,
            )
        except Exception as exc:  # noqa: BLE001 - 未知异常也要如实记录，不能静默
            return ToolResult(
                tool=spec.name,
                version=spec.version,
                ok=False,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
                items_in=items_in,
            )

        return ToolResult(
            tool=spec.name,
            version=spec.version,
            ok=True,
            output=output,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            items_in=items_in,
        )


def _count_items(kwargs: Mapping[str, Any]) -> int:
    """估算本次处理的输入条数，用于用量统计。

    只认名字里带 events/items 的序列参数，不猜别的。
    """
    for key in ("events", "items"):
        value = kwargs.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return len(value)
    return 0


#: 进程内默认注册表
_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """返回已装好内置工具的注册表（首次调用时装配）。"""
    global _registry
    if _registry is None:
        from app.tools.wiring import build_default_tool_registry

        _registry = build_default_tool_registry()
    return _registry


def reset_tool_registry() -> None:
    """仅供测试：清空单例，让下一次调用重新装配。"""
    global _registry
    _registry = None


def make_spec(
    *,
    name: str,
    version: str,
    domain: str,
    func: Callable[..., Any],
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    description: str = "",
) -> ToolSpec:
    """构造 `ToolSpec`，`domain` 固定为 `generic` 的工具用它。"""
    return ToolSpec(
        name=name,
        version=version,
        domain=domain,
        input_schema=input_schema,
        output_schema=output_schema,
        timeout=timeout,
        func=func,
        description=description,
    )


def ensure_datetime(value: Any, *, field_name: str) -> datetime:
    """把输入里的时间统一成 aware datetime。

    接受 datetime 或 ISO 字符串；naive datetime **拒绝**——
    含糊的时间会让时间窗口切分悄悄错位（与阶段 04 同一条理由）。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ToolInputError(
                f"{field_name} 是 naive datetime；时间窗口运算需要带时区的时间"
            )
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ToolInputError(f"{field_name} 不是合法 ISO 时间: {value!r}") from exc
        if parsed.tzinfo is None:
            raise ToolInputError(f"{field_name} 字符串不含时区: {value!r}")
        return parsed
    raise ToolInputError(f"{field_name} 必须是 datetime 或 ISO 字符串，收到 {type(value).__name__}")
