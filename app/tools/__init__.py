"""Tool Registry 层：统一注册、按名取用、结果可记录。

三个 V1 通用算子见 `app/tools/data_ops.py`；注册与执行见 `app/tools/registry.py`。
具体工具的装配在组合根 `app/tools/wiring.py`。
"""

from __future__ import annotations

from app.tools.data_ops import event_filter, stats_calculator, time_window
from app.tools.registry import (
    DEFAULT_TIMEOUT_SECONDS,
    ToolExecutionError,
    ToolInputError,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    get_tool_registry,
    make_spec,
    reset_tool_registry,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ToolExecutionError",
    "ToolInputError",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "event_filter",
    "get_tool_registry",
    "make_spec",
    "reset_tool_registry",
    "stats_calculator",
    "time_window",
]
