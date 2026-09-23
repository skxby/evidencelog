"""V1 通用数据算子（计划第 578–586 行）。

三个工具，全是**纯确定性函数**：同样输入必然同样输出，不调用模型，
**不含领域判断**（阈值属 analyzer）。

    stats_calculator   事件总数、各 severity / event_type 分布、时间跨度、错误率
    event_filter       按 severity / 类型 / 时间范围 / 关键词过滤
    time_window        时间窗口切分与窗口内聚合

边界行为（验收要求「空集 / 单事件 / 大窗口行为明确」）在本模块被显式定义：
- 空集：返回零值结构，**不抛异常**（空集是合法输入，不是错误）；
- 单事件：时间跨度为 0，窗口数为 1；
- 大窗口：窗口宽度大于总跨度时返回单个窗口，不产生空窗口。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.tools.registry import EventDict, ToolInputError, ensure_datetime

#: 严重度取值（与 app.models.enums 同口径；此处不 import 以保持工具层可独立复用）
SEVERITIES = ("high", "medium", "low")
EVENT_TYPES = ("log", "metric")

#: 什么算「错误」。这是**字符串匹配规则**，不是领域阈值判断，故放在工具层。
#: `denied/refused/timeout` 都算——它们在运维语义里几乎总是真问题。
ERROR_KEYWORDS = (
    "error",
    "fail",
    "fatal",
    "panic",
    "crash",
    "exception",
    "denied",
    "refused",
    "timeout",
    "timed out",
)


def _iter_events(events: Any) -> list[EventDict]:
    if events is None:
        return []
    if not isinstance(events, (list, tuple)):
        # 拒绝非法输入，不静默当空集
        raise ToolInputError(f"events 必须是列表，收到 {type(events).__name__}")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ToolInputError(f"events[{index}] 必须是 dict，收到 {type(event).__name__}")
    return list(events)


def _is_error(event: EventDict) -> bool:
    """判定一条事件是否算错误。

    判据：severity == high，或 message 命中错误关键词。
    """
    if str(event.get("severity", "")).lower() == "high":
        return True
    message = str(event.get("message", "")).lower()
    return any(keyword in message for keyword in ERROR_KEYWORDS)


# ============================================================
# stats_calculator
# ============================================================


def stats_calculator(events: list[EventDict]) -> dict[str, Any]:
    """事件总数、各 severity / event_type 分布、时间跨度、错误率。"""
    rows = _iter_events(events)
    total = len(rows)

    severity_counts: Counter[str] = Counter()
    event_type_counts: Counter[str] = Counter()
    errors = 0

    for event in rows:
        severity = str(event.get("severity", "low")).lower()
        severity_counts[severity] += 1
        event_type_counts[str(event.get("event_type", "log")).lower()] += 1
        if _is_error(event):
            errors += 1

    timestamps = [
        ts for ts in (_maybe_ts(e.get("timestamp")) for e in rows) if ts is not None
    ]
    time_start = min(timestamps) if timestamps else None
    time_end = max(timestamps) if timestamps else None
    span_seconds = (time_end - time_start).total_seconds() if timestamps else 0.0

    return {
        "total": total,
        "severity_counts": {s: severity_counts.get(s, 0) for s in SEVERITIES}
        | {k: v for k, v in sorted(severity_counts.items()) if k not in SEVERITIES},
        "event_type_counts": {t: event_type_counts.get(t, 0) for t in EVENT_TYPES}
        | {k: v for k, v in sorted(event_type_counts.items()) if k not in EVENT_TYPES},
        "error_count": errors,
        # 空集时错误率为 0.0（不是 None，也不是 1.0）——空集没有错误
        "error_rate": round(errors / total, 4) if total else 0.0,
        "time_start": time_start.isoformat() if time_start else None,
        "time_end": time_end.isoformat() if time_end else None,
        "span_seconds": span_seconds,
        # 有事件但一条都没有可用时间戳 —— 这种事要显式暴露，别让调用方以为跨度真是 0
        "events_without_timestamp": total - len(timestamps),
    }


def _maybe_ts(value: Any) -> datetime | None:
    """尽量取时间戳；取不到返回 None（统计工具不该因个别坏行整体失败）。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


# ============================================================
# event_filter
# ============================================================


