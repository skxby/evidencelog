"""组合根：把内置通用工具注册进注册表。

这三个工具都是 **generic** 域：跨领域复用、不含领域判断（计划第 578、586 行）。
领域私有的检测逻辑走 analyzer，不在这里注册。
"""

from __future__ import annotations

from app.tools.data_ops import event_filter, stats_calculator, time_window
from app.tools.registry import DEFAULT_TIMEOUT_SECONDS, ToolRegistry, make_spec

#: 通用工具的领域标签。刻意不是 computer_monitoring——
#: 这些算子在别的领域同样可用。
GENERIC_DOMAIN = "generic"

TOOL_VERSION = "1.0.0"


def build_default_tool_registry() -> ToolRegistry:
    """装配 V1 的三个通用数据算子。"""
    registry = ToolRegistry()

    registry.register(
        make_spec(
            name="stats_calculator",
            version=TOOL_VERSION,
            domain=GENERIC_DOMAIN,
            func=stats_calculator,
            description="事件总数、severity / event_type 分布、时间跨度、错误率",
            timeout=DEFAULT_TIMEOUT_SECONDS,
            input_schema={
                "type": "object",
                "required": ["events"],
                "properties": {
                    "events": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "事件列表；约定键 event_id/timestamp/severity/event_type/message",
                    }
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "total": {"type": "integer"},
                    "severity_counts": {"type": "object"},
                    "event_type_counts": {"type": "object"},
                    "error_count": {"type": "integer"},
                    "error_rate": {"type": "number"},
                    "time_start": {"type": ["string", "null"]},
                    "time_end": {"type": ["string", "null"]},
                    "span_seconds": {"type": "number"},
                    "events_without_timestamp": {"type": "integer"},
                },
            },
        )
    )

    registry.register(
        make_spec(
            name="event_filter",
            version=TOOL_VERSION,
            domain=GENERIC_DOMAIN,
            func=event_filter,
            description="按 severity / event_type / 时间范围 / 关键词过滤（条件间为 AND）",
            timeout=DEFAULT_TIMEOUT_SECONDS,
            input_schema={
                "type": "object",
                "required": ["events"],
                "properties": {
                    "events": {"type": "array", "items": {"type": "object"}},
                    "severities": {"type": ["array", "null"], "items": {"type": "string"}},
                    "event_types": {"type": ["array", "null"], "items": {"type": "string"}},
                    "time_start": {"type": ["string", "null"], "description": "ISO 8601，含时区"},
                    "time_end": {"type": ["string", "null"], "description": "ISO 8601，含时区"},
                    "keyword": {"type": ["string", "null"], "description": "在 message 里子串匹配"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "matched_event_ids": {"type": "array", "items": {"type": "integer"}},
                    "matched_count": {"type": "integer"},
                    "input_count": {"type": "integer"},
                                        "skipped_no_timestamp": {"type": "integer"},
                    "matched_without_id": {"type": "integer"},
                },
            },
        )
    )

    registry.register(
        make_spec(
            name="time_window",
            version=TOOL_VERSION,
            domain=GENERIC_DOMAIN,
            func=time_window,
            description="时间窗口切分与窗口内聚合（左闭右开，只产出有事件的窗口）",
            timeout=DEFAULT_TIMEOUT_SECONDS,
            input_schema={
                "type": "object",
                "required": ["events", "window_seconds"],
                "properties": {
                    "events": {"type": "array", "items": {"type": "object"}},
                    "window_seconds": {"type": "integer", "minimum": 1},
                    "anchor": {"type": ["string", "null"], "description": "首个窗口起点，ISO 8601"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "window_seconds": {"type": "integer"},
                    "window_count": {"type": "integer"},
                    "windows": {"type": "array", "items": {"type": "object"}},
                    "events_without_timestamp": {"type": "integer"},
                    "input_count": {"type": "integer"},
                },
            },
        )
    )

    return registry
