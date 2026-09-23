"""三个通用数据算子的单测 —— 覆盖计划第 588 行要求的边界：空集 / 单事件 / 大窗口。

这些算子是**纯函数**：同样输入必然同样输出（每个工具都有确定性断言）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.tools.data_ops import event_filter, stats_calculator, time_window
from app.tools.registry import ToolInputError

UTC = timezone.utc
BASE = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


def ev(
    event_id: int,
    *,
    seconds: int = 0,
    severity: str = "low",
    event_type: str = "log",
    message: str = "ok",
    timestamp: datetime | None = ...,  # type: ignore[assignment]
) -> dict:
    event: dict = {
        "event_id": event_id,
        "severity": severity,
        "event_type": event_type,
        "message": message,
    }
    if timestamp is not ...:
        event["timestamp"] = timestamp
    else:
        event["timestamp"] = BASE + timedelta(seconds=seconds)
    return event


SAMPLE = [
    ev(1, seconds=0, severity="high", message="disk failure"),
    ev(2, seconds=30, severity="low", message="service ok"),
    ev(3, seconds=90, severity="medium", event_type="metric", message="cpu 80%"),
    ev(4, seconds=200, severity="high", event_type="metric", message="memory error"),
]


# ============================================================
# stats_calculator
# ============================================================


def test_stats_counts_totals_and_distributions():
    result = stats_calculator(SAMPLE)
    assert result["total"] == 4
    assert result["severity_counts"] == {"high": 2, "medium": 1, "low": 1}
    assert result["event_type_counts"] == {"log": 2, "metric": 2}


def test_stats_computes_time_span():
    result = stats_calculator(SAMPLE)
    assert result["span_seconds"] == 200.0
    assert result["time_start"] == BASE.isoformat()
    assert result["time_end"] == (BASE + timedelta(seconds=200)).isoformat()


def test_stats_error_rate_counts_high_severity_and_keywords():
    # 事件 1（high）与 4（high）算错误；2/3 不算
    result = stats_calculator(SAMPLE)
    assert result["error_count"] == 2
    assert result["error_rate"] == 0.5


def test_stats_treats_error_keywords_as_errors():
    events = [ev(1, severity="low", message="connection refused by peer")]
    result = stats_calculator(events)
    assert result["error_count"] == 1


def test_stats_empty_set_returns_zeroes_without_error():
    """空集是合法输入，必须返回零值结构而不是抛异常。"""
    result = stats_calculator([])
    assert result["total"] == 0
    assert result["error_count"] == 0
    assert result["error_rate"] == 0.0
    assert result["span_seconds"] == 0.0
    assert result["time_start"] is None
    assert result["time_end"] is None
    assert result["severity_counts"] == {"high": 0, "medium": 0, "low": 0}


def test_stats_single_event_has_zero_span():
    result = stats_calculator([ev(1)])
    assert result["total"] == 1
    assert result["span_seconds"] == 0.0
    assert result["time_start"] == result["time_end"]


def test_stats_reports_events_missing_timestamp():
    """无时间戳的事件不能被当成「跨度 0」，要显式暴露。"""
    events = [ev(1, timestamp=None), ev(2, timestamp=None)]
    result = stats_calculator(events)
    assert result["total"] == 2
    assert result["events_without_timestamp"] == 2
    assert result["time_start"] is None


def test_stats_rejects_non_list_input():
    with pytest.raises(ToolInputError, match="必须是列表"):
        stats_calculator("not a list")  # type: ignore[arg-type]


def test_stats_rejects_non_dict_items():
    with pytest.raises(ToolInputError, match=r"events\[0\]"):
        stats_calculator(["not a dict"])  # type: ignore[list-item]


def test_stats_is_deterministic():
    assert stats_calculator(SAMPLE) == stats_calculator(SAMPLE)


# ============================================================
# event_filter
# ============================================================


def test_filter_by_severity_is_or_within_condition():
    result = event_filter(SAMPLE, severities=["high", "medium"])
    assert result["matched_count"] == 3


def test_filter_by_event_type():
    result = event_filter(SAMPLE, event_types=["metric"])
    assert result["matched_count"] == 2


def test_filter_conditions_are_and_between_kinds():
    result = event_filter(SAMPLE, severities=["high"], event_types=["metric"])
    assert result["matched_count"] == 1
    assert result["matched_event_ids"] == [4]


def test_filter_by_time_range_is_inclusive():
    result = event_filter(
        SAMPLE,
        time_start=BASE + timedelta(seconds=30),
        time_end=BASE + timedelta(seconds=90),
    )
    assert result["matched_event_ids"] == [2, 3]


def test_filter_by_keyword_matches_message_substring():
    result = event_filter(SAMPLE, keyword="cpu")
    assert result["matched_count"] == 1


def test_filter_keyword_is_case_insensitive():
    assert event_filter(SAMPLE, keyword="DISK")["matched_count"] == 1


def test_filter_no_conditions_returns_everything():
    assert event_filter(SAMPLE)["matched_count"] == 4


def test_filter_empty_input_returns_empty_result():
    result = event_filter([], severities=["high"])
    assert result["matched_event_ids"] == []
    assert result["matched_count"] == 0
    assert result["input_count"] == 0


def test_filter_reports_events_skipped_for_lack_of_timestamp():
    """时间条件生效时，无时间戳的事件无法判定 —— 如实计数，不悄悄当不匹配。"""
    events = [ev(1, timestamp=BASE), ev(2, timestamp=None)]
    result = event_filter(events, time_start=BASE)
    assert result["matched_count"] == 1
    assert result["skipped_no_timestamp"] == 1


def test_filter_accepts_iso_string_times():
    result = event_filter(
        SAMPLE,
        time_start=(BASE + timedelta(seconds=30)).isoformat(),
    )
    assert result["matched_count"] == 3


def test_filter_rejects_inverted_range():
    with pytest.raises(ToolInputError, match="晚于"):
        event_filter(
            SAMPLE,
            time_start=BASE + timedelta(seconds=100),
            time_end=BASE,
        )


def test_filter_rejects_naive_datetime():
    """naive 时间会让窗口切分悄悄错位，必须拒绝而不是猜时区。"""
    with pytest.raises(ToolInputError, match="naive"):
        # 刻意构造 naive datetime 以验证它被拒绝（DTZ001 在此是测试意图）
        naive = datetime(2026, 9, 23, 12, 0)  # noqa: DTZ001
        event_filter(SAMPLE, time_start=naive)


def test_filter_rejects_bad_time_string():
    with pytest.raises(ToolInputError, match="不是合法 ISO"):
        event_filter(SAMPLE, time_start="yesterday")


def test_filter_preserves_input_order():
    matched_ids = event_filter(SAMPLE, severities=["high"])["matched_event_ids"]
    assert matched_ids == [1, 4]


def test_filter_is_deterministic():
    assert event_filter(SAMPLE, severities=["high"]) == event_filter(SAMPLE, severities=["high"])


# ============================================================
# time_window
# ============================================================


def test_time_window_splits_into_expected_buckets():
    result = time_window(SAMPLE, window_seconds=60)
    # 0s / 30s 落窗口 0；90s 落窗口 1；200s 落窗口 3
    assert result["window_count"] == 3
    assert [(w["index"], w["count"]) for w in result["windows"]] == [(0, 2), (1, 1), (3, 1)]


def test_time_window_omits_empty_windows():
    """大窗口下不产出成千上万个空桶。"""
    result = time_window(SAMPLE, window_seconds=60)
    indexes = [w["index"] for w in result["windows"]]
    assert 2 not in indexes  # 60–120s 区间没有事件


def test_time_window_large_window_collapses_to_one():
    """大窗口（宽于总跨度）→ 单个窗口。"""
    result = time_window(SAMPLE, window_seconds=100_000)
    assert result["window_count"] == 1
    assert result["windows"][0]["count"] == 4


def test_time_window_single_event_yields_one_window():
    result = time_window([ev(1)], window_seconds=60)
    assert result["window_count"] == 1
    assert result["windows"][0]["count"] == 1


def test_time_window_empty_set():
    result = time_window([], window_seconds=60)
    assert result["window_count"] == 0
    assert result["windows"] == []
    assert result["input_count"] == 0


def test_time_window_boundary_is_half_open():
    """左闭右开：恰好落在右边界的事件属于下一个窗口。"""
    events = [ev(1, seconds=0), ev(2, seconds=60)]
    result = time_window(events, window_seconds=60)
    assert [(w["index"], w["count"]) for w in result["windows"]] == [(0, 1), (1, 1)]


def test_time_window_aggregates_severity_and_errors():
    result = time_window(SAMPLE, window_seconds=60)
    first = result["windows"][0]
    assert first["severity_counts"] == {"high": 1, "medium": 0, "low": 1}
    assert first["error_count"] == 1


def test_time_window_respects_anchor():
    result = time_window(SAMPLE, window_seconds=60, anchor=BASE - timedelta(seconds=30))
    # anchor 前移 30s 后，0s 与 30s 分属不同窗口
    assert result["anchor"] == (BASE - timedelta(seconds=30)).isoformat()
    assert result["windows"][0]["count"] == 1


def test_time_window_counts_untimed_events_separately():
    events = [ev(1, seconds=0), ev(2, timestamp=None)]
    result = time_window(events, window_seconds=60)
    assert result["events_without_timestamp"] == 1
    assert result["windows"][0]["count"] == 1


@pytest.mark.parametrize("bad", [0, -1, 1.5, "60", None])
def test_time_window_rejects_invalid_width(bad):
    with pytest.raises(ToolInputError, match="window_seconds"):
        time_window(SAMPLE, window_seconds=bad)  # type: ignore[arg-type]


def test_time_window_is_deterministic():
    assert time_window(SAMPLE, window_seconds=60) == time_window(SAMPLE, window_seconds=60)
