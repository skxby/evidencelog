"""模板聚类与事件分组（计划第 743 行，修订说明第 6a 条）。

**V1 手写「正则变量替换」，不引 Drain3**（修订说明第 6a 条）：
把数字 / IP / hex / 时间戳 / 路径等可变部分替换为占位符得到模板，再按模板聚合。
理由：零额外依赖、行为可解释、易写单测。

生产环境日志的模板化有个绕不开的取舍：占位符太激进会把不同问题并成一组，
太保守则同一问题因 pid/端口不同而分裂。这里的选择是**保留结构性区分**
（进程名、错误关键词），**抹掉实例性差异**（数字、id、路径、时间）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

#: 同一模板在时间窗内才归为一组（计划第 340 行：同模板 / 同时间窗）
DEFAULT_GROUP_WINDOW_SECONDS = 300

#: 占位符
PLACEHOLDER_IP = "<IP>"
PLACEHOLDER_HEX = "<HEX>"
PLACEHOLDER_NUM = "<NUM>"
PLACEHOLDER_UUID = "<UUID>"
PLACEHOLDER_TIME = "<TIME>"
PLACEHOLDER_PATH = "<PATH>"
PLACEHOLDER_EMAIL = "<EMAIL>"

#: 替换顺序有讲究：先抹更具体的形态，否则 UUID 会被 <HEX> 或 <NUM> 先吃掉一半。
_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # UUID 优先：它同时像 hex 和数字
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        PLACEHOLDER_UUID,
    ),
    # 时间戳（含毫秒 / ISO / 常见分隔）
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\b"
            r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\b"
        ),
        PLACEHOLDER_TIME,
    ),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), PLACEHOLDER_EMAIL),
    (re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"), PLACEHOLDER_IP),
    # 长 hex（>=8 位）才是 id；短 hex 可能是普通单词的一部分
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), PLACEHOLDER_HEX),
    # 路径：至少两段，避免把 `a/b` 这种普通写法也吃掉
    (re.compile(r"(?:/[\w.\-]+){2,}/?"), PLACEHOLDER_PATH),
    # 十六进制字面量 0x...
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), PLACEHOLDER_HEX),
    # 数字（含小数、负号、带单位）
    (re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])"), PLACEHOLDER_NUM),
)

#: 折叠重复空白，让 `a  b` 与 `a b` 得到同一模板
_WHITESPACE = re.compile(r"\s+")


def make_template(message: str) -> str:
    """把一条 message 变成模板：抹掉实例性差异，保留结构性信息。"""
    if not message:
        return ""
    text = message
    for pattern, placeholder in _SUBSTITUTIONS:
        text = pattern.sub(placeholder, text)
    return _WHITESPACE.sub(" ", text).strip()


def group_key_for(*, source_id: int, template: str, window_start: Any = None) -> str:
    """分组键：source + 模板 + 时间窗起点。

    时间窗进键是必要的：同一模板在两个相隔很远的时段各刷一次，是两次独立事件，
    合成一组会让"事件数"与"持续时间"都失真。
    """
    raw = f"{source_id}\0{template}\0{window_start or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class GroupableEvent:
    """分组算法需要的最小事件视图。"""

    event_id: int
    source_id: int
    timestamp: Any
    message: str
    severity: str = "low"
    event_type: str = "log"


@dataclass
class FormedGroup:
    """聚类产出的一组事件。"""

    source_id: int
    group_key: str
    template: str
    event_ids: list[int] = field(default_factory=list)
    time_start: Any = None
    time_end: Any = None

    @property
    def event_count(self) -> int:
        return len(self.event_ids)


def _window_start(moment: Any, window_seconds: int) -> Any:
    """把时刻对齐到窗口起点（floor）。"""
    if moment is None:
        return None
    from datetime import datetime

    if not isinstance(moment, datetime):
        return None
    epoch = int(moment.timestamp())
    floored = epoch - (epoch % window_seconds)
    return datetime.fromtimestamp(floored, tz=moment.tzinfo)


def cluster_events(
    events: list[GroupableEvent],
    *,
    window_seconds: int = DEFAULT_GROUP_WINDOW_SECONDS,
) -> list[FormedGroup]:
    """按「同 source + 同模板 + 同时间窗」聚类。

    输出按 (source_id, group_key) 稳定排序，保证同样输入得到同样结果顺序
    （确定性是红线 1 的前提）。
    """
    if window_seconds <= 0:
        raise ValueError(f"window_seconds 必须为正，收到 {window_seconds}")

    groups: dict[tuple[int, str], FormedGroup] = {}

    for event in events:
        template = make_template(event.message)
        start = _window_start(event.timestamp, window_seconds)
        key = group_key_for(
            source_id=event.source_id, template=template, window_start=start
        )
        bucket = (event.source_id, key)
        group = groups.get(bucket)
        if group is None:
            group = FormedGroup(
                source_id=event.source_id,
                group_key=key,
                template=template,
                time_start=event.timestamp,
                time_end=event.timestamp,
            )
            groups[bucket] = group

        group.event_ids.append(event.event_id)
        if event.timestamp is not None:
            if group.time_start is None or event.timestamp < group.time_start:
                group.time_start = event.timestamp
            if group.time_end is None or event.timestamp > group.time_end:
                group.time_end = event.timestamp

    return [groups[key] for key in sorted(groups)]


# ============================================================
# 事故归并（计划第 50–53 行的 V1 启发式）
# ============================================================

#: 默认归并窗口（计划第 96 行：MERGE_WINDOW 默认 10 分钟）
DEFAULT_MERGE_WINDOW_SECONDS = 600

#: severity 档位序，用于"同档"判定
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class MergeCandidate:
    """一个待归并的分组。"""

    group_id: int
    source_id: int
    severity: str
    time_start: Any
    time_end: Any
    #: 异常签名：同一 analyzer 名或同一模板簇家族
    signature: str = ""


def _same_severity_tier(left: str, right: str) -> bool:
    return _SEVERITY_RANK.get(left, 0) == _SEVERITY_RANK.get(right, 0)


def _windows_overlap_or_close(
    left: MergeCandidate, right: MergeCandidate, *, merge_window_seconds: int
) -> bool:
    """时间窗重叠，或间隔 ≤ merge_window（计划第 96 行）。"""
    from datetime import datetime

    for attr in ("time_start", "time_end"):
        if not isinstance(getattr(left, attr), datetime) or not isinstance(
            getattr(right, attr), datetime
        ):
            return False

    if left.time_start <= right.time_end and right.time_start <= left.time_end:
        return True  # 重叠

    gap = (
        right.time_start - left.time_end
        if right.time_start > left.time_end
        else left.time_start - right.time_end
    )
    return gap <= timedelta(seconds=merge_window_seconds)


def merge_into_incidents(
    candidates: list[MergeCandidate],
    *,
    merge_window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS,
    require_same_severity: bool = True,
    existing_incidents: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把分组归并成事故（计划第 92–99 行）。

    归并条件**同时满足**：
      ① 同 source_id；
      ② 时间窗重叠，或间隔 ≤ merge_window；
      ③ 命中同一「异常签名」（同 analyzer 或同模板簇家族）；
      ④ 可选：severity 同档。

    **这是确定性代码（红线 1），且明确是近似分组、不是根因判定**
    （修订说明第 5 条）——根因只能标 inference / possibility 并挂证据。
    """
    if merge_window_seconds <= 0:
        raise ValueError(f"merge_window_seconds 必须为正，收到 {merge_window_seconds}")

    incidents: list[dict[str, Any]] = []

    # 按 (source, signature) 分桶后再比时间：把 O(n²) 的全局两两比较
    # 降成桶内比较，且天然满足条件①③。
    buckets: dict[tuple[int, str], list[MergeCandidate]] = {}
    for candidate in candidates:
        buckets.setdefault((candidate.source_id, candidate.signature), []).append(candidate)

    for (source_id, signature), bucket in sorted(buckets.items()):
        ordered = sorted(bucket, key=lambda c: (c.time_start or 0))
        current: list[MergeCandidate] = []

        def flush(
            members: list[MergeCandidate],
            *,
            source_id: int = source_id,
            signature: str = signature,
        ) -> None:
            """把当前累积的一组落成一个事故。

            参数显式传入而不是直接闭包捕获循环变量：闭包 + 循环变量是典型的
            延迟绑定陷阱（ruff B023），一旦调用时机改变就会把后一轮的值写进
            前一轮的结果里，而且这种错误在测试里往往看不出来。
            """
            if not members:
                return
            incidents.append(
                {
                    "source_id": source_id,
                    "signature": signature,
                    "group_ids": [c.group_id for c in members],
                    "severity": max(
                        (c.severity for c in members),
                        key=lambda s: _SEVERITY_RANK.get(s, 0),
                    ),
                    "time_start": min(
                        (c.time_start for c in members if c.time_start), default=None
                    ),
                    "time_end": max(
                        (c.time_end for c in members if c.time_end), default=None
                    ),
                    # 措辞纪律：这是近似分组，不是根因判定
                    "merge_basis": "same_source+same_signature+time_window",
                }
            )

        for candidate in ordered:
            if not current:
                current.append(candidate)
                continue

            head = current[-1]
            same_window = _windows_overlap_or_close(
                head, candidate, merge_window_seconds=merge_window_seconds
            )
            same_severity = (
                _same_severity_tier(head.severity, candidate.severity)
                if require_same_severity
                else True
            )
            if same_window and same_severity:
                current.append(candidate)
            else:
                flush(current)
                current = [candidate]
        flush(current)

    # 与历史事故合并：时间窗连续且同签名的并入已有事故（计划第 102 行）
    for incident in incidents:
        if not existing_incidents:
            break
        for existing in existing_incidents:
            if existing.get("source_id") != incident["source_id"]:
                continue
            if existing.get("signature") != incident["signature"]:
                continue
            if _incident_windows_close(
                existing, incident, merge_window_seconds=merge_window_seconds
            ):
                incident["reused_incident_id"] = existing.get("id")
                break

    return incidents


def _incident_windows_close(
    left: dict[str, Any], right: dict[str, Any], *, merge_window_seconds: int
) -> bool:
    from datetime import datetime

    left_start, left_end = left.get("time_start"), left.get("time_end")
    right_start, right_end = right.get("time_start"), right.get("time_end")
    if not all(isinstance(x, datetime) for x in (left_start, left_end, right_start, right_end)):
        return False
    if left_start <= right_end and right_start <= left_end:
        return True
    gap = (
        right_start - left_end if right_start > left_end else left_start - right_end
    )
    return gap <= timedelta(seconds=merge_window_seconds)
