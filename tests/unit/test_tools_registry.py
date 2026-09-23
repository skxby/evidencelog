"""Tool Registry 单测 —— 覆盖计划第 588 行验收：注册器能按名取用、结果可记录到 Run。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.tools.registry import (
    ToolInputError,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ensure_datetime,
    get_tool_registry,
    make_spec,
    reset_tool_registry,
)
from app.tools.wiring import GENERIC_DOMAIN, TOOL_VERSION, build_default_tool_registry

UTC = timezone.utc
BASE = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture()
def registry() -> ToolRegistry:
    return build_default_tool_registry()


def sample_events() -> list[dict]:
    return [
        {"event_id": 1, "timestamp": BASE, "severity": "high", "event_type": "log", "message": "boom"},
        {
            "event_id": 2,
            "timestamp": BASE + timedelta(seconds=10),
            "severity": "low",
            "event_type": "log",
            "message": "fine",
        },
    ]


# ============================================================
# 注册与取用（验收：注册器能按名取用）
# ============================================================


def test_three_v1_tools_are_registered(registry: ToolRegistry):
    """计划第 580–584 行的三个通用算子。"""
    assert registry.names() == ["event_filter", "stats_calculator", "time_window"]
    assert len(registry) == 3


def test_get_returns_spec_with_full_metadata(registry: ToolRegistry):
    """计划第 576 行：元数据必须含 name/version/domain/input_schema/output_schema/timeout。"""
    spec = registry.get("stats_calculator")
    assert isinstance(spec, ToolSpec)
    assert spec.name == "stats_calculator"
    assert spec.version == TOOL_VERSION
    assert spec.domain == GENERIC_DOMAIN
    assert isinstance(spec.input_schema, dict) and spec.input_schema
    assert isinstance(spec.output_schema, dict) and spec.output_schema
    assert spec.timeout > 0


def test_spec_as_dict_is_serializable():
    """元数据要能进 Run 记录，故不得含函数对象。"""
    import json

    spec = build_default_tool_registry().get("event_filter")
    payload = spec.as_dict()
    assert "func" not in payload
    json.dumps(payload)  # 不抛异常即通过


def test_unknown_tool_raises_with_available_names(registry: ToolRegistry):
    with pytest.raises(ToolNotFoundError, match="未注册"):
        registry.get("no_such_tool")


def test_duplicate_registration_is_refused(registry: ToolRegistry):
    spec = registry.get("stats_calculator")
    with pytest.raises(ValueError, match="已注册"):
        registry.register(spec)


def test_tools_are_generic_domain_not_domain_specific(registry: ToolRegistry):
    """工具是跨领域复用的通用算子，不该挂到具体领域名下。"""
    for spec in registry.specs():
        assert spec.domain == GENERIC_DOMAIN
    assert len(registry.by_domain(GENERIC_DOMAIN)) == 3
    assert registry.by_domain("computer_monitoring") == []


def test_registry_supports_custom_tool_registration():
    """加工具 = 注册一行，内核不动。"""
    custom = ToolRegistry()
    custom.register(
        make_spec(
            name="my_op",
            version="0.1.0",
            domain="generic",
            func=lambda items: len(items),
            input_schema={"type": "object"},
            output_schema={"type": "integer"},
        )
    )
    assert custom.call("my_op", items=[1, 2, 3]).output == 3


def test_tools_module_does_not_import_a_concrete_domain():
    """通用工具不得依赖具体领域 —— 否则「跨领域复用」是假的（红线 8）。"""
    from pathlib import Path

    for path in (Path(__file__).resolve().parents[2] / "app" / "tools").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "app.domains.computer_monitoring" not in text, path.name
        assert "from app.domains" not in text, path.name


# ============================================================
# 执行与记录（验收：执行结果可记录到 Run）
# ============================================================


def test_call_returns_recordable_result(registry: ToolRegistry):
    result = registry.call("stats_calculator", events=sample_events())
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.tool == "stats_calculator"
    assert result.output["total"] == 2
    assert result.items_in == 2
    assert result.elapsed_ms >= 0


def test_result_as_dict_is_json_serializable(registry: ToolRegistry):
    import json

    payload = registry.call("stats_calculator", events=sample_events()).as_dict()
    json.dumps(payload)
    assert payload["ok"] is True
    assert "output" in payload
    assert "error" not in payload


def test_failed_result_carries_error_and_omits_output(registry: ToolRegistry):
    """红线 4：失败必须带原因，绝不产出「看着正常」的结果。"""
    result = registry.call("stats_calculator", events="not a list")
    assert result.ok is False
    assert result.error and "ToolInputError" in result.error
    payload = result.as_dict()
    assert "output" not in payload
    assert "error" in payload


def test_call_does_not_raise_on_tool_error(registry: ToolRegistry):
    """Agent 循环需要把失败也记进 Run，而不是让流程崩掉。"""
    result = registry.call("event_filter", events=sample_events(), keyword=None, severities=["bogus"])
    assert result.ok is True
    assert result.output["matched_count"] == 0


def test_unknown_tool_call_raises_before_execution():
    """按名取不到工具属于调用方错误，应直接抛，而不是返回一个假结果。"""
    registry = build_default_tool_registry()
    with pytest.raises(ToolNotFoundError):
        registry.call("nope")


def test_items_in_counts_events_argument(registry: ToolRegistry):
    result = registry.call("time_window", events=sample_events(), window_seconds=60)
    assert result.items_in == 2


def test_items_in_is_zero_when_no_sequence_argument(registry: ToolRegistry):
    result = registry.call("stats_calculator")
    assert result.items_in == 0
    assert result.ok is False  # 缺必填参数：如实失败


# ============================================================
# 单例
# ============================================================


def test_default_registry_singleton_is_reused():
    reset_tool_registry()
    first = get_tool_registry()
    second = get_tool_registry()
    assert first is second
    reset_tool_registry()


# ============================================================
# ensure_datetime 输入校验
# ============================================================


def test_ensure_datetime_accepts_aware_datetime():
    assert ensure_datetime(BASE, field_name="t") == BASE


def test_ensure_datetime_accepts_iso_string_with_offset():
    parsed = ensure_datetime("2026-09-23T12:00:00+00:00", field_name="t")
    assert parsed == BASE


def test_ensure_datetime_rejects_naive_datetime():
    with pytest.raises(ToolInputError, match="naive"):
        # 刻意构造 naive datetime 以验证它被拒绝（DTZ001 在此是测试意图）
        naive = datetime(2026, 9, 23, 12, 0)  # noqa: DTZ001
        ensure_datetime(naive, field_name="t")


def test_ensure_datetime_rejects_naive_string():
    with pytest.raises(ToolInputError, match="不含时区"):
        ensure_datetime("2026-09-23T12:00:00", field_name="t")


def test_ensure_datetime_rejects_wrong_type():
    with pytest.raises(ToolInputError, match="必须是 datetime"):
        ensure_datetime(12345, field_name="t")