def event_filter(
    events: list[EventDict],
    *,
    severities: list[str] | None = None,
    event_types: list[str] | None = None,
    time_start: datetime | str | None = None,
    time_end: datetime | str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """按 severity / 类型 / 时间范围 / 关键词过滤。

    各条件之间是 **AND**；同一条件内的多个取值是 **OR**。
    不传某条件即不按它过滤（`None` 与空列表都表示「不过滤」）。

    **只返回命中的 event_id，不返回事件本体**，理由是两条都重要：
    1. 事件本体里的 `timestamp` 是 datetime，塞进 `AgentRun.tool_usage`（JSONB）
       会在 flush 时抛 TypeError —— 工具结果必须天生可记录；
    2. 命中可能上千条，把本体倒进 Run 记录会让表无谓膨胀。
    需要本体时拿 id 回查即可。
    """
    rows = _iter_events(events)

    severity_set = {s.lower() for s in (severities or [])}
    event_type_set = {t.lower() for t in (event_types or [])}
    start = ensure_datetime(time_start, field_name="time_start") if time_start is not None else None
    end = ensure_datetime(time_end, field_name="time_end") if time_end is not None else None
    if start is not None and end is not None and start > end:
        raise ToolInputError(f"time_start({start}) 晚于 time_end({end})")
    needle = keyword.lower() if keyword else None

    matched_ids: list[Any] = []
    matched_without_id = 0
    skipped_no_timestamp = 0

    for event in rows:
        if severity_set and str(event.get("severity", "")).lower() not in severity_set:
            continue
        if event_type_set and str(event.get("event_type", "")).lower() not in event_type_set:
            continue

        if start is not None or end is not None:
            ts = _maybe_ts(event.get("timestamp"))
            if ts is None:
                # 时间条件生效时，无时间戳的事件无法判定，如实跳过并计数
                skipped_no_timestamp += 1
                continue
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue

        if needle is not None:
            haystack = f"{event.get('message', '')}".lower()
            if needle not in haystack:
                continue

        if "event_id" in event:
            matched_ids.append(event["event_id"])
        else:
            matched_without_id += 1

    return {
        "matched_event_ids": matched_ids,
        "matched_count": len(matched_ids) + matched_without_id,
        "input_count": len(rows),
        "skipped_no_timestamp": skipped_no_timestamp,
        # 命中但没有 event_id 的条数：调用方需要知道"数得出来但取不回来"的部分
        "matched_without_id": matched_without_id,
    }


# ============================================================
# time_window
# ============================================================


def time_window(
    events: list[EventDict],
    *,
    window_seconds: int,
    anchor: datetime | str | None = None,
) -> dict[str, Any]:
    """时间窗口切分与窗口内聚合。

    `anchor` 是第一个窗口的起点；不传则取事件里的最早时间。
    窗口按 `[start, start+width)` 左闭右开，**只产出有事件的窗口**
    （不产出空窗口，避免大窗口下返回成千上万个空桶）。

    无时间戳的事件被计入 `events_without_timestamp`，不塞进任何窗口。
    """
    if not isinstance(window_seconds, int) or window_seconds <= 0:
        raise ToolInputError(f"window_seconds 必须是正整数，收到 {window_seconds!r}")

    rows = _iter_events(events)
    timed: list[tuple[datetime, EventDict]] = []
    untimed = 0
    for event in rows:
        ts = _maybe_ts(event.get("timestamp"))
        if ts is None:
            untimed += 1
            continue
        timed.append((ts, event))

    if not timed:
        return {
            "window_seconds": window_seconds,
            "window_count": 0,
            "windows": [],
            "events_without_timestamp": untimed,
            "input_count": len(rows),
        }

    timed.sort(key=lambda pair: pair[0])
    origin = (
        ensure_datetime(anchor, field_name="anchor") if anchor is not None else timed[0][0]
    )
    width = timedelta(seconds=window_seconds)

    buckets: dict[int, list[EventDict]] = {}
    for ts, event in timed:
        # 早于 anchor 的事件归入第 0 个窗口（不能丢，也不能产生负索引）
        offset = ts - origin
        index = 0 if offset < timedelta(0) else int(offset // width)
        buckets.setdefault(index, []).append(event)

    windows = []
    for index in sorted(buckets):
        start = origin + width * index
        bucket_events = buckets[index]
        windows.append(
            {
                "index": index,
                "start": start.isoformat(),
                "end": (start + width).isoformat(),
                "count": len(bucket_events),
                "severity_counts": _severity_counts(bucket_events),
                "error_count": sum(1 for e in bucket_events if _is_error(e)),
            }
        )

    return {
        "window_seconds": window_seconds,
        "anchor": origin.isoformat(),
        "window_count": len(windows),
        "windows": windows,
        "events_without_timestamp": untimed,
        "input_count": len(rows),
    }


def _severity_counts(events: list[EventDict]) -> dict[str, int]:
    counter: Counter[str] = Counter(str(e.get("severity", "low")).lower() for e in events)
    return {s: counter.get(s, 0) for s in SEVERITIES}
